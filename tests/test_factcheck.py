"""Decide-mode fact-checking: pure pipeline, :Verdict store, CLI (issue #80).

External behavior only: the orchestrator runs over fetched rows with injected
fakes (no network); the verdict store runs against a query-interpreting fake
executing the exact Cypher the writer issues (prior art:
tests/test_source_provenance.py). ADR-0005 records the storage decision.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from principle_graph.cli import fact_check_command, main
from principle_graph.factcheck import (
    LocalVerdictLLM,
    SearchResult,
    VerdictOutput,
    candidates_for_domains,
    fact_check_candidates,
    fact_check_notice,
    fact_check_rows,
)
from principle_graph.ledger import LedgerRow
from principle_graph.neo4j import Neo4jGraphWriter


def _row(subject="S", relation="CAUSES", object_="O", source_ref="book-1:ch-1",
         confidence=0.7, domain="economics"):
    return LedgerRow(subject, relation, object_, source_ref, confidence,
                     "evidence text", domain=domain)


class _FakeSearcher:
    def __init__(self, results=None):
        self.queries = []
        self.results = results or [SearchResult("https://x.example/a", "A", "snippet a")]

    def search(self, query):
        self.queries.append(query)
        return self.results


class _FakeLLM:
    def __init__(self, out=None):
        self.claims = []
        self.evidence = []
        self.out = out or VerdictOutput("support", 0.8, "because")

    def check(self, claim, evidence):
        self.claims.append(claim)
        self.evidence.append(evidence)
        return self.out


# --- pure pipeline -------------------------------------------------------

def test_pipeline_builds_receipt_from_row_and_injected_seams():
    searcher = _FakeSearcher()
    llm = _FakeLLM()
    receipts = fact_check_rows([_row()], searcher=searcher, llm=llm,
                               model="glm-5.2", search_provider="ddg",
                               now=lambda: "2026-09-06T00:00:00")
    assert len(receipts) == 1
    r = receipts[0]
    assert (r.subject, r.relation, r.object, r.source_ref) == \
        ("S", "CAUSES", "O", "book-1:ch-1")
    assert r.verdict == "support" and r.confidence == 0.8
    assert r.evidence_urls == ("https://x.example/a",)
    assert r.model == "glm-5.2" and r.search_provider == "ddg"
    assert r.created_at == "2026-09-06T00:00:00"
    assert searcher.queries and "S" in searcher.queries[0]
    assert "https://x.example/a" in llm.evidence[0]


def test_pipeline_preserves_rows_untouched():
    row = _row()
    fact_check_rows([row], searcher=_FakeSearcher(), llm=_FakeLLM(),
                    model="m", search_provider="ddg", now=lambda: "t")
    assert row.confidence == 0.7  # never mutated by verdicts


def test_pipeline_rejects_invalid_verdict_enum():
    with pytest.raises(ValueError):
        fact_check_rows([_row()], searcher=_FakeSearcher(),
                        llm=_FakeLLM(VerdictOutput("maybe", 0.5, "")),
                        model="m", search_provider="ddg", now=lambda: "t")


def test_pipeline_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        fact_check_rows([_row()], searcher=_FakeSearcher(),
                        llm=_FakeLLM(VerdictOutput("support", 1.5, "")),
                        model="m", search_provider="ddg", now=lambda: "t")


# --- trigger: cross-source domain overlap --------------------------------

def test_candidates_surface_when_domain_held_by_other_source():
    existing = [_row("A", "CAUSES", "B", "book-1:ch-1")]
    written = [_row("C", "CAUSES", "D", "book-2:ch-1")]
    candidates = fact_check_candidates(written, existing)
    assert [c.source_ref for c in candidates] == ["book-1:ch-1", "book-2:ch-1"]


def test_candidates_empty_for_same_source_overlap():
    existing = [_row("A", "CAUSES", "B", "book-1:ch-1")]
    written = [_row("C", "CAUSES", "D", "book-1:ch-2")]
    assert fact_check_candidates(written, existing) == []


def test_candidates_skip_untagged_domains():
    written = [_row(domain="")]
    existing = [_row("A", "CAUSES", "B", "book-1:ch-1", domain="")]
    assert fact_check_candidates(written, existing) == []


def test_candidates_dedup_by_identity():
    existing = [_row("A", "CAUSES", "B", "book-1:ch-1")]
    written = [_row("C", "CAUSES", "D", "book-2:ch-1"),
               _row("C", "CAUSES", "D", "book-2:ch-1")]
    out = fact_check_candidates(written, existing)
    assert len(out) == 2  # written duplicate collapses onto one candidate


def test_candidates_for_domains_fires_on_multi_source_domain():
    fetched = {
        "economics": [_row("A", "CAUSES", "B", "book-1:ch-1"),
                      _row("C", "CAUSES", "D", "book-2:ch-1")],
        "": [_row(domain="")],
    }
    seen = []
    def fetch(domain):
        seen.append(domain)
        return fetched.get(domain, [])
    out = candidates_for_domains(["economics", ""], fetch)
    assert seen == ["economics"]  # untagged domains never trigger
    assert [c.source_ref for c in out] == ["book-1:ch-1", "book-2:ch-1"]


def test_candidates_for_domains_quiet_for_single_source_domain():
    def fetch(domain):
        return [_row("A", "CAUSES", "B", "book-1:ch-1")]
    assert candidates_for_domains(["economics"], fetch) == []


def test_fact_check_notice_surfaces_and_survives_bad_writers():
    def fetch(domain):
        return [_row("A", "CAUSES", "B", "book-1:ch-1"),
                _row("C", "CAUSES", "D", "book-2:ch-1")]
    lines = fact_check_notice(["economics"], fetch)
    assert len(lines) == 1 and "2 row(s)" in lines[0]
    assert fact_check_notice([""], fetch) == []
    assert fact_check_notice(["economics"], None) == []
    def broken(domain):
        raise RuntimeError("down")
    assert fact_check_notice(["economics"], broken) == []


# --- :Verdict store (fake graph executes the exact Cypher) ---------------

class _Record(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return self

    counters = None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, graph):
        self.graph = graph

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, query, **params):
        g = self.graph
        if "$domain" in query and "WHERE e.domain" in query:
            return _Result([
                _Record(subject=s, relation=rel, object=o, source_ref=src,
                        confidence=row["confidence"], evidence=row["evidence"],
                        scope_conditions="", domain=row["domain"],
                        raw_relation=row["raw_relation"])
                for (s, rel, o, src), row in g["rows"].items()
                if row["domain"] == params["domain"]
            ])
        if "CREATE (v:Verdict" in query:
            identity = (params["subject"], params["relation"], params["object"],
                        params["source_ref"])
            if identity not in g["rows"]:
                return _Result([])
            g["verdicts"].append({
                "id": params["verdict_id"], "verdict": params["verdict"],
                "confidence": params["confidence"],
                "evidence_urls": params["evidence_urls"],
                "model": params["model"],
                "search_provider": params["search_provider"],
                "reasoning": params["reasoning"], "checks": identity,
                "created_at": params["created_at"],
            })
            return _Result([_Record(id=params["verdict_id"])])
        if "(v:Verdict)-[:CHECKS]->(e)" in query:
            return _Result([
                _Record(subject=i[0], relation=i[1], object=i[2], source_ref=i[3],
                        **{k: v[k] for k in ("verdict", "confidence",
                                             "evidence_urls", "model",
                                             "search_provider", "reasoning")})
                for v, i in sorted(
                    ((v, v["checks"]) for v in g["verdicts"]),
                    key=lambda pair: pair[0]["id"])
            ])
        raise AssertionError(f"fake does not handle query: {query}")


class _FakeDriver:
    def __init__(self, graph):
        self.graph = graph

    def session(self, database=None):
        return _FakeSession(self.graph)

    def close(self):
        pass


@pytest.fixture
def graph():
    return {
        "rows": {
            ("S", "CAUSES", "O", "book-1:ch-1"): {
                "confidence": 0.7, "evidence": "ev", "domain": "economics",
                "raw_relation": "CAUSES",
            },
        },
        "verdicts": [],
    }


def _writer(graph):
    return Neo4jGraphWriter(_FakeDriver(graph))


def _receipt(**overrides):
    from principle_graph.factcheck import VerdictReceipt
    defaults = dict(subject="S", relation="CAUSES", object="O",
                    source_ref="book-1:ch-1", verdict="support",
                    confidence=0.8, evidence_urls=("https://x.example/a",),
                    model="glm-5.2", search_provider="ddg", reasoning="r",
                    created_at="2026-09-06T00:00:00")
    return VerdictReceipt(**{**defaults, **overrides})


def test_save_verdicts_creates_check_wired_nodes(graph):
    report = _writer(graph).save_verdicts([_receipt()])
    assert report == {"verdicts_created": 1, "rows_not_found": 0}
    assert graph["verdicts"][0]["verdict"] == "support"
    assert graph["verdicts"][0]["confidence"] == 0.8
    assert graph["verdicts"][0]["checks"] == ("S", "CAUSES", "O", "book-1:ch-1")
    # Receipt clock is persisted verbatim — the store binds $created_at from
    # the receipt, not the DB clock, so receipt and node timestamps agree.
    assert graph["verdicts"][0]["created_at"] == "2026-09-06T00:00:00"


def test_save_verdicts_counts_missing_rows(graph):
    report = _writer(graph).save_verdicts(
        [_receipt(subject="Ghost", source_ref="book-1:ch-1")])
    assert report == {"verdicts_created": 0, "rows_not_found": 1}


def test_save_verdicts_appends_never_rewrites(graph):
    w = _writer(graph)
    w.save_verdicts([_receipt()])
    w.save_verdicts([_receipt(verdict="refute")])
    assert [v["verdict"] for v in graph["verdicts"]] == ["support", "refute"]
    assert graph["verdicts"][0]["id"] != graph["verdicts"][1]["id"]
    # Row properties were never the target of a write: the fake has no SET path
    # and the store only MATCHes rows before CREATE-ing verdicts.
    assert graph["rows"][("S", "CAUSES", "O", "book-1:ch-1")]["confidence"] == 0.7


def test_rows_for_domain_roundtrip(graph):
    rows = _writer(graph).rows_for_domain("economics")
    assert len(rows) == 1 and rows[0].domain == "economics"
    assert rows[0].relation == "CAUSES"


def test_verdicts_for_source_walk(graph):
    graph["verdicts"].append({
        "id": "v1", "verdict": "support", "confidence": 0.8,
        "evidence_urls": ["https://x.example/a"], "model": "glm-5.2",
        "search_provider": "ddg", "reasoning": "r",
        "checks": ("S", "CAUSES", "O", "book-1:ch-1"),
    })
    out = _writer(graph).verdicts_for_source("book-1")
    assert len(out) == 1
    assert out[0]["verdict"] == "support"
    assert out[0]["source_ref"] == "book-1:ch-1"


# --- local verdict LLM (rides the extraction transport, ADR-0001) --------

def test_local_verdict_llm_parses_tool_call():
    from principle_graph.llm_gateway import MessagesResponse, ToolUseBlock

    class _Client:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return MessagesResponse(content=[ToolUseBlock(
                type="tool_use", name="propose_verdict",
                input={"verdict": "refute", "confidence": 0.9,
                       "reasoning": "contradicted"})])

    client = _Client()
    llm = LocalVerdictLLM(client, model="glm-5.2")
    out = llm.check("S CAUSES O", "evidence: [https://x]")
    assert out == VerdictOutput("refute", 0.9, "contradicted")
    assert client.kwargs["tools"][0]["name"] == "propose_verdict"


# --- CLI -----------------------------------------------------------------

def test_cli_fact_check_over_domain(monkeypatch):
    from principle_graph import cli as cli_mod

    class _WriterStub:
        def rows_for_domain(self, domain):
            return [_row()]

        def save_verdicts(self, receipts):
            return {"verdicts_created": len(receipts), "rows_not_found": 0}

    def fake_build(settings):
        return None, _WriterStub(), _FakeSearcher(), _FakeLLM()

    monkeypatch.setattr(cli_mod, "_build_fact_check_seams", fake_build)
    out = io.StringIO()
    code = fact_check_command(cli_mod.Settings(), domain="economics", out=out)
    assert code == 0
    text = out.getvalue()
    assert "1 verdict(s)" in text and "support" in text


def test_cli_fact_check_over_source_walks_verdicts(monkeypatch):
    from principle_graph import cli as cli_mod

    class _WriterStub:
        def provenance_for_source(self, source_id):
            return [_row()]

        def save_verdicts(self, receipts):
            return {"verdicts_created": 1, "rows_not_found": 0}

        def verdicts_for_source(self, source_id):
            return [{"verdict": "support"}]

    monkeypatch.setattr(
        cli_mod, "_build_fact_check_seams",
        lambda settings: (None, _WriterStub(), _FakeSearcher(), _FakeLLM()))
    out = io.StringIO()
    code = fact_check_command(cli_mod.Settings(), source="book-1", out=out)
    assert code == 0
    assert "Verdicts recorded for source 'book-1': 1" in out.getvalue()


def test_cli_rejects_domain_and_source_together():
    from principle_graph.config import Settings
    out = io.StringIO()
    code = fact_check_command(Settings(), domain="d", source="s", out=out)
    assert code == 2


def test_cli_subcommand_registered():
    from principle_graph.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["fact-check", "--domain", "economics"])
    assert args.command == "fact-check"
    assert args.domain == "economics"
