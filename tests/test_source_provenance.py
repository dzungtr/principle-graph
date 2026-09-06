"""Source provenance: :Source nodes, FROM_SOURCE edges, backfill, walk (issue #79).

External behavior only: a query-interpreting fake executes the exact Cypher the
writer issues, in memory (prior art: tests/test_ledger_migration.py), plus the
CLI surface. The pure prefix rule is pinned in tests/test_ledger_core.py.
"""
from __future__ import annotations

import copy
import io
from contextlib import redirect_stdout

import pytest

from principle_graph.cli import backfill_sources_command, main, provenance_command
from principle_graph.neo4j import Neo4jGraphWriter
from principle_graph.reduction import GraphEdge


class _Record(dict):
    """Neo4j-record stand-in: mapping access plus ``get``."""

    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return _Consume()

    def __iter__(self):
        return iter(self._rows)


class _Consume:
    counters = None


class _FakeSession:
    """Executes the exact queries the source-provenance writer issues, in memory."""

    def __init__(self, graph):
        self.graph = graph

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, query, **params):
        g = self.graph
        if "FROM_SOURCE]-(e:ExtractionEvent)" in query:  # provenance walk
            rows = [
                _Record(subject=s, relation=rel, object=o, source_ref=src,
                        confidence=row["confidence"], evidence=row["evidence"],
                        scope_conditions=row["scope_conditions"], domain=row["domain"])
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
                if (s, rel, o, src) in g.from_source
                and src.split(":", 1)[0] == params["source_id"]
            ]
            return _Result(rows)
        if "MERGE (s)-[:REPORTED]->" in query:  # row merge (ingest; may link source)
            identity = (params["subject"], params["relation"], params["object"],
                        params["source_ref"])
            if identity not in g.rows:
                g.rows[identity] = {
                    "confidence": params["confidence"],
                    "evidence": params["evidence"],
                    "scope_conditions": params["scope_conditions"],
                    "domain": params["domain"],
                    "created_at": g.tick(),
                    "updated_at": g.tick(),
                }
            if "MERGE (src:Source" in query:
                g.link_source(identity, params["source_id"])
            return _Result([])
        if "SET e.confidence = $confidence" in query:  # refresh row update (+ link)
            identity = (params["subject"], params["relation"], params["object"],
                        params["source_ref"])
            row = g.rows[identity]
            row.update(confidence=params["confidence"], evidence=params["evidence"],
                       scope_conditions=params["scope_conditions"],
                       domain=params["domain"], updated_at=g.tick())
            if "MERGE (src:Source" in query:
                g.link_source(identity, params["source_id"])
            return _Result([])
        if "EXISTS { (e)-[:FROM_SOURCE]->() }" in query:  # backfill rows load
            return _Result([
                _Record(subject=s, relation=rel, object=o, source_ref=src,
                        created_at=row["created_at"],
                        has_source=(s, rel, o, src) in g.from_source)
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
                if src  # rows without a source ref cannot carry provenance
            ])
        if "collect(src.id)" in query:  # existing :Source ids
            return _Result([_Record(ids=sorted(g.sources))])
        if "MERGE (src:Source {id: $source_id})" in query:  # backfill node merge
            if params["source_id"] not in g.sources:  # ON CREATE SET only
                g.sources[params["source_id"]] = {"first_seen": params["first_seen"]}
            return _Result([])
        if "MATCH (src:Source {id: $source_id})" in query:  # backfill edge link
            identity = (params["subject"], params["relation"], params["object"],
                        params["source_ref"])
            g.from_source.add(identity)
            return _Result([])
        if "RETURN e.source_ref AS source_ref, e.confidence AS confidence" in query:
            rows = [  # existing ledger rows for the triple, created_at order
                _Record(source_ref=src, confidence=row["confidence"],
                        evidence=row["evidence"], scope_conditions=row["scope_conditions"])
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
                if (s, rel, o) == (params["subject"], params["relation"], params["object"])
            ]
            return _Result(rows)
        if "RETURN r.confidence AS confidence" in query:  # arrow confidence read
            arrow = g.arrows.setdefault(
                (params["subject"], _rel(query), params["object"]),
                {"confidence": None, "updated_at": g.tick()},
            )
            return _Result([_Record(confidence=arrow.get("confidence"))])
        if "MERGE (s)-[r:" in query:  # arrow aggregate write
            arrow = g.arrows.setdefault(
                (params["subject"], _rel(query), params["object"]),
                {"confidence": None, "updated_at": g.tick()},
            )
            arrow["confidence"] = params["aggregate_confidence"]
            arrow["updated_at"] = g.tick()
            return _Result([])
        raise AssertionError(f"fake does not handle query: {query}")


