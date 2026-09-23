"""Ingest novelty filter: Jev decision-model gate (issue #93, ADR-0006).

Network-free and database-free: the Jev decisions client runs over an injected
fake transport (prior art: the transport seam in tests/test_llm_gateway.py and
tests/test_factcheck.py). Frozen decisions from the issue are exercised here:
model-knowledge novelty frame, dedup before classification, argmax gate,
hard-abort transport failure, and the always-on escape hatch.
"""
from __future__ import annotations

import io
import json

import pytest

from principle_graph.novelty import (
    JevDecisionsClient,
    NoveltyError,
    apply_novelty_filter,
    render_claim,
)


def _candidate(subject="TCP", relation="MAY_DESCRIBE", object_="Postel's Law",
               evidence="be conservative in what you send", **overrides):
    candidate = {
        "subject": subject, "subject_type": "technology",
        "relation": relation, "object": object_, "object_type": "principle",
        "confidence": 0.8, "evidence": evidence,
        "scope_conditions": "", "source_ref": "book-1:ch-1",
    }
    candidate.update(overrides)
    return candidate


class _FakeTransport:
    """Record requests, replay queued response bodies; raise queued errors."""

    def __init__(self, responses=None, errors=None):
        self.requests: list[tuple[str, dict, bytes]] = []
        self._responses = list(responses or [])
        self._errors = list(errors or [])

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, dict(headers), body))
        if self._errors:
            raise self._errors.pop(0)
        return self._responses.pop(0)


def _decision_body(choice="novel", confidence=0.71,
                   probabilities=None):
    probabilities = probabilities or {"novel": 0.71, "common_sense": 0.24, "noise": 0.05}
    return json.dumps({
        "answers": {"novelty": {"type": "choice", "choice": choice,
                                "confidence": confidence,
                                "probabilities": probabilities}},
        "usage": {"input_tokens": 96, "output_tokens": 20, "cost": 0.000004},
    }).encode()


# --- decision 7 + claim rendering rule ------------------------------------

def test_render_claim_lowercases_raw_verb_and_splits_underscores():
    assert render_claim(_candidate()) == "TCP may describe Postel's Law"


def test_client_sends_plain_knowledge_state_with_claim_and_evidence_only():
    transport = _FakeTransport([_decision_body()])
    client = JevDecisionsClient(
        base_url="https://openrouter.ai/api/alpha/decisions",
        model="typesafe/jev-1.13", api_key="k", transport=transport)
    client.classify([_candidate()])
    url, headers, body = transport.requests[0]
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert headers["Authorization"] == "Bearer k"
    payload = json.loads(body)
    assert payload["model"] == "typesafe/jev-1.13"
    assert set(payload["state"]) == {"claim", "evidence"}
    assert payload["state"] == {"claim": "TCP may describe Postel's Law",
                                "evidence": "be conservative in what you send"}
    assert set(payload["questions"]["novelty"]["criteria"]) == \
        {"noise", "common_sense", "novel"}


# --- decision 2: dedup by casefolded raw triple ---------------------------

def test_dedup_one_call_per_unique_casefolded_triple():
    transport = _FakeTransport([_decision_body()])
    client = JevDecisionsClient(api_key="k", transport=transport)
    kept, stats = apply_novelty_filter(
        [_candidate(),
         _candidate(relation="may_describe", subject="tcp")],
        client)
    assert len(transport.requests) == 1  # one decision call per unique proposal
    assert len(kept) == 2  # duplicates share the verdict; both flow on


# --- decisions 1 + 5: argmax gate -----------------------------------------

@pytest.mark.parametrize("choice,expected", [
    ("novel", True), ("noise", False), ("common_sense", False),
])
def test_gate_is_pure_argmax_choice(choice, expected):
    transport = _FakeTransport([_decision_body(choice=choice)])
    client = JevDecisionsClient(api_key="k", transport=transport)
    kept, stats = apply_novelty_filter([_candidate()], client)
    assert (len(kept) == 1) is expected
    assert stats.novelty_calls == 1
    if not expected:
        assert getattr(stats, f"filtered_{choice}") == 1


# --- decision 3: stats aggregates -----------------------------------------

