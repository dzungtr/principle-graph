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


def test_graph_writer_get_edge_returns_graph_edge_with_evidence():
    row = {
        "subject": "a",
        "relation": "SUPPORTS",
        "object": "b",
        "confidence": 0.6,
        "source_ref": "s1",
        "evidence": ["ev1", "ev2"],
        "scope_conditions": "scope",
    }
    driver = RecordingDriver(rows=[row])
    writer = Neo4jGraphWriter(driver)
    edge = writer.get_edge("a", "supports", "b")
    assert edge == GraphEdge("a", "SUPPORTS", "b", 0.6, "s1", ("ev1", "ev2"), "scope")


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