def _rel(query: str) -> str:
    start = query.index("-[r:") + 4
    end = query.index("]", start)
    return query[start:end].split("{")[0]


class _FakeDriver:
    """In-memory property graph with :Source nodes and a write-tick clock."""

    def __init__(self):
        self.arrows: dict[tuple[str, str, str], dict] = {}
        self.rows: dict[tuple[str, str, str, str], dict] = {}
        self.sources: dict[str, dict] = {}
        self.from_source: set[tuple[str, str, str, str]] = set()
        self._clock = 0

    def tick(self) -> str:
        self._clock += 1
        return f"t{self._clock}"

    def session(self, database=None):
        return _FakeSession(self)

    def close(self):
        return None

    def link_source(self, identity, source_id):
        """``MERGE (:Source {id}) ON CREATE SET first_seen`` + ``MERGE`` edge."""
        if source_id not in self.sources:
            self.sources[source_id] = {"first_seen": self.tick()}
        self.from_source.add(identity)

    def add_row(self, subject, relation, object_, source_ref, confidence=0.5,
                evidence="witness", scope_conditions=""):
        """Seed a ledger row directly (post-migration state, unlinked)."""
        identity = (subject, relation.upper(), object_, source_ref)
        self.rows[identity] = {
            "confidence": confidence,
            "evidence": evidence,
            "scope_conditions": scope_conditions,
            "domain": "",
            "created_at": self.tick(),
            "updated_at": self.tick(),
        }

    def snapshot(self):
        return copy.deepcopy({"arrows": self.arrows, "rows": self.rows,
                              "sources": self.sources,
                              "from_source": set(self.from_source)})


def _writer(driver, repeat_mode="keep-first") -> Neo4jGraphWriter:
    return Neo4jGraphWriter(driver, database="neo4j", repeat_mode=repeat_mode)


# --- ingestion write path ---------------------------------------------------


def test_new_ingestion_writes_source_node_and_from_source_edge():
    driver = _FakeDriver()
    _writer(driver).upsert_extraction(
        GraphEdge("a", "supports", "b", 0.5, "book-1:chapter-2/page-1", ("witness",), ""))
    assert list(driver.sources) == ["book-1"]
    assert driver.sources["book-1"]["first_seen"]
    (identity,) = driver.from_source
    assert identity == ("a", "SUPPORTS", "b", "book-1:chapter-2/page-1")


def test_source_ref_string_is_unchanged_on_the_row():
    """Ledger identity depends on the raw source_ref string (ADR-0002) — the
    :Source link is additive, never a rewrite of the row's reference."""
    driver = _FakeDriver()
    _writer(driver).upsert_extraction(
        GraphEdge("a", "supports", "b", 0.5, "book-1:chapter-2/page-1", ("witness",), ""))
    ((identity, _sid),) = ((i, i[3]) for i in driver.from_source)
    assert identity[3] == "book-1:chapter-2/page-1"


def test_keep_first_reingest_is_a_noop_for_sources_and_edges():
    """What #79 owns — rows, :Source nodes, FROM_SOURCE links — never moves on a
    keep-first re-ingest. (The arrow's derived recompute always fires; that is
    pre-existing ADR-0002 behavior, not provenance state.)"""
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "doc:chunk-1", ("first",), ""))
    before = driver.snapshot()
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.9, "doc:chunk-1", ("second",), ""))
    after = driver.snapshot()
    assert (after["rows"], after["sources"], after["from_source"]) == (
        before["rows"], before["sources"], before["from_source"])
    assert len(driver.from_source) == 1