def test_stats_counts_and_mean_probabilities():
    transport = _FakeTransport([
        _decision_body(choice="novel",
                       probabilities={"novel": 0.9, "common_sense": 0.05, "noise": 0.05}),
        _decision_body(choice="noise",
                       probabilities={"novel": 0.1, "common_sense": 0.2, "noise": 0.7}),
    ])
    client = JevDecisionsClient(api_key="k", transport=transport)
    kept, stats = apply_novelty_filter(
        [_candidate(), _candidate(subject="Gravity", relation="ATTRACTS",
                                  object_="Mass", evidence="apples fall")],
        client)
    assert len(kept) == 1
    assert stats.novelty_calls == 2
    assert stats.filtered_noise == 1 and stats.filtered_common_sense == 0
    assert stats.mean_probabilities == {"novel": 0.5, "common_sense": 0.125,
                                        "noise": 0.375}


def test_stats_render_in_transcript():
    # NoveltyStats had its own render(); it was dead in production — the
    # transcript renders via IngestStats.render (covered by
    # test_orchestrator_applies_filter_between_extract_and_resolve). Cover the
    # aggregate itself instead, through apply_novelty_filter's return value.
    transport = _FakeTransport([_decision_body(choice="noise")])
    client = JevDecisionsClient(api_key="k", transport=transport)
    _, stats = apply_novelty_filter([_candidate()], client)
    assert stats.novelty_calls == 1
    assert stats.filtered_noise == 1 and stats.filtered_common_sense == 0


# --- decision 4: hard abort ------------------------------------------------

def test_transport_failure_aborts_before_any_commit():
    transport = _FakeTransport(errors=[OSError("connection refused")])
    client = JevDecisionsClient(api_key="k", transport=transport)
    with pytest.raises(NoveltyError):
        apply_novelty_filter([_candidate()], client)


def test_non_choice_answer_type_is_transport_failure():
    transport = _FakeTransport([json.dumps({
        "answers": {"novelty": {"type": "boolean", "value": True}}}).encode()])
    client = JevDecisionsClient(api_key="k", transport=transport)
    with pytest.raises(NoveltyError):
        apply_novelty_filter([_candidate()], client)


def test_unexpected_choice_key_is_transport_failure():
    transport = _FakeTransport([json.dumps({
        "answers": {"novelty": {"type": "choice", "choice": "maybe"}}}).encode()])
    client = JevDecisionsClient(api_key="k", transport=transport)
    with pytest.raises(NoveltyError):
        apply_novelty_filter([_candidate()], client)


def test_missing_api_key_aborts_clearly():
    client = JevDecisionsClient(api_key="", transport=_FakeTransport())
    with pytest.raises(NoveltyError, match="OPENROUTER_API_KEY"):
        apply_novelty_filter([_candidate()], client)


# --- decision 6: opt-out returns current behavior --------------------------

def test_orchestrator_opt_out_skips_seam(monkeypatch, tmp_path):
    from principle_graph.extraction import ExtractionRun
    from principle_graph.orchestrator import IngestOrchestrator

    calls = []

    class _ExplodingFilter:
        def classify(self, claims):
            calls.append(claims)
            raise AssertionError("filter must not run when opted out")

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.candidates.append(_candidate())
            run.completed_chunks.append("c1")
            return run

    class _Store:
        def find_entities(self, name, entity_type):
            return []
        def search_similar(self, *a, **k):
            return []
        def structural_corroboration(self, *a, **k):
            return None

    class _Resolver:
        def resolve(self, name, entity_type, source_ref=None):
            from principle_graph.resolution import Entity, Resolution
            return Resolution("create", name, entity_type,
                              Entity(f"t:{name}", name, entity_type))

    class _Writer:
        unknown_relation_counts = {}
        unknown_domain_counts = {}
        def get_edge(self, *a):
            return None
        def upsert_entity(self, entity):
            pass
        def upsert_edge(self, edge):
            pass

    source = tmp_path / "s.md"
    source.write_text("# hi\n\nsome text\n", encoding="utf-8")
    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=_Writer(), novelty_filter=None)
    result = orch.run(source)
    assert calls == []  # opt-out seam skipped entirely
    assert result.stats.filtered_noise == 0 and result.stats.novelty_calls == 0


