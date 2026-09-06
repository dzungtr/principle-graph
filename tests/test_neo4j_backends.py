"""Recording-fake tests for the Neo4j persistence adapters."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from principle_graph.config import Settings
from principle_graph.neo4j import (
    Neo4jEntityStore,
    Neo4jGraphWriter,
    RejectedRecordSink,
    load_existing_edges,
)
from principle_graph.reduction import GraphEdge, GraphEntity
from principle_graph.resolution import Entity


class RecordingResult:
    """Result wrapper exposing ``single()`` like a real Neo4j result."""

    def __init__(self, rows: list):
        self._rows = rows

    def single(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class RecordingSession:
    """Captures every executed Cypher query and its parameters."""

    def __init__(self, rows: list):
        self._rows = rows
        self.queries: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        self.queries.append((query, params))
        return RecordingResult(self._rows)

    def close(self):
        return None


class RecordingDriver:
    """Fake Neo4j driver that exposes the session as a RecordingSession."""

    def __init__(self, rows: list | None = None):
        self.sessions: list[RecordingSession] = []
        self._rows = rows or []

    def session(self, database=None):
        session = RecordingSession(self._rows)
        self.sessions.append(session)
        return session


def test_graph_writer_upserts_entity_with_timestamps_and_embedding():
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, database="neo4j")

    writer.upsert_entity(GraphEntity("Marie Curie", "Person", embedding=(0.1, 0.2)))

    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert "MERGE (e:Entity {name: $name, type: $type})" in query
    assert "ON CREATE SET e.created_at" in query
    assert "ON MATCH SET e.updated_at" in query
    assert params == {"name": "Marie Curie", "type": "Person", "embedding": [0.1, 0.2]}


def test_graph_writer_upserts_edge_with_uppercase_relation_and_provenance():
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, database="neo4j")

    writer.upsert_edge(GraphEdge("a", "supports", "b", 0.7, "s1", ("ev1",), "scope"))

    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert "MERGE (s)-[r:SUPPORTS]->(o)" in query
    assert "ON CREATE SET r.created_at" in query
    assert "SET r.confidence" in query and "r.evidence" in query
    assert params == {
        "subject": "a",
        "object": "b",
        "confidence": 0.7,
        "evidence": ["ev1"],
        "scope_conditions": "scope",
        "source_ref": "s1",
    }


def test_graph_writer_get_edge_returns_none_when_missing():
    driver = RecordingDriver(rows=[])
    writer = Neo4jGraphWriter(driver)
    assert writer.get_edge("a", "supports", "b") is None
    (session,) = driver.sessions
    [(query, _)] = session.queries
    assert "MATCH (s:Entity {name: $subject})-[r:SUPPORTS]->(o:Entity {name: $object})" in query


def test_graph_writer_get_edge_sources_provenance_from_rows():
    row = {
        "subject": "a",
        "relation": "SUPPORTS",
        "object": "b",
        "confidence": 0.8,
        "scope_conditions": "scope",
        "legacy_source_ref": None,
        "legacy_evidence": None,
        "row_refs": ["s1", "s2"],
        "row_evidence": ["ev1", "ev2"],
    }
    driver = RecordingDriver(rows=[row])
    writer = Neo4jGraphWriter(driver)
    edge = writer.get_edge("a", "supports", "b")
    assert edge == GraphEdge("a", "SUPPORTS", "b", 0.8, "s2", ("ev1", "ev2"), "scope")
    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert ("OPTIONAL MATCH (s)-[:REPORTED]->"
            "(e:ExtractionEvent {relation: $relation})-[:ABOUT]->(o)") in query
    assert params["relation"] == "SUPPORTS"


def test_graph_writer_get_edge_falls_back_to_unmigrated_arrow_properties():
    """Arrows not yet backfilled (#60) still answer through their legacy properties."""
    row = {
        "subject": "a",
        "relation": "SUPPORTS",
        "object": "b",
        "confidence": 0.5,
        "scope_conditions": "scope",
        "legacy_source_ref": "s0",
        "legacy_evidence": ["old"],
        "row_refs": [],
        "row_evidence": [],
    }
    driver = RecordingDriver(rows=[row])
    writer = Neo4jGraphWriter(driver)
    assert writer.get_edge("a", "supports", "b") == GraphEdge(
        "a", "SUPPORTS", "b", 0.5, "s0", ("old",), "scope")


def test_rejected_sink_writes_jsonl_and_creates_parent(tmp_path: Path):
    sink = RejectedRecordSink(tmp_path / "rejected.jsonl")
    sink.record_rejected({"subject": "a", "decision": "rejected", "evidence": ("e1",)})
    sink.record_rejected({"subject": "b", "decision": "rejected"})

    lines = (tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"subject": "a", "decision": "rejected", "evidence": ["e1"]},
        {"subject": "b", "decision": "rejected"},
    ]