def test_refresh_reingest_reuses_the_existing_source_link():
    driver = _FakeDriver()
    writer = _writer(driver, repeat_mode="refresh")
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "doc:chunk-1", ("first",), ""))
    first_seen = driver.sources["doc"]["first_seen"]
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.9, "doc:chunk-1", ("refined",), ""))
    assert len(driver.from_source) == 1
    assert driver.sources["doc"]["first_seen"] == first_seen  # node not recreated


def test_one_source_node_per_source_id_across_chunks():
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "book-1:ch-1", ("e1",), ""))
    writer.upsert_extraction(GraphEdge("b", "cites", "c", 0.5, "book-1:ch-2", ("e2",), ""))
    assert list(driver.sources) == ["book-1"]
    assert len(driver.from_source) == 2


def test_distinct_sources_get_distinct_nodes():
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "book-1:ch-1", ("e1",), ""))
    writer.upsert_extraction(GraphEdge("b", "cites", "c", 0.5, "book-2:ch-1", ("e2",), ""))
    assert sorted(driver.sources) == ["book-1", "book-2"]


def test_malformed_source_ref_fails_before_any_write():
    driver = _FakeDriver()
    with pytest.raises(ValueError, match="source id"):
        _writer(driver).upsert_extraction(
            GraphEdge("a", "supports", "b", 0.5, "chunk-1", ("witness",), ""))
    assert driver.rows == {} and driver.sources == {}


# --- backfill pass ------------------------------------------------------------


def _backfill_driver() -> _FakeDriver:
    driver = _FakeDriver()
    driver.add_row("a", "SUPPORTS", "b", "book-1:ch-1", 0.6, "book one says")
    driver.add_row("b", "CITES", "c", "book-1:ch-2", 0.5, "book one cites")
    driver.add_row("c", "SUPPORTS", "d", "book-2:ch-1", 0.4, "book two says")
    return driver


def test_backfill_creates_one_source_per_prefix_with_first_seen_metadata():
    driver = _backfill_driver()
    report = _writer(driver).backfill_sources()
    assert report == {"rows_seen": 3, "edges_created": 3, "edges_already_linked": 0,
                      "sources_created": 2, "sources_already_present": 0}
    assert sorted(driver.sources) == ["book-1", "book-2"]
    # first-seen = the earliest row's created_at for that source
    first_book1 = min(row["created_at"] for identity, row in driver.rows.items()
                      if identity[3].startswith("book-1:"))
    assert driver.sources["book-1"]["first_seen"] == first_book1
    assert len(driver.from_source) == 3


def test_backfill_second_run_is_a_full_noop():
    driver = _backfill_driver()
    writer = _writer(driver)
    writer.backfill_sources()
    before = driver.snapshot()
    report = writer.backfill_sources()
    assert report == {"rows_seen": 3, "edges_created": 0, "edges_already_linked": 3,
                      "sources_created": 0, "sources_already_present": 2}
    assert driver.snapshot() == before  # timestamps included: no write fired


def test_backfill_leaves_row_values_and_source_refs_untouched():
    """Row identity keys carry the source_ref string; equality proves the
    strings and every row value survived the backfill untouched."""
    driver = _backfill_driver()
    writer = _writer(driver)
    rows_before = copy.deepcopy(driver.rows)
    writer.backfill_sources()
    assert driver.rows == rows_before


def test_backfill_skips_rows_without_a_source_ref():
    driver = _FakeDriver()
    driver.add_row("a", "SUPPORTS", "b", "book-1:ch-1", 0.6, "linked")
    driver.add_row("x", "SUPPORTS", "y", "", 0.5, "legacy row without ref")
    report = _writer(driver).backfill_sources()
    assert report["rows_seen"] == 1
    assert driver.from_source == {("a", "SUPPORTS", "b", "book-1:ch-1")}


