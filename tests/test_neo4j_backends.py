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
    assert params == {"name": "Marie Curie", "type": "Person",
                      "embedding": [0.1, 0.2], "aliases": []}


def test_graph_writer_canonicalizes_entity_type_and_persists_aliases():
    from principle_graph.label_registry import LabelEntry, LabelRegistry
    registry = LabelRegistry(1, {"person": LabelEntry("person", ("politician",), None, "")})
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, database="neo4j", entity_registry=registry)

    writer.upsert_entity(GraphEntity("Friedrich Merz", "politician",
                                     aliases=("Merz",)))

    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert params["type"] == "person"  # same canonical key the resolver matched on
    assert params["aliases"] == ["Merz"]
    assert "e.aliases = CASE WHEN $aliases = []" in query
    assert writer.unknown_entity_type_counts == {}


def test_graph_writer_flags_unknown_entity_type_at_write_boundary():
    from principle_graph.label_registry import LabelEntry, LabelRegistry
    registry = LabelRegistry(1, {"person": LabelEntry("person", (), None, "")})
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, database="neo4j", entity_registry=registry)

    writer.upsert_entity(GraphEntity("Obscurity", "xenosophy"))

    (session,) = driver.sessions
    [(_, params)] = session.queries
    assert params["type"] == "xenosophy"  # pass-through flagged, never rejected
    assert writer.unknown_entity_type_counts == {"xenosophy": 1}


def test_entity_store_containment_prefilter_and_alias_append():
    driver = RecordingDriver(rows=[{"node": {"name": "Friedrich Merz",
                                             "type": "person",
                                             "aliases": []}}])
    store = Neo4jEntityStore(driver, database="neo4j")

    candidates = store.containment_candidates("Merz", "person")
    assert [c.name for c in candidates] == ["Friedrich Merz"]
    store.add_alias(Entity("e1", "Friedrich Merz", "person"), "Merz")

    prefilter_query, prefilter_params = driver.sessions[0].queries[0]
    assert "MATCH (e:Entity {type: $type})" in prefilter_query
    assert prefilter_params["tokens"] == ["merz"]
    alias_query, alias_params = driver.sessions[1].queries[0]
    assert "NOT a IN coalesce(e.aliases, [])" in alias_query
    assert alias_params == {"name": "Friedrich Merz", "type": "person", "alias": "Merz"}


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
    assert query == ("MATCH (e:Entity {type: $type}) "
                     "WHERE e.name = $name OR $name IN coalesce(e.aliases, []) "
                     "RETURN e AS node")
    assert params == {"name": "Marie Curie", "type": "Person"}


def test_entity_store_list_relation_types_returns_distinct_sorted_types():
    driver = RecordingDriver([{"relType": "REDUCES"}, {"relType": "BOOSTS"}])
    store = Neo4jEntityStore(driver)
    assert store.list_relation_types() == ("REDUCES", "BOOSTS")
    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert query == "MATCH ()-[r]->() RETURN DISTINCT type(r) AS relType ORDER BY relType"
    assert params == {}


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
        # Evidence-search slice: no embedder wired, so the param is None and the
        # CASE guard leaves any stored vector untouched.
        "evidence_embedding": None,
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


# --- Domain tagging at the write boundary (issue #78) ---

def _merge_params(edge, **writer_kwargs):
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, **writer_kwargs)
    writer.upsert_extraction(edge)
    (session,) = driver.sessions
    merge_query, params = session.queries[1]
    assert "e.domain = $domain" in merge_query
    return writer, params


def test_writer_persists_untagged_domain_by_default():
    writer, params = _merge_params(
        GraphEdge("a", "supports", "b", 0.5, "s1:c1", ("w",), ""))
    assert params["domain"] == ""
    assert writer.unknown_domain_counts == {}


def test_writer_canonicalizes_domain_alias_before_write():
    writer, params = _merge_params(
        GraphEdge("a", "supports", "b", 0.5, "s1:c1", ("w",), "",
                  domain="macroeconomics"))
    assert params["domain"] == "economics"
    assert writer.unknown_domain_counts == {}


def test_unknown_domain_passes_through_flagged():
    edge = GraphEdge("a", "supports", "b", 0.5, "s1:c1", ("w",), "",
                     domain="xenosophy")
    writer, params = _merge_params(edge)
    assert params["domain"] == "xenosophy"
    writer.upsert_extraction(GraphEdge(
        "c", "supports", "d", 0.5, "s2:c1", ("w",), "", domain="xenosophy"))
    assert writer.unknown_domain_counts == {"xenosophy": 2}


def test_entity_store_find_entities_matches_persisted_aliases():
    """P1 fix: the name-keyed read path consults persisted aliases."""
    driver = RecordingDriver()
    store = Neo4jEntityStore(driver)
    store.find_entities("Merz", "person")
    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert query == ("MATCH (e:Entity {type: $type}) "
                     "WHERE e.name = $name OR $name IN coalesce(e.aliases, []) "
                     "RETURN e AS node")
    assert params == {"name": "Merz", "type": "person"}


def test_entity_store_containment_uses_token_list_not_string_param():
    """P0 fix: containment prefilter compares token lists; never a string param."""
    driver = RecordingDriver(rows=[{"node": {"name": "Friedrich Merz",
                                             "type": "person",
                                             "aliases": ["Merz"]}}])
    store = Neo4jEntityStore(driver)
    store.containment_candidates("Merz", "person")
    (session,) = driver.sessions
    [(query, params)] = session.queries
    assert "$name" not in query
    assert "coalesce(e.aliases, [])" in query
    assert params == {"type": "person", "tokens": ["merz"]}