def test_graph_writer_uses_configured_rejected_log_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PG_REJECTED_LOG_PATH", str(tmp_path / "rejected.jsonl"))
    settings = Settings.from_env()
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, rejected_log_path=settings.rejected_log_path)
    writer.record_rejected({"subject": "a", "decision": "rejected"})
    assert json.loads((tmp_path / "rejected.jsonl").read_text().splitlines()[0]) == {
        "subject": "a",
        "decision": "rejected",
    }


def test_entity_store_find_entities_uses_parameterized_cypher():
    driver = RecordingDriver()
    store = Neo4jEntityStore(driver)
    store.find_entities("Marie Curie", "Person")
    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert query == "MATCH (e:Entity {name: $name, type: $type}) RETURN e AS node"
    assert params == {"name": "Marie Curie", "type": "Person"}


def test_entity_store_search_similar_uses_vector_index_with_type_and_limit():
    driver = RecordingDriver()
    store = Neo4jEntityStore(driver)
    list(store.search_similar((0.1, 0.2), "Person", limit=3))
    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert query == (
        "CALL db.index.vector.queryNodes($index, $limit, $embedding) "
        "YIELD node, score WHERE node.type = $type "
        "RETURN node AS node, score ORDER BY score DESC"
    )
    assert params == {
        "index": "entity_embedding",
        "limit": 3,
        "embedding": [0.1, 0.2],
        "type": "Person",
    }


def test_entity_store_structural_corroboration_uses_list_corroboration_and_uppercases_relation():
    captured: list[tuple[str, dict]] = []

    class _Driver:
        def session(self, database=None):
            class _Session:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *_exc):
                    return False

                def run(self_inner, query, **params):
                    captured.append((query, params))
                    return iter(())

            return _Session()

    store = Neo4jEntityStore(_Driver())
    entity = Entity("e1", "Marie Curie", "Person")
    store.structural_corroboration(entity, [("a", "supports"), ("b", "cites")])

    [(query, params)] = captured
    assert "WHERE [n.name, type(r)] IN $neighbors" in query
    assert params["name"] == "Marie Curie"
    assert params["type"] == "Person"
    assert params["neighbors"] == [["a", "SUPPORTS"], ["b", "CITES"]]
    assert params["limit"] == 2


def test_entity_store_structural_corroboration_short_circuits_on_empty_neighbors():
    driver = RecordingDriver()
    store = Neo4jEntityStore(driver)
    assert store.structural_corroboration(Entity("e1", "x", "y"), []) == []
    assert driver.sessions == []


def test_structural_corroboration_rejects_non_uppercase_relation():
    driver = RecordingDriver()
    store = Neo4jEntityStore(driver)
    with pytest.raises(ValueError, match="uppercase"):
        store.structural_corroboration(Entity("e1", "x", "y"), [("a", "not-valid")])


def test_upsert_extraction_loads_existing_rows_for_the_triple():
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "s1:c1", ("witness",), ""))
    (session,) = driver.sessions
    load_query, load_params = session.queries[0]
    assert ("MATCH (s:Entity {name: $subject})-[:REPORTED]->"
            "(e:ExtractionEvent {relation: $relation})-[:ABOUT]->"
            "(o:Entity {name: $object})") in load_query
    assert load_params == {"subject": "a", "object": "b", "relation": "SUPPORTS"}


def test_upsert_extraction_merges_row_on_identity_with_single_string_evidence():
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "doc:chunk-1",
                                       ("witness",), "when armed"))
    (session,) = driver.sessions
    merge_query, params = session.queries[1]
    assert ("MERGE (s)-[:REPORTED]->"
            "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})-[:ABOUT]->(o)") in merge_query
    assert "ON CREATE SET e.confidence = $confidence" in merge_query
    assert "e.evidence = $evidence" in merge_query
    assert "e.scope_conditions = $scope_conditions" in merge_query
    assert "e.domain = $domain" in merge_query
    # Keep-first: a matched row only touches its timestamp, never its values.
    assert merge_query.count("ON MATCH") == 1
    assert "ON MATCH SET e.updated_at = datetime()" in merge_query
    assert params["source_ref"] == "doc:chunk-1"
    assert params["evidence"] == "witness"
    assert params["confidence"] == 0.5
    assert params["scope_conditions"] == "when armed"
    assert params["domain"] == ""