def test_backfill_rejects_malformed_source_refs():
    driver = _FakeDriver()
    driver.add_row("a", "SUPPORTS", "b", "no-prefix-ref", 0.6, "sloppy ref")
    with pytest.raises(ValueError, match="no-prefix-ref"):
        _writer(driver).backfill_sources()


def test_backfill_counts_ingest_linked_rows_as_already_linked():
    driver = _FakeDriver()
    _writer(driver).upsert_extraction(
        GraphEdge("a", "supports", "b", 0.5, "doc:chunk-1", ("witness",), ""))
    report = _writer(driver).backfill_sources()
    assert report == {"rows_seen": 1, "edges_created": 0, "edges_already_linked": 1,
                      "sources_created": 0, "sources_already_present": 1}


# --- provenance walk ----------------------------------------------------------


def test_provenance_walk_returns_all_rows_including_later_contradicted():
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(
        GraphEdge("a", "supports", "b", 0.6, "book-1:ch-1", ("book one says",), ""))
    # a later extraction from another source contradicts the claim at the arrow
    writer.upsert_extraction(
        GraphEdge("a", "supports", "b", 0.1, "book-2:ch-9", ("book two denies",), ""))
    rows = writer.provenance_for_source("book-1")
    assert [(row.subject, row.relation, row.object) for row in rows] == [("a", "SUPPORTS", "b")]
    (row,) = rows
    assert row.source_ref == "book-1:ch-1"
    assert row.confidence == 0.6
    assert row.evidence == "book one says"


def test_provenance_walk_orders_rows_by_creation():
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "book-1:ch-1", ("first",), ""))
    writer.upsert_extraction(GraphEdge("b", "cites", "c", 0.5, "book-1:ch-2", ("second",), ""))
    rows = writer.provenance_for_source("book-1")
    assert [row.source_ref for row in rows] == ["book-1:ch-1", "book-1:ch-2"]
    assert [row.evidence for row in rows] == ["first", "second"]


def test_provenance_walk_unknown_source_returns_empty():
    driver = _backfill_driver()
    assert _writer(driver).provenance_for_source("book-9") == []


# --- CLI surface ----------------------------------------------------------------


def test_cli_backfill_sources_reports_and_exits_zero():
    driver = _backfill_driver()
    import principle_graph.cli as cli
    original = cli._driver
    cli._driver = lambda _settings: driver
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = backfill_sources_command(cli.Settings.from_env(), out=out)
    finally:
        cli._driver = original
    assert code == 0
    assert "sources_created=2" in out.getvalue() and "edges_created=3" in out.getvalue()


def test_cli_provenance_walks_a_source():
    driver = _FakeDriver()
    _writer(driver).upsert_extraction(
        GraphEdge("a", "supports", "b", 0.6, "book-1:ch-1", ("book one says",), ""))
    import principle_graph.cli as cli
    original = cli._driver
    cli._driver = lambda _settings: driver
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = provenance_command(cli.Settings.from_env(), "book-1", out=out)
    finally:
        cli._driver = original
    assert code == 0
    text = out.getvalue()
    assert "book-1:ch-1" in text and "SUPPORTS" in text and "book one says" in text


def test_cli_provenance_unknown_source_reports_empty_and_exits_zero():
    driver = _FakeDriver()
    import principle_graph.cli as cli
    original = cli._driver
    cli._driver = lambda _settings: driver
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = provenance_command(cli.Settings.from_env(), "book-9", out=out)
    finally:
        cli._driver = original
    assert code == 0
    assert "book-9" in out.getvalue()


def test_cli_backfill_sources_help_documents_behavior_and_idempotency(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["backfill-sources", "--help"])
    assert exit_info.value.code == 0
    assert "idempotent" in capsys.readouterr().out.lower()


def test_cli_provenance_help_documents_the_walk(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["provenance", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "source" in out and "claimed" in out