def test_orchestrator_applies_filter_between_extract_and_resolve(monkeypatch, tmp_path):
    from principle_graph.extraction import ExtractionRun
    from principle_graph.resolution import Entity, Resolution
    from principle_graph.review import ReviewResult
    from principle_graph.orchestrator import IngestOrchestrator

    class _Filter:
        def __init__(self):
            self.saw = None

        def classify(self, claims):
            self.saw = list(claims)
            from principle_graph.novelty import NoveltyVerdict
            return [NoveltyVerdict(_candidate(), "novel", 0.9,
                                   {"novel": 0.9, "common_sense": 0.05, "noise": 0.05}),
                    NoveltyVerdict(_candidate(subject="Filler"), "noise", 0.8,
                                   {"novel": 0.1, "common_sense": 0.1, "noise": 0.8})]

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.candidates.extend([_candidate(),
                                   _candidate(subject="Filler")])
            run.completed_chunks.append("c1")
            return run

    seen_names = []

    class _Store:
        def find_entities(self, name, entity_type):
            seen_names.append(name)
            return []
        def search_similar(self, *a, **k):
            return []
        def structural_corroboration(self, *a, **k):
            return None

    class _Writer:
        unknown_relation_counts = {}
        unknown_domain_counts = {}
        def get_edge(self, *a):
            return None
        def upsert_entity(self, entity):
            pass
        def upsert_edge(self, edge):
            pass

    flt = _Filter()
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nsome text\n", encoding="utf-8")
    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=_Writer(), novelty_filter=flt)
    monkeypatch.setattr("principle_graph.orchestrator.review_and_commit",
                        lambda delta, writer, **kw: ReviewResult(approved=delta,
                                                                 rejected=[]))
    result = orch.run(source)
    # noise candidate never reached the resolver (dropped before resolve)
    assert seen_names == ["TCP", "Postel's Law"]
    assert result.stats.filtered_noise == 1
    assert len(result.delta.new_edges) == 1
    # decision 7: aggregates render through the production seam (IngestStats.render,
    # called from cli.py). Means: novel=(0.9+0.1)/2, common_sense=(0.05+0.1)/2,
    # noise=(0.05+0.8)/2.
    transcript = result.stats.render()
    assert "filtered items: 1 (noise=1, common_sense=0)" in transcript
    assert "mean probabilities:" in transcript


# --- config surface ---------------------------------------------------------

def test_settings_jev_fields_and_env_overrides(monkeypatch):
    from principle_graph.config import Settings
    monkeypatch.delenv("PG_JEV_BASE_URL", raising=False)
    monkeypatch.delenv("PG_JEV_MODEL", raising=False)
    monkeypatch.delenv("PG_JEV_TIMEOUT", raising=False)
    s = Settings.from_env()
    assert s.jev_base_url == "https://openrouter.ai/api/alpha/decisions"
    assert s.jev_model == "typesafe/jev-1.13"
    assert s.jev_timeout == 10.0
    monkeypatch.setenv("PG_JEV_BASE_URL", "https://example/api")
    monkeypatch.setenv("PG_JEV_MODEL", "typesafe/jev-x")
    monkeypatch.setenv("PG_JEV_TIMEOUT", "3.5")
    s = Settings.from_env()
    assert (s.jev_base_url, s.jev_model, s.jev_timeout) == \
        ("https://example/api", "typesafe/jev-x", 3.5)


# --- CLI --------------------------------------------------------------------

def test_cli_no_novelty_filter_flag_registered():
    from principle_graph.cli import build_parser
    args = build_parser().parse_args(["ingest", "s.md", "--no-novelty-filter"])
    assert args.no_novelty_filter is True
    args = build_parser().parse_args(["ingest", "s.md"])
    assert args.no_novelty_filter is False


def test_ingest_missing_openrouter_key_aborts(monkeypatch, tmp_path):
    from principle_graph import cli as cli_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    source = tmp_path / "s.md"
    source.write_text("text", encoding="utf-8")
    code = cli_mod.ingest_command(cli_mod.Settings(), str(source),
                                  yes=True, out=io.StringIO())
    assert code == 2


def test_ingest_opt_out_needs_no_key(monkeypatch, tmp_path):
    from principle_graph import cli as cli_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    source = tmp_path / "s.md"
    source.write_text("text", encoding="utf-8")
    code = cli_mod.ingest_command(cli_mod.Settings(), str(source), yes=True,
                                  no_novelty_filter=True, out=io.StringIO())
    assert code != 2  # past the key check (fails later on Neo4j preflight, code 1)