# --- state ledger write path (issue #98, PR #108 fix round) ------------------

class _StateRow(dict):
    """Row dict with tolerant .get, mirroring the real driver's Record."""
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _state_driver(rows):
    class _Session(RecordingSession):
        def run(self, query, **params):
            self.queries.append((query, params))
            if "HAS_STATE_EVENT" in query and "RETURN" in query:
                return RecordingResult(rows)
            return RecordingResult([])

    class _Driver(RecordingDriver):
        def session(self, database=None):
            session = _Session(self._rows)
            self.sessions.append(session)
            return session

    return _Driver(rows)


def _state_event(**overrides):
    from principle_graph.state import StateEvent
    values = dict(
        entity="Friedrich Merz", entity_type="person", state_key="approval_rating",
        value="42", unit="percent", as_of="2026-09-01", confidence=0.9,
        evidence="polls", scope_conditions="", source_ref="note-1:chunk-1",
    )
    values.update(overrides)
    return StateEvent(**values)


def test_upsert_state_event_loads_all_sibling_rows_before_map_recompute():
    """P1-1: sibling state keys must survive the denormalized map recompute."""
    rows = [
        _StateRow(state_key="approval_rating", entity_type="person",
                  value="42", unit="percent", as_of="2026-09-01",
                  confidence=0.9, evidence="polls", scope_conditions="",
                  source_ref="note-1:chunk-1"),
        _StateRow(state_key="yield_level", entity_type="person",
                  value="elevated", unit="", as_of="2026-09-02",
                  confidence=0.8, evidence="bund", scope_conditions="",
                  source_ref="note-1:chunk-2"),
    ]
    driver = _state_driver(rows)
    writer = Neo4jGraphWriter(driver)

    writer.upsert_state_event(_state_event(state_key="approval_rating",
                                           as_of="2026-09-03"))

    (session,) = driver.sessions
    load = [(q, p) for q, p in session.queries
            if "HAS_STATE_EVENT" in q and "RETURN" in q]
    [(load_query, load_params)] = load
    # Load is unfiltered by state_key: planning sees every sibling row.
    assert "{state_key" not in load_query
    assert load_params == {"entity": "Friedrich Merz", "entity_type": "person"}
    map_set = [(q, p) for q, p in session.queries if "SET e.state" in q]
    [(query, params)] = map_set
    entries = json.loads(params["state"])
    assert set(entries) == {"approval_rating", "yield_level"}


def test_upsert_state_event_canonicalizes_entity_type_at_boundary():
    """P1-2: a registry-alias type never fragments the state entity."""
    from principle_graph.label_registry import LabelEntry, LabelRegistry
    registry = LabelRegistry(1, {"person": LabelEntry("person", ("politician",), None, "")})
    driver = _state_driver([])
    writer = Neo4jGraphWriter(driver, entity_registry=registry)

    writer.upsert_state_event(_state_event(entity_type="politician"))

    (session,) = driver.sessions
    merge = [(q, p) for q, p in session.queries if "HAS_STATE_EVENT" in q and "MERGE" in q]
    [(query, params)] = merge
    assert params["entity_type"] == "person"
    assert "{name: $entity, type: $entity_type}" in query


class _StaticEmbedder:
    """Evidence-search slice: fixed-vector embedder with a call counter."""

    def __init__(self, vector: list[float]):
        self._vector = vector
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        return self._vector


def test_writer_embeds_evidence_on_extraction_row_create():
    # Evidence-search slice: an evidence embedder wired at construction rides
    # along on row writes; identity-planning is unaffected (plan sees text only).
    driver = RecordingDriver()
    embedder = _StaticEmbedder([0.1, 0.2])
    writer = Neo4jGraphWriter(driver, database="neo4j", evidence_embedder=embedder)
    writer.upsert_extraction(
        GraphEdge("a", "supports", "b", 0.9, "doc:chunk-1", ("some evidence",))
    )
    [load, merge_row, arrow] = driver.sessions[0].queries
    assert "evidence_embedding = CASE WHEN $evidence_embedding IS NULL" in merge_row[0]
    assert merge_row[1]["evidence_embedding"] == [0.1, 0.2]
    assert embedder.calls == 1


def test_writer_keep_first_merge_never_erases_existing_embedding():
    # The CASE guard: with no embedder wired, the param is None and a stored
    # vector survives a re-ingest (keep-first extends to the embedding).
    driver = RecordingDriver()
    writer = Neo4jGraphWriter(driver, database="neo4j")
    writer.upsert_extraction(
        GraphEdge("a", "supports", "b", 0.9, "doc:chunk-1", ("some evidence",))
    )
    [_, merge_row, _arrow] = driver.sessions[0].queries
    assert merge_row[1]["evidence_embedding"] is None
    assert "THEN e.evidence_embedding ELSE $evidence_embedding END" in merge_row[0]


def test_search_evidence_returns_hits_with_provenance():
    driver = RecordingDriver(rows=[{
        "subject": "a", "relation": "SUPPORTS", "object": "b",
        "evidence": "text", "confidence": 0.9,
        "source_ref": "doc:chunk-1", "score": 0.87,
    }])
    writer = Neo4jGraphWriter(driver, database="neo4j")
    (hit,) = writer.search_evidence([0.5, 0.5], limit=3)
    [(_, params)] = driver.sessions[0].queries
    assert params["index"] == "extraction_evidence_embedding"
    assert params["embedding"] == [0.5, 0.5]
    assert hit.subject == "a" and hit.relation == "SUPPORTS" and hit.object == "b"
    assert hit.score == 0.87 and hit.source_ref == "doc:chunk-1"