def test_upsert_extraction_never_overwrites_a_matched_row():
    existing = {"source_ref": "s1:c1", "confidence": 0.6, "evidence": "first",
                "scope_conditions": "old"}
    driver = RecordingDriver(rows=[dict(existing)])
    writer = Neo4jGraphWriter(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.9, "s1:c1", ("second",), "new"))
    (session,) = driver.sessions
    queries = [query for query, _ in session.queries]
    assert not any("MERGE (s)-[:REPORTED]" in query for query in queries)
    recompute_query, recompute_params = session.queries[-1]
    assert "SET r.confidence = $aggregate_confidence" in recompute_query
    assert recompute_params["aggregate_confidence"] == 0.6


def test_upsert_extraction_recomputes_arrow_from_all_rows():
    driver = RecordingDriver(rows=[{"source_ref": "s1:c1", "confidence": 0.6,
                                    "evidence": "first", "scope_conditions": ""}])
    writer = Neo4jGraphWriter(driver)
    writer.upsert_extraction(GraphEdge("a", "supports", "b", 0.5, "s2:c2", ("second",), ""))
    (session,) = driver.sessions
    recompute_query, recompute_params = session.queries[-1]
    assert "MERGE (s)-[r:SUPPORTS]->(o)" in recompute_query
    assert "SET r.confidence = $aggregate_confidence" in recompute_query
    assert "r.scope_conditions = CASE WHEN $scope_conditions = ''" in recompute_query
    assert recompute_params["aggregate_confidence"] == 0.8


def test_load_existing_edges_only_fetches_named_triples():
    rows_by_triple: dict[tuple[str, str, str], dict | None] = {
        ("a", "SUPPORTS", "b"): _edge_row(
            GraphEdge("a", "SUPPORTS", "b", 0.7, "s1", ("e1",), "scope")
        ),
        ("a", "CITES", "c"): None,
    }

    class _Driver:
        def __init__(self):
            self.queries: list[tuple[str, dict]] = []

        def session(self, database=None):
            outer = self

            class _Session:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *_exc):
                    return False

                def run(self_inner, query, **params):
                    outer.queries.append((query, params))
                    relation = _cypher_relation(query)
                    key = (params["subject"], relation, params["object"])
                    row = rows_by_triple.get(key)
                    return RecordingResult([] if row is None else [row])



            return _Session()

    driver = _Driver()
    result = load_existing_edges(
        driver, [("a", "supports", "b"), ("a", "cites", "c"), ("a", "supports", "x")]
    )

    assert [edge.subject for edge in result] == ["a"]
    assert len(driver.queries) == 3
    for query, _params in driver.queries:
        assert "MATCH (s:Entity {name: $subject})-[r:" in query


def _cypher_relation(query: str) -> str:
    import re

    match = re.search(r"\[r:([A-Z_]+)\]->", query)
    assert match is not None
    return match.group(1)


def _edge_row(edge: GraphEdge) -> dict:
    return {
        "subject": edge.subject,
        "relation": edge.relation,
        "object": edge.object,
        "confidence": edge.confidence,
        "source_ref": edge.source_ref,
        "evidence": list(edge.evidence),
        "scope_conditions": edge.scope_conditions,
    }


# ---------------------------------------------------------------------------
# Repeat-mode treatment of matched rows (issue #59).
# ---------------------------------------------------------------------------

from principle_graph.neo4j import resolve_repeat_mode as _writer_mode_validator  # noqa: E402


def test_writer_defaults_to_keep_first():
    writer = Neo4jGraphWriter(RecordingDriver())
    assert writer.repeat_mode == "keep-first"


def test_writer_rejects_invalid_repeat_mode_before_any_session_opens():
    driver = RecordingDriver()
    with pytest.raises(ValueError, match="invalid repeat mode"):
        Neo4jGraphWriter(driver, repeat_mode="overwrite")
    assert driver.sessions == []  # no write, not even a read, was attempted


def test_writer_refresh_mode_updates_the_matched_row_instead_of_merging():
    existing_row = {
        "source_ref": "doc:chunk-1", "confidence": 0.5,
        "evidence": "first evidence", "scope_conditions": "old scope",
    }
    driver = RecordingDriver(rows=[existing_row])
    writer = Neo4jGraphWriter(driver, repeat_mode="refresh")
    writer.upsert_extraction(
        GraphEdge("a", "supports", "b", 0.9, "doc:chunk-1", ("refined evidence",), "new scope")
    )
    (session,) = driver.sessions
    [load, refresh_row, arrow] = session.queries
    # Row load precedes planning; the row write is an in-place update, not a
    # create-merge; the arrow carries the refreshed aggregate.
    assert "ExtractionEvent {relation: $relation}" in load[0] and "RETURN" in load[0]
    assert "SET e.confidence = $confidence, e.evidence = $evidence" in refresh_row[0]
    assert "MERGE (s)-[:REPORTED]->" not in refresh_row[0]
    assert refresh_row[1] == {
        "subject": "a", "object": "b", "relation": "SUPPORTS",
        "source_ref": "doc:chunk-1", "confidence": 0.9,
        "evidence": "refined evidence", "scope_conditions": "new scope", "domain": "",
        "source_id": "doc",
        # ADR-0003: the extracted verb rides along on every row write.
        "raw_relation": "supports",
    }
    assert arrow[1]["aggregate_confidence"] == 0.9
    assert arrow[1]["scope_conditions"] == "new scope"
    assert len(session.queries) == 3


