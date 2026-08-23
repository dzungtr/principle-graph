"""Neo4j persistence adapters for the principle graph."""
from __future__ import annotations
from dataclasses import asdict, is_dataclass
import json, os, re
from pathlib import Path
from typing import Any, Iterable, Sequence
from .resolution import Entity, EntityStore, SimilarEntity, normalize_name
from .review import GraphEdge, GraphEntity
_RELATION = re.compile(r"^[A-Z][A-Z0-9_]*$")
def _relation(value: str) -> str:
    result = value.upper()
    if not _RELATION.fullmatch(result): raise ValueError("relation must be an uppercase Neo4j identifier")
    return result
def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict): return record.get(key, default)
    try: return record[key]
    except (KeyError, IndexError, TypeError): return default
def _entity(record: Any) -> Entity:
    node = _value(record, "node", record)
    get = lambda k, d=None: _value(node, k, d)
    embedding = get("embedding")
    return Entity(str(get("id", get("name", ""))), str(get("name", "")), str(get("type", "")), tuple(get("aliases", ()) or ()), tuple(embedding) if embedding is not None else None)
def _edge(record: Any) -> GraphEdge:
    rel = _value(record, "rel", record)
    get = lambda k, d=None: _value(rel, k, _value(record, k, d))
    return GraphEdge(str(_value(record, "subject", _value(record, "start_name", ""))), str(get("relation", get("type", ""))).upper(), str(_value(record, "object", _value(record, "end_name", ""))), float(get("confidence", 0.0)), str(get("source_ref", "") or ""), tuple(get("evidence", ()) or ()), str(get("scope_conditions", "") or ""))
class _Neo4j:
    def __init__(self, driver: Any, database: str = "neo4j") -> None: self.driver, self.database = driver, database
    def _run(self, query: str, **params: Any) -> list[Any]:
        with self.driver.session(database=self.database) as session: return list(session.run(query, **params))
class Neo4jGraphWriter(_Neo4j):
    def upsert_entity(self, entity: GraphEntity | Entity) -> None:
        name = entity.name
        entity_type = entity.entity_type if isinstance(entity, GraphEntity) else entity.type
        embedding = getattr(entity, "embedding", None)
        self._run("""MERGE (e:Entity {name: $name, type: $entity_type})
ON CREATE SET e.created_at = datetime(), e.updated_at = datetime(), e.embedding = $embedding
ON MATCH SET e.updated_at = datetime(), e.embedding = coalesce($embedding, e.embedding)
RETURN e""", name=name, entity_type=entity_type, embedding=list(embedding) if embedding is not None else None)
    def upsert_edge(self, edge: GraphEdge) -> None:
        relation = _relation(edge.relation)
        self._run("""MATCH (s:Entity {name: $subject}), (o:Entity {name: $object})
MERGE (s)-[r:$(relation)]->(o)
ON CREATE SET r.confidence = $confidence, r.evidence = $evidence, r.scope_conditions = $scope_conditions, r.source_ref = $source_ref, r.created_at = datetime(), r.updated_at = datetime()
ON MATCH SET r.confidence = $confidence, r.evidence = coalesce(r.evidence, []) + [x IN $evidence WHERE NOT x IN coalesce(r.evidence, [])], r.scope_conditions = CASE WHEN $scope_conditions <> '' THEN $scope_conditions ELSE r.scope_conditions END, r.source_ref = CASE WHEN $source_ref <> '' THEN $source_ref ELSE r.source_ref END, r.updated_at = datetime()
RETURN r""", subject=edge.subject, object=edge.object, relation=relation, confidence=max(0.0, min(1.0, edge.confidence)), evidence=list(edge.evidence), scope_conditions=edge.scope_conditions, source_ref=edge.source_ref)
    def get_edge(self, subject: str, relation: str, object_: str) -> GraphEdge | None:
        rows = self._run("""MATCH (s:Entity {name: $subject})-[r]-(o:Entity {name: $object}) WHERE type(r) = $relation RETURN s.name AS subject, type(r) AS relation, o.name AS object, r AS rel""", subject=subject, object=object_, relation=_relation(relation))
        return _edge(rows[0]) if rows else None
    def record_rejected(self, record: dict[str, object]) -> None:
        path = Path(os.getenv("PG_REJECTED_LOG", os.getenv("PG_REJECTED_PATH", ".pg/rejected.jsonl"))); path.parent.mkdir(parents=True, exist_ok=True)
        def encode(value: Any) -> Any:
            if is_dataclass(value): return asdict(value)
            if isinstance(value, tuple): return list(value)
            raise TypeError(f"not JSON serializable: {type(value).__name__}")
        with path.open("a", encoding="utf-8") as stream: stream.write(json.dumps(record, default=encode, ensure_ascii=False) + "\n")
class Neo4jResolutionStore(_Neo4j, EntityStore):
    def find_entities(self, name: str, entity_type: str) -> Sequence[Entity]:
        rows = self._run("MATCH (e:Entity) WHERE e.type = $entity_type AND toLower(trim(e.name)) = $normalized RETURN e AS node", entity_type=entity_type, normalized=normalize_name(name))
        return [_entity(row) for row in rows]
    def search_similar(self, embedding: Sequence[float], entity_type: str, limit: int = 10) -> Sequence[SimilarEntity]:
        rows = self._run("CALL db.index.vector.queryNodes('entity_embedding', $limit, $embedding) YIELD node, score WHERE node.type = $entity_type RETURN node, score ORDER BY score DESC", embedding=list(embedding), entity_type=entity_type, limit=limit)
        return [SimilarEntity(_entity(row), float(_value(row, "score", 0.0))) for row in rows]
    def structural_corroboration(self, entity: Entity, neighbors: Sequence[tuple[str, str]]) -> Sequence[dict[str, Any]]:
        if not neighbors: return ()
        rows = self._run("""MATCH (e:Entity {name: $name})-[r]-(n:Entity)
WHERE (n.name, type(r), CASE WHEN startNode(r) = e THEN 'out' ELSE 'in' END) IN $neighbors
RETURN n.name AS neighbor, type(r) AS relation, CASE WHEN startNode(r) = e THEN 'out' ELSE 'in' END AS direction LIMIT $limit""", name=entity.name, neighbors=[(n, _relation(rel), direction) for n, rel in neighbors], limit=min(len(neighbors), 20))
        return [dict(row) if hasattr(row, "keys") else row for row in rows]
class Neo4jEdgeLoader(_Neo4j):
    def load_edges(self, candidates: Iterable[tuple[str, str, str]]) -> Sequence[GraphEdge]:
        triples = [(s, _relation(r), o) for s, r, o in candidates]
        if not triples: return ()
        rows = self._run("UNWIND $triples AS candidate MATCH (s:Entity {name: candidate[0]})-[r]->(o:Entity {name: candidate[2]}) WHERE type(r) = candidate[1] RETURN s.name AS subject, type(r) AS relation, o.name AS object, r AS rel", triples=triples)
        return [_edge(row) for row in rows]
    def load(self, candidates: Iterable[tuple[str, str, str]]) -> Sequence[GraphEdge]: return self.load_edges(candidates)
Neo4jWriter = Neo4jGraphWriter
Neo4jEntityStore = Neo4jResolutionStore
ExistingEdgeLoader = Neo4jEdgeLoader
