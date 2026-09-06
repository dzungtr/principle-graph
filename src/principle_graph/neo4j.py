"""Neo4j persistence adapters for graph and resolution seams."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .ledger import LedgerRow, plan_ledger_writes, resolve_repeat_mode
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


def assemble_provenance(
    row_refs: Sequence[str | None] | None,
    row_evidence: Sequence[str | None] | None,
    legacy_source_ref: str | None,
    legacy_evidence: Sequence[str] | None,
) -> tuple[str, tuple[str, ...]]:
    """Arrow→ledger provenance join shared by every read path (issue #61).

    Rows win: the newest row's source reference and the collected row evidence
    strings (created_at order). Legacy arrow properties answer only while an
    arrow is unmigrated (no rows); the #60 backfill removes them, so this
    fallback is the migration-safe bridge, not a second source of truth.
    """
    refs = [ref for ref in (row_refs or []) if ref]
    evidence = [item for item in (row_evidence or []) if item]
    source_ref = refs[-1] if refs else (legacy_source_ref or "")
    return source_ref, tuple(evidence) or tuple(legacy_evidence or [])


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
        repeat_mode: str = "keep-first",
    ) -> None:
        self.driver = driver
        self.database = database
        self.rejected_sink = RejectedRecordSink(rejected_log_path)
        # Invalid modes raise here, before any session opens (issue #59 AC 2).
        self.repeat_mode = resolve_repeat_mode(repeat_mode)

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

    _ROW_LOAD_QUERY = (
        "MATCH (s:Entity {name: $subject})-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation})-[:ABOUT]->(o:Entity {name: $object}) "
        "RETURN e.source_ref AS source_ref, e.confidence AS confidence, "
        "e.evidence AS evidence, e.scope_conditions AS scope_conditions "
        "ORDER BY e.created_at"
    )
    # Identity-bearing write shape (ADR-0002, Lesson 0003 Task B): the pattern
    # MERGE enforces (triple, source_ref) identity where Community 5.x cannot.
    _ROW_MERGE_QUERY = (
        "MATCH (s:Entity {name: $subject}), (o:Entity {name: $object}) "
        "MERGE (s)-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})-[:ABOUT]->(o) "
        "ON CREATE SET e.confidence = $confidence, e.evidence = $evidence, "
        "e.scope_conditions = $scope_conditions, e.domain = $domain, "
        "e.created_at = datetime(), e.updated_at = datetime() "
        "ON MATCH SET e.updated_at = datetime()"
    )
    # Refresh mode (issue #59): the matched row's values are replaced in place;
    # created_at is preserved so get_edge's row ordering stays stable.
    _ROW_REFRESH_QUERY = (
        "MATCH (s:Entity {name: $subject})-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})-[:ABOUT]->"
        "(o:Entity {name: $object}) "
        "SET e.confidence = $confidence, e.evidence = $evidence, "
        "e.scope_conditions = $scope_conditions, e.domain = $domain, "
        "e.updated_at = datetime()"
    )

    def upsert_extraction(self, edge: GraphEdge) -> None:
        """Append one accepted extraction as a ledger row and recompute the arrow.

        The plan comes from the pure ledger module under this writer's repeat
        mode (keep-first default; refresh replaces matched rows); this adapter
        only executes it. The arrow's derived aggregate is recomputed from all
        rows for the triple on every call — never set independently.
        """
        relation = _relation(edge.relation)
        candidate = LedgerRow(
            edge.subject, relation, edge.object, edge.source_ref, edge.confidence,
            edge.evidence[0] if edge.evidence else "", edge.scope_conditions,
        )
        with self.driver.session(database=self.database) as session:
            existing = [
                LedgerRow(candidate.subject, relation, candidate.object,
                          record["source_ref"] or "", record["confidence"] or 0.0,
                          record["evidence"] or "", record["scope_conditions"] or "")
                for record in session.run(
                    self._ROW_LOAD_QUERY,
                    subject=candidate.subject,
                    object=candidate.object,
                    relation=relation,
                )
            ]
            plan = plan_ledger_writes(existing, [candidate], mode=self.repeat_mode)
            for row in plan.rows_to_create:
                session.run(
                    self._ROW_MERGE_QUERY,
                    subject=row.subject,
                    object=row.object,
                    relation=row.relation,
                    source_ref=row.source_ref,
                    confidence=row.confidence,
                    evidence=row.evidence,
                    scope_conditions=row.scope_conditions,
                    domain=row.domain,
                )
            for row in plan.rows_to_update:
                session.run(
                    self._ROW_REFRESH_QUERY,
                    subject=row.subject,
                    object=row.object,
                    relation=row.relation,
                    source_ref=row.source_ref,
                    confidence=row.confidence,
                    evidence=row.evidence,
                    scope_conditions=row.scope_conditions,
                    domain=row.domain,
                )
            for update in plan.arrow_updates:
                query = (
                    f"MATCH (s:Entity {{name: $subject}}), (o:Entity {{name: $object}}) "
                    f"MERGE (s)-[r:{update.relation}]->(o) "
                    "ON CREATE SET r.created_at = datetime() "
                    "SET r.confidence = $aggregate_confidence, "
                    "r.scope_conditions = CASE WHEN $scope_conditions = '' "
                    "THEN r.scope_conditions ELSE $scope_conditions END, "
                    "r.updated_at = datetime()"
                )
                session.run(
                    query,
                    subject=update.subject,
                    object=update.object,
                    aggregate_confidence=update.aggregate_confidence,
                    scope_conditions=update.scope_conditions,
                )

    # --- backfill migration (issue #60) -----------------------------------
    # The EXISTS patterns are pinned to the arrow's own relation (issue #70):
    # a pair may carry several relation types, and a crash between batches can
    # leave one migrated and its sibling not — matching on the (subject, object)
    # pair alone would then skip the un-migrated arrow and strip its legacy
    # provenance before any ledger row exists for it.
    _BACKFILL_LOAD_QUERY = (
        "MATCH (s:Entity)-[r]->(o:Entity) "
        "WHERE type(r) <> 'REPORTED' "
        "AND NOT EXISTS { (s)-[:REPORTED]->(:ExtractionEvent {relation: type(r)})-[:ABOUT]->(o) } "
        "RETURN s.name AS subject, type(r) AS relation, o.name AS object, "
        "r.confidence AS confidence, r.evidence AS evidence, "
        "r.scope_conditions AS scope_conditions, r.source_ref AS source_ref"
    )
    _BACKFILL_ROWS_LOAD_QUERY = (
        "MATCH (s:Entity)-[:REPORTED]->(e:ExtractionEvent)-[:ABOUT]->(o:Entity) "
        "RETURN s.name AS subject, e.relation AS relation, o.name AS object, "
        "e.source_ref AS source_ref, e.confidence AS confidence, "
        "e.evidence AS evidence, e.scope_conditions AS scope_conditions "
        "ORDER BY e.created_at"
    )
    # No ON MATCH clause: a re-run must not touch already-migrated rows, so a
    # second migration changes no state at all (issue #60 idempotency AC).
    _BACKFILL_ROW_MERGE_QUERY = (
        "MATCH (s:Entity {name: $subject}), (o:Entity {name: $object}) "
        "MERGE (s)-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})-[:ABOUT]->(o) "
        "ON CREATE SET e.confidence = $confidence, e.evidence = $evidence, "
        "e.scope_conditions = $scope_conditions, e.domain = $domain, "
        "e.created_at = datetime(), e.updated_at = datetime()"
    )
    _BACKFILL_ARROW_CONFIDENCE_QUERY = (
        "MATCH (s:Entity {{name: $subject}})-[r:{relation}]->(o:Entity {{name: $object}}) "
        "RETURN r.confidence AS confidence"
    )
    _BACKFILL_ARROW_SET_QUERY = (
        "MATCH (s:Entity {{name: $subject}}), (o:Entity {{name: $object}}) "
        "MERGE (s)-[r:{relation}]->(o) "
        "ON CREATE SET r.created_at = datetime() "
        "SET r.confidence = $aggregate_confidence, r.updated_at = datetime()"
    )
    _BACKFILL_STRIP_CLAUSE = (
        "WHERE type(r) <> 'REPORTED' "
        "AND EXISTS { (s)-[:REPORTED]->(:ExtractionEvent {relation: type(r)})-[:ABOUT]->(o) } "
        "AND (r.evidence IS NOT NULL OR r.source_ref IS NOT NULL)"
    )
    _BACKFILL_STRIP_COUNT_QUERY = (
        "MATCH (s:Entity)-[r]->(o:Entity) " + _BACKFILL_STRIP_CLAUSE
        + " RETURN count(r) AS count"
    )
    _BACKFILL_STRIP_QUERY = (
        "MATCH (s:Entity)-[r]->(o:Entity) " + _BACKFILL_STRIP_CLAUSE
        + " REMOVE r.evidence, r.source_ref"
    )
    # A single-row complement aggregate round-trips the prior confidence to
    # within one float ulp; the epsilon guard turns that round-trip into an
    # exact no-op so migration never moves an arrow's stored value.
    _AGGREGATE_EPSILON = 1e-12

    def migrate_ledger(self) -> dict[str, int]:
        """Backfill one ledger row per existing typed edge (ADR-0002, PRD #57).

        Idempotent by ledger identity: only arrows without ledger rows are
        candidates (migrated arrows are never re-derived — the strip removes
        their provenance properties), rows merge with no ON MATCH side
        effects, arrow writes fire only when the recomputed aggregate actually
        differs beyond float epsilon, and legacy provenance stripping only
        fires while such properties remain. A second run changes no state —
        timestamps included — and arrow confidences never move: a single-row
        complement aggregate equals the prior edge confidence.
        """
        with self.driver.session(database=self.database) as session:
            candidates: list[LedgerRow] = []
            for record in session.run(self._BACKFILL_LOAD_QUERY):
                confidence = record["confidence"]
                if confidence is None:
                    raise ValueError(
                        f"edge {record['subject']}-[{record['relation']}]->"
                        f"{record['object']} has no confidence; migration "
                        "requires confident edges"
                    )
                evidence = record["evidence"] or []
                candidates.append(LedgerRow(
                    record["subject"], _relation(record["relation"]),
                    record["object"], record["source_ref"] or "",
                    float(confidence),
                    evidence[0] if evidence else "",  # row shape: one evidence string
                    record["scope_conditions"] or "",
                ))
            existing = [
                LedgerRow(
                    record["subject"], _relation(record["relation"]),
                    record["object"], record["source_ref"] or "",
                    float(record["confidence"] or 0.0),
                    record["evidence"] or "", record["scope_conditions"] or "",
                )
                for record in session.run(self._BACKFILL_ROWS_LOAD_QUERY)
            ]
            plan = plan_ledger_writes(existing, candidates)
            for row in plan.rows_to_create:
                session.run(
                    self._BACKFILL_ROW_MERGE_QUERY,
                    subject=row.subject,
                    object=row.object,
                    relation=row.relation,
                    source_ref=row.source_ref,
                    confidence=row.confidence,
                    evidence=row.evidence,
                    scope_conditions=row.scope_conditions,
                    domain=row.domain,
                ).consume()
            arrows_recomputed = 0
            for update in plan.arrow_updates:
                record = session.run(
                    self._BACKFILL_ARROW_CONFIDENCE_QUERY.format(relation=update.relation),
                    subject=update.subject,
                    object=update.object,
                ).single()
                current = record["confidence"] if record is not None else None
                if (current is not None
                        and abs(float(current) - update.aggregate_confidence)
                        <= self._AGGREGATE_EPSILON):
                    continue
                session.run(
                    self._BACKFILL_ARROW_SET_QUERY.format(relation=update.relation),
                    subject=update.subject,
                    object=update.object,
                    aggregate_confidence=update.aggregate_confidence,
                ).consume()
                arrows_recomputed += 1
            stripped_record = session.run(self._BACKFILL_STRIP_COUNT_QUERY).single()
            stripped = stripped_record["count"] if stripped_record is not None else 0
            if stripped:
                session.run(self._BACKFILL_STRIP_QUERY).consume()
            return {
                "edges_seen": len(candidates),
                "rows_created": len(plan.rows_to_create),
                "rows_skipped": len(plan.skipped_identities),
                "arrows_recomputed": arrows_recomputed,
                "arrows_unchanged": len(plan.arrow_updates) - arrows_recomputed,
                "legacy_props_stripped": stripped,
            }

    def get_edge(self, subject: str, relation: str, object_: str) -> GraphEdge | None:
        relation = _relation(relation)
        query = (
            f"MATCH (s:Entity {{name: $subject}})-[r:{relation}]->(o:Entity {{name: $object}}) "
            "OPTIONAL MATCH (s)-[:REPORTED]->(e:ExtractionEvent {relation: $relation})-[:ABOUT]->(o) "
            "WITH s, r, o, e ORDER BY e.created_at "
            "RETURN s.name AS subject, type(r) AS relation, o.name AS object, "
            "r.confidence AS confidence, r.scope_conditions AS scope_conditions, "
            "r.source_ref AS legacy_source_ref, r.evidence AS legacy_evidence, "
            "collect(e.source_ref) AS row_refs, collect(e.evidence) AS row_evidence"
        )
        with self.driver.session(database=self.database) as session:
            record = session.run(
                query, subject=subject, object=object_, relation=relation
            ).single()
        if record is None:
            return None
        get = (
            record.get
            if hasattr(record, "get")
            else (lambda key, default=None: record[key] if key in record else default)
        )
        row_refs = get("row_refs")
        row_evidence = get("row_evidence")
        source_ref, evidence = assemble_provenance(
            row_refs, row_evidence, get("legacy_source_ref"), get("legacy_evidence")
        )
        return GraphEdge(
            get("subject"),
            get("relation"),
            get("object"),
            get("confidence"),
            source_ref,
            evidence,
            get("scope_conditions", "") or "",
        )

    def edges_for_entity(self, name: str) -> list[GraphEdge]:
        """All arrows touching *name* for the fan-out hot path, provenance
        joined from the ledger in one OPTIONAL hop. Arrows are still matched
        directly — the ledger never sits on the hot path (PRD #57 Read paths).
        """
        query = (
            "MATCH (a:Entity)-[r]->(b:Entity) "
            "WHERE a.name = $name OR b.name = $name "
            "OPTIONAL MATCH (a)-[:REPORTED]->"
            "(e:ExtractionEvent {relation: type(r)})-[:ABOUT]->(b) "
            "WITH a, r, b, e ORDER BY e.created_at "
            "RETURN a.name AS subject, type(r) AS relation, b.name AS object, "
            "r.confidence AS confidence, r.scope_conditions AS scope_conditions, "
            "r.source_ref AS legacy_source_ref, r.evidence AS legacy_evidence, "
            "collect(e.source_ref) AS row_refs, collect(e.evidence) AS row_evidence"
        )
        with self.driver.session(database=self.database) as session:
            rows = session.run(query, name=name)
            return [
                GraphEdge(
                    row["subject"], row["relation"], row["object"], row["confidence"],
                    *assemble_provenance(
                        row["row_refs"], row["row_evidence"],
                        row["legacy_source_ref"], row["legacy_evidence"],
                    ),
                    row["scope_conditions"] or "",
                )
                for row in rows
            ]

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