def test_writer_refresh_mode_still_merges_new_identities():
    driver = RecordingDriver(rows=[])
    writer = Neo4jGraphWriter(driver, repeat_mode="refresh")
    writer.upsert_extraction(
        GraphEdge("a", "supports", "b", 0.7, "doc:chunk-9", ("evidence",))
    )
    (session,) = driver.sessions
    [load, create, arrow] = session.queries
    assert "MERGE (s)-[:REPORTED]->" in create[0]
    assert create[1]["source_ref"] == "doc:chunk-9"
    assert arrow[1]["aggregate_confidence"] == 0.7


def test_writer_accepts_mode_variations_via_shared_validator():
    assert _writer_mode_validator(" Refresh ") == "refresh"


# ---------------------------------------------------------------------------
# Fan-out provenance join: Arrow→ledger hop (issue #61).
# ---------------------------------------------------------------------------


def test_edges_for_entity_matches_arrows_only_and_joins_rows_in_one_hop():
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver)
    writer.edges_for_entity("rates")
    (session,) = driver.sessions
    [(query, params)] = session.queries
    # Hot path: arrows matched directly; the ledger is a single OPTIONAL hop.
    assert query.startswith("MATCH (a:Entity)-[r]->(b:Entity) ")
    assert "WHERE a.name = $name OR b.name = $name" in query
    assert ("OPTIONAL MATCH (a)-[:REPORTED]->"
            "(e:ExtractionEvent {relation: type(r)})-[:ABOUT]->(b)") in query
    assert "ORDER BY e.created_at" in query
    assert params == {"name": "rates"}


def test_edges_for_entity_sources_provenance_from_rows_newest_last():
    row = {
        "subject": "rates", "relation": "MAY_DESCRIBE", "object": "target",
        "confidence": 0.95, "scope_conditions": "loose usage",
        "legacy_source_ref": None, "legacy_evidence": None,
        "row_refs": ["doc:chunk-1", "doc:chunk-2"],
        "row_evidence": ["first claim", "second claim"],
    }
    driver = RecordingDriver(rows=[row])
    edge = Neo4jGraphWriter(driver).edges_for_entity("rates")[0]
    assert edge == GraphEdge("rates", "MAY_DESCRIBE", "target", 0.95,
                             "doc:chunk-2", ("first claim", "second claim"), "loose usage")


def test_edges_for_entity_falls_back_to_legacy_arrow_properties_when_unmigrated():
    row = {
        "subject": "other", "relation": "HEDGES", "object": "rates",
        "confidence": 0.9, "scope_conditions": "crisis only",
        "legacy_source_ref": "book:2", "legacy_evidence": ["hedge claim"],
        "row_refs": [None], "row_evidence": [None],
    }
    driver = RecordingDriver(rows=[row])
    edge = Neo4jGraphWriter(driver).edges_for_entity("rates")[0]
    assert edge.source_ref == "book:2"
    assert edge.evidence == ("hedge claim",)


def test_edges_for_entity_joins_provenance_per_arrow_in_mixed_pairs():
    # Two arrows on one pair, different relations: the migrated one sources
    # rows, the unmigrated one keeps its legacy props — no cross-contamination
    # (the failure mode flagged as follow-up #70 for migrate_ledger).
    rows = [
        {
            "subject": "rates", "relation": "MAY_DESCRIBE", "object": "target",
            "confidence": 0.95, "scope_conditions": "loose",
            "legacy_source_ref": "stale:ref", "legacy_evidence": ["stale"],
            "row_refs": ["doc:chunk-2"], "row_evidence": ["second claim"],
        },
        {
            "subject": "target", "relation": "ANCHORS", "object": "rates",
            "confidence": 0.4, "scope_conditions": "narrow",
            "legacy_source_ref": "book:7", "legacy_evidence": ["anchor claim"],
            "row_refs": [None], "row_evidence": [None],
        },
    ]
    driver = RecordingDriver(rows=rows)
    edges = Neo4jGraphWriter(driver).edges_for_entity("rates")
    assert [(e.relation, e.source_ref, e.evidence) for e in edges] == [
        ("MAY_DESCRIBE", "doc:chunk-2", ("second claim",)),
        ("ANCHORS", "book:7", ("anchor claim",)),
    ]
