"""Neo4j persistence adapters for graph and resolution seams."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .reduction import GraphEdge, GraphEntity
from .resolution import Entity, SimilarEntity

_RELATION = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _relation(value: str) -> str:
    value = value.upper()
    if not _RELATION.fullmatch(value):
        raise ValueError("relationship type must be an uppercase identifier")
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class RejectedRecordSink:
    """Append rejected review records to a local JSONL log (parent dir created on demand)."""

    def __init__(self, path: str | Path = ".pg/rejected.jsonl") -> None:
        self.path = Path(path)

    def record_rejected(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=_json_default) + "\n")


class Neo4jGraphWriter:
    """GraphWriter seam implementation: entity/edge upserts, get-edge, rejected records."""

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        rejected_log_path: str | Path = ".pg/rejected.jsonl",
    ) -> None:
        self.driver = driver
        self.database = database
        self.rejected_sink = RejectedRecordSink(rejected_log_path)

    def upsert_entity(self, entity: GraphEntity) -> None:
        query = (
            "MERGE (e:Entity {name: $name, type: $type}) "
            "ON CREATE SET e.created_at = datetime(), e.updated_at = datetime() "
            "ON MATCH SET e.updated_at = datetime() "
            "SET e.embedding = CASE WHEN $embedding IS NULL THEN e.embedding ELSE $embedding END"
        )
        embedding = getattr(entity, "embedding", None)
        if embedding is not None:
            embedding = list(embedding)
        with self.driver.session(database=self.database) as session:
            session.run(
                query,
                name=entity.name,
                type=entity.entity_type,
                embedding=embedding,
            )

    def upsert_edge(self, edge: GraphEdge) -> None:
        relation = _relation(edge.relation)
        query = (
            f"MATCH (s:Entity {{name: $subject}}), (o:Entity {{name: $object}}) "
            f"MERGE (s)-[r:{relation}]->(o) "
            "ON CREATE SET r.created_at = datetime() "
            "SET r.confidence = $confidence, r.evidence = $evidence, "
            "r.scope_conditions = $scope_conditions, r.source_ref = $source_ref, "
            "r.updated_at = datetime()"
        )
        with self.driver.session(database=self.database) as session:
            session.run(
                query,
                subject=edge.subject,
                object=edge.object,
                confidence=edge.confidence,
                evidence=list(edge.evidence),
                scope_conditions=edge.scope_conditions,
                source_ref=edge.source_ref,
            )

    def get_edge(self, subject: str, relation: str, object_: str) -> GraphEdge | None:
        relation = _relation(relation)
        query = (
            f"MATCH (s:Entity {{name: $subject}})-[r:{relation}]->(o:Entity {{name: $object}}) "
            "RETURN s.name AS subject, type(r) AS relation, o.name AS object, "
            "r.confidence AS confidence, r.source_ref AS source_ref, r.evidence AS evidence, "
            "r.scope_conditions AS scope_conditions"
        )
        with self.driver.session(database=self.database) as session:
            record = session.run(query, subject=subject, object=object_).single()
        if record is None:
            return None
        get = (
            record.get
            if hasattr(record, "get")
            else (lambda key, default=None: record[key] if key in record else default)
        )
        return GraphEdge(
            get("subject"),
            get("relation"),
            get("object"),
            get("confidence"),
            get("source_ref", ""),
            tuple(get("evidence", []) or []),
            get("scope_conditions", "") or "",
        )

    def record_rejected(self, record: dict[str, object]) -> None:
        self.rejected_sink.record_rejected(record)


class Neo4jEntityStore:
    """Resolution store backed by Neo4j: alias lookup, vector search, structural corroboration."""

    VECTOR_INDEX = "entity_embedding"

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def find_entities(self, name: str, entity_type: str) -> Sequence[Entity]:
        query = "MATCH (e:Entity {name: $name, type: $type}) RETURN e AS node"
        with self.driver.session(database=self.database) as session:
            return [_entity(row["node"]) for row in session.run(query, name=name, type=entity_type)]

    def search_similar(
        self, embedding: Sequence[float], entity_type: str, limit: int = 10
    ) -> Sequence[SimilarEntity]:
        query = (
            "CALL db.index.vector.queryNodes($index, $limit, $embedding) "
            "YIELD node, score WHERE node.type = $type "
            "RETURN node AS node, score ORDER BY score DESC"
        )
        with self.driver.session(database=self.database) as session:
            return [
                SimilarEntity(_entity(row["node"]), row["score"])
                for row in session.run(
                    query,
                    index=self.VECTOR_INDEX,
                    limit=limit,
                    embedding=list(embedding),
                    type=entity_type,
                )
            ]

    def structural_corroboration(
        self, entity: Entity, neighbors: Sequence[tuple[str, str]]
    ) -> list[dict[str, str]]:
        if not neighbors:
            return []
        query = (
            "MATCH (e:Entity {name: $name, type: $type}) MATCH (e)-[r]-(n:Entity) "
            "WHERE [n.name, type(r)] IN $neighbors "
            "RETURN n.name AS name, type(r) AS relation LIMIT $limit"
        )
        expected = [[name, _relation(relation)] for name, relation in neighbors]
        with self.driver.session(database=self.database) as session:
            return [
                {"name": row["name"], "relation": row["relation"]}
                for row in session.run(
                    query,
                    name=entity.name,
                    type=entity.type,
                    neighbors=expected,
                    limit=len(expected),
                )
            ]


def _entity(node: Any) -> Entity:
    get = (
        node.get
        if hasattr(node, "get")
        else (lambda key, default=None: getattr(node, key, default))
    )
    embedding = get("embedding")
    return Entity(
        str(get("name")),
        get("name"),
        get("type"),
        tuple(get("aliases", []) or []),
        tuple(embedding) if embedding is not None else None,
    )


def load_existing_edges(
    driver: Any,
    triples: Iterable[tuple[str, str, str]],
    database: str = "neo4j",
) -> list[GraphEdge]:
    """Look up only the requested triples — no full-graph scan."""
    writer = Neo4jGraphWriter(driver, database)
    return [edge for triple in triples if (edge := writer.get_edge(*triple)) is not None]
