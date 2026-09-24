"""Neo4j persistence adapters for graph and resolution seams."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .label_registry import (
    LabelRegistry,
    default_domain_registry_path,
    default_registry_path,
    load_label_registry,
)
from .ledger import LedgerRow, plan_ledger_writes, resolve_repeat_mode, source_id_of
from .normalization import plan_normalization
from .reduction import GraphEdge, GraphEntity
from .resolution import Entity, SimilarEntity, normalize_name

logger = logging.getLogger(__name__)

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
        relation_registry: LabelRegistry | None = None,
        domain_registry: LabelRegistry | None = None,
        entity_registry: LabelRegistry | None = None,
    ) -> None:
        self.driver = driver
        self.database = database
        self.rejected_sink = RejectedRecordSink(rejected_log_path)
        # Invalid modes raise here, before any session opens (issue #59 AC 2).
        self.repeat_mode = resolve_repeat_mode(repeat_mode)
        # Write-boundary normalization (ADR-0003): the registry loads once, at
        # construction, so a malformed registry fails before any write.
        self.relation_registry = (
            relation_registry if relation_registry is not None
            else load_label_registry(default_registry_path())
        )
        # Domain tags (PRD #76 slice #78) share the loader contract; same
        # fail-fast-at-construction rule. Unknown domains pass through flagged.
        self.domain_registry = (
            domain_registry if domain_registry is not None
            else load_label_registry(default_domain_registry_path())
        )
        self.unknown_domain_counts: dict[str, int] = {}
        self._warned_unknown_domain: set[str] = set()
        # Unknown verbs pass through flagged: warned once per distinct verb,
        # counted per occurrence for the run summary (PRD #76).
        self.unknown_relation_counts: dict[str, int] = {}
        self._warned_unknown: set[str] = set()
        # Issue #99: unknown entity types pass through flagged, warned once per
        # distinct type, counted per occurrence for the run summary.
        self.unknown_entity_type_counts: dict[str, int] = {}
        self._warned_unknown_entity_type: set[str] = set()
        # Entity-type registry (issue #96): used for write-path type
        # canonicalization so the persisted type always matches what the
        # resolver matched on (issue #99).
        self.entity_registry = entity_registry

    def _canonicalize_for_write(
        self, subject: str, relation: str, object_: str
    ) -> tuple[str, str, str, str]:
        """Registry canonicalization before any Cypher is generated (ADR-0003).

        Returns the canonical triple plus the verb exactly as extracted.
        Unknown verbs pass through unchanged and are flagged.
        """
        canon = self.relation_registry.canonicalize(subject, relation, object_)
        if canon.unknown:
            self.unknown_relation_counts[canon.raw_relation] = (
                self.unknown_relation_counts.get(canon.raw_relation, 0) + 1
            )
            if canon.raw_relation not in self._warned_unknown:
                self._warned_unknown.add(canon.raw_relation)
                logger.warning(
                    "unregistered relation %r passed through uncanonicalized; "
                    "add it (or an alias) to the relation registry to consolidate it",
                    canon.raw_relation,
                )
        return canon.subject, canon.relation, canon.object, canon.raw_relation

    def _canonical_domain(self, domain: str) -> str:
        """Canonicalize one optional domain tag at the write boundary (slice #78).

        Empty stays untagged; aliases collapse to the canonical domain; unknown
        domains pass through unchanged and are flagged (warned once per distinct
        domain, counted per occurrence for the run summary) — never rejected.
        """
        domain = str(domain or "").strip()
        if not domain:
            return ""
        canonical = self.domain_registry.canonical_for(domain)
        if not self.domain_registry.is_known(domain):
            self.unknown_domain_counts[canonical] = (
                self.unknown_domain_counts.get(canonical, 0) + 1
            )
            if canonical not in self._warned_unknown_domain:
                self._warned_unknown_domain.add(canonical)
                logger.warning(
                    "unregistered domain %r passed through untagged-canonically; "
                    "add it (or an alias) to the domain registry to consolidate it",
                    canonical,
                )
        return canonical

    def upsert_entity(self, entity: GraphEntity) -> None:
        # Issue #99 write-boundary consistency: the entity type is registry-
        # canonicalized here too, so the resolved/matched type (read key) and
        # the persisted type (write key ``type``) are always the same canonical
        # spelling. Unknown types pass through flagged, never rejected.
        raw_type = entity.entity_type
        entity_type = (
            entity.entity_type if self.entity_registry is None
            else self.entity_registry.canonical_for(raw_type)
        )
        if self.entity_registry is not None and not self.entity_registry.is_known(raw_type):
            self.unknown_entity_type_counts[entity_type] = (
                self.unknown_entity_type_counts.get(entity_type, 0) + 1)
            if entity_type not in self._warned_unknown_entity_type:
                self._warned_unknown_entity_type.add(entity_type)
                logger.warning(
                    "unregistered entity type %r passed through uncanonicalized; "
                    "add it (or an alias) to the entity registry to consolidate it",
                    entity_type,
                )
        query = (
            "MERGE (e:Entity {name: $name, type: $type}) "
            "ON CREATE SET e.created_at = datetime(), e.updated_at = datetime() "
            "ON MATCH SET e.updated_at = datetime() "
            "SET e.embedding = CASE WHEN $embedding IS NULL THEN e.embedding ELSE $embedding END, "
            "e.aliases = CASE WHEN $aliases = [] THEN e.aliases "
            "ELSE coalesce(e.aliases, []) + [a IN $aliases WHERE NOT a IN coalesce(e.aliases, [])] END"
        )
        embedding = getattr(entity, "embedding", None)
        if embedding is not None:
            embedding = list(embedding)
        with self.driver.session(database=self.database) as session:
            session.run(
                query,
                name=entity.name,
                type=entity_type,
                embedding=embedding,
                aliases=list(getattr(entity, "aliases", ()) or ()),
            )

    def upsert_edge(self, edge: GraphEdge) -> None:
        subject, relation, object_, _raw = self._canonicalize_for_write(
            edge.subject, edge.relation, edge.object
        )
        relation = _relation(relation)
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
                subject=subject,
                object=object_,
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
        "e.raw_relation = $raw_relation, "
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
        "e.raw_relation = $raw_relation, "
        "e.updated_at = datetime()"
    )
    # Source provenance (issue #79, ADR-0004): appended to both row writes so new
    # ingestion links every row to its :Source node in the same write. Additive:
    # the row's source_ref string is untouched — ledger identity depends on it.
    _SOURCE_LINK_CLAUSE = (
        "MERGE (src:Source {id: $source_id}) "
        "ON CREATE SET src.first_seen = datetime() "
        "MERGE (e)-[:FROM_SOURCE]->(src)"
    )

    def _write_row(self, session, query: str, row: LedgerRow) -> None:
        """Execute one row write, adding the :Source link when the row has a ref.

        A row without a source id prefix is rejected here, before any write
        fires (ADR-0004: :Source coverage stays total by construction).
        """
        params: dict[str, Any] = {
            "subject": row.subject,
            "object": row.object,
            "relation": row.relation,
            "source_ref": row.source_ref,
            "confidence": row.confidence,
            "evidence": row.evidence,
            "scope_conditions": row.scope_conditions,
            "domain": row.domain,
            "raw_relation": row.raw_relation,
        }
        if row.source_ref:
            params["source_id"] = source_id_of(row.source_ref)
            query = f"{query} {self._SOURCE_LINK_CLAUSE}"
        session.run(query, **params)

    def upsert_extraction(self, edge: GraphEdge) -> None:
        """Append one accepted extraction as a ledger row and recompute the arrow.

        The plan comes from the pure ledger module under this writer's repeat
        mode (keep-first default; refresh replaces matched rows); this adapter
        only executes it. The arrow's derived aggregate is recomputed from all
        rows for the triple on every call — never set independently. The
        relation is canonicalized through the registry before the plan is
        built (ADR-0003); the extracted verb is preserved as ``raw_relation``.
        """
        subject, relation, object_, raw_relation = self._canonicalize_for_write(
            edge.subject, edge.relation, edge.object
        )
        relation = _relation(relation)
        domain = self._canonical_domain(getattr(edge, "domain", ""))
        candidate = LedgerRow(
            subject, relation, object_, edge.source_ref, edge.confidence,
            edge.evidence[0] if edge.evidence else "", edge.scope_conditions,
            domain=domain,
            raw_relation=raw_relation,
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
                self._write_row(session, self._ROW_MERGE_QUERY, row)
            for row in plan.rows_to_update:
                self._write_row(session, self._ROW_REFRESH_QUERY, row)
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
                    # Row shape is a single evidence string (PRD #57); legacy
                    # pre-#58 arrows may carry a list, so the items are joined
                    # at seed time rather than truncated to the first (issue
                    # #72) — the join round-trips through the provenance read.
                    "\n".join(evidence),
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

    # --- source provenance backfill + walk (issue #79, ADR-0004) ----------
    # All rows that carry a source_ref, with their link state; created_at order
    # makes the earliest row per source the first_seen seed.
    _SOURCES_ROWS_LOAD_QUERY = (
        "MATCH (s:Entity)-[:REPORTED]->(e:ExtractionEvent)-[:ABOUT]->(o:Entity) "
        "WHERE e.source_ref IS NOT NULL AND e.source_ref <> '' "
        "RETURN s.name AS subject, e.relation AS relation, o.name AS object, "
        "e.source_ref AS source_ref, e.created_at AS created_at, "
        "EXISTS { (e)-[:FROM_SOURCE]->() } AS has_source "
        "ORDER BY e.created_at"
    )
    _SOURCES_IDS_QUERY = "MATCH (src:Source) RETURN collect(src.id) AS ids"
    # ON CREATE SET only: a re-run must not touch an existing :Source node, so a
    # second backfill changes no state at all (issue #79 idempotency AC).
    _SOURCES_NODE_MERGE_QUERY = (
        "MERGE (src:Source {id: $source_id}) "
        "ON CREATE SET src.first_seen = $first_seen"
    )
    _SOURCES_LINK_QUERY = (
        "MATCH (s:Entity {name: $subject})-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})-[:ABOUT]->"
        "(o:Entity {name: $object}) "
        "MATCH (src:Source {id: $source_id}) "
        "MERGE (e)-[:FROM_SOURCE]->(src)"
    )
    # Provenance walk: everything one source claimed, latest state irrelevant —
    # rows a later extraction or verdict contradicted still appear (issue #79).
    _PROVENANCE_WALK_QUERY = (
        "MATCH (src:Source {id: $source_id})<-[:FROM_SOURCE]-"
        "(e:ExtractionEvent)<-[:REPORTED]-(s:Entity) "
        "MATCH (e)-[:ABOUT]->(o:Entity) "
        "RETURN s.name AS subject, e.relation AS relation, o.name AS object, "
        "e.source_ref AS source_ref, e.confidence AS confidence, "
        "e.evidence AS evidence, e.scope_conditions AS scope_conditions, "
        "e.domain AS domain "
        "ORDER BY e.created_at"
    )

    def backfill_sources(self) -> dict[str, int]:
        """Create :Source nodes and FROM_SOURCE edges for existing rows (ADR-0004).

        Idempotent by construction: only rows without a FROM_SOURCE edge are
        linked, :Source nodes merge with ON CREATE SET first_seen only (first-seen
        = the earliest linked row's created_at), and a second run issues no
        writes at all — timestamps included. The denormalized source_ref string
        on rows is never touched; rows without a usable source id prefix raise
        as data errors rather than fabricating unwalkable provenance.
        """
        with self.driver.session(database=self.database) as session:
            initial_ids = set(
                session.run(self._SOURCES_IDS_QUERY).single()["ids"] or [])
            rows = [dict(record)
                    for record in session.run(self._SOURCES_ROWS_LOAD_QUERY)]
            known = set(initial_ids)
            sources_created = 0
            edges_created = 0
            edges_already_linked = 0
            for record in rows:
                source_id = source_id_of(record["source_ref"])
                if source_id not in known:
                    session.run(
                        self._SOURCES_NODE_MERGE_QUERY,
                        source_id=source_id,
                        first_seen=record["created_at"],
                    ).consume()
                    known.add(source_id)
                    sources_created += 1
                if record["has_source"]:
                    edges_already_linked += 1
                    continue
                session.run(
                    self._SOURCES_LINK_QUERY,
                    subject=record["subject"],
                    relation=_relation(record["relation"]),
                    object=record["object"],
                    source_ref=record["source_ref"],
                    source_id=source_id,
                ).consume()
                edges_created += 1
            row_source_ids = {source_id_of(record["source_ref"]) for record in rows}
            return {
                "rows_seen": len(rows),
                "edges_created": edges_created,
                "edges_already_linked": edges_already_linked,
                "sources_created": sources_created,
                "sources_already_present": len(row_source_ids & initial_ids),
            }

    def provenance_for_source(self, source_id: str) -> list[LedgerRow]:
        """Everything one source claimed: all rows linked via FROM_SOURCE (ADR-0004).

        Read-only over the ledger — later verdicts or re-aggregations never hide
        what a source claimed; rows return with their refs and evidence.
        """
        with self.driver.session(database=self.database) as session:
            return [
                LedgerRow(
                    record["subject"], _relation(record["relation"]),
                    record["object"], record["source_ref"] or "",
                    float(record["confidence"] or 0.0),
                    record["evidence"] or "", record["scope_conditions"] or "",
                    record["domain"] or "",
                )
                for record in session.run(
                    self._PROVENANCE_WALK_QUERY, source_id=source_id
                )
            ]

    # --- relation normalization pass (issue #77, ADR-0003) -----------------
    _NORMALIZE_ROWS_LOAD_QUERY = (
        "MATCH (s:Entity)-[:REPORTED]->(e:ExtractionEvent)-[:ABOUT]->(o:Entity) "
        "RETURN s.name AS subject, e.relation AS relation, o.name AS object, "
        "e.source_ref AS source_ref, e.confidence AS confidence, "
        "e.evidence AS evidence, e.scope_conditions AS scope_conditions, "
        "e.domain AS domain, e.raw_relation AS raw_relation "
        "ORDER BY e.created_at"
    )
    _NORMALIZE_ROW_DELETE_QUERY = (
        "MATCH (s:Entity {name: $subject})-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})-[:ABOUT]->"
        "(o:Entity {name: $object}) "
        "DETACH DELETE e"
    )
    _NORMALIZE_ARROW_DELETE_QUERY = (
        "MATCH (s:Entity {{name: $subject}})-[r:{relation}]->(o:Entity {{name: $object}}) "
        "DELETE r"
    )
    # Recomputed target arrows reuse the ledger write shape: the MERGE creates
    # the arrow when the flipped triple has no arrow yet, and the CASE guard
    # keeps an existing arrow's scope unless the rewrite carries one.
    _NORMALIZE_ARROW_UPDATE_QUERY = (
        "MATCH (s:Entity {{name: $subject}}), (o:Entity {{name: $object}}) "
        "MERGE (s)-[r:{relation}]->(o) "
        "ON CREATE SET r.created_at = datetime() "
        "SET r.confidence = $aggregate_confidence, "
        "r.scope_conditions = CASE WHEN $scope_conditions = '' "
        "THEN r.scope_conditions ELSE $scope_conditions END, "
        "r.updated_at = datetime()"
    )

    def normalize_relations(self) -> dict[str, int]:
        """Re-canonicalize the existing ledger through the registry (ADR-0003).

        The plan is computed over the current rows and executed in one pass:
        rewritten rows move to their canonical identity (inverse flips move the
        row to the flipped triple), same-source verb variants collapse onto one
        row, emptied arrows are deleted, and target arrows recompute their
        aggregate from the final row set. Rows already canonical or carrying
        unknown verbs are left untouched, so a second run is an empty plan —
        full state no-op, timestamps included.
        """
        with self.driver.session(database=self.database) as session:
            existing = [
                LedgerRow(
                    record["subject"], _relation(record["relation"]),
                    record["object"], record["source_ref"] or "",
                    float(record["confidence"] or 0.0),
                    record["evidence"] or "", record["scope_conditions"] or "",
                    record["domain"] or "",
                    raw_relation=record["raw_relation"] or "",
                )
                for record in session.run(self._NORMALIZE_ROWS_LOAD_QUERY)
            ]
            plan = plan_normalization(existing, self.relation_registry)
            for identity in plan.rows_to_delete:
                session.run(
                    self._NORMALIZE_ROW_DELETE_QUERY,
                    subject=identity[0], relation=identity[1],
                    source_ref=identity[3], object=identity[2],
                ).consume()
            for row in plan.rows_to_create:
                # _write_row re-links FROM_SOURCE so provenance coverage stays
                # total when a row moves to its canonical identity (#85 seam).
                self._write_row(session, self._ROW_MERGE_QUERY, row)
            for triple in plan.arrows_to_delete:
                session.run(
                    self._NORMALIZE_ARROW_DELETE_QUERY.format(relation=triple[1]),
                    subject=triple[0], object=triple[2],
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
                    self._NORMALIZE_ARROW_UPDATE_QUERY.format(relation=update.relation),
                    subject=update.subject,
                    object=update.object,
                    aggregate_confidence=update.aggregate_confidence,
                    scope_conditions=update.scope_conditions,
                ).consume()
                arrows_recomputed += 1
            return {
                "rows_seen": len(existing),
                "rows_created": len(plan.rows_to_create),
                "rows_deleted": len(plan.rows_to_delete),
                "arrows_deleted": len(plan.arrows_to_delete),
                "arrows_recomputed": arrows_recomputed,
                "unknown_flagged": len(plan.unknown_flagged),
            }

    # --- decide-mode fact-check store (issue #80, ADR-0005) ----------------
    _FACTCHECK_ROWS_FOR_DOMAIN_QUERY = (
        "MATCH (s:Entity)-[:REPORTED]->(e:ExtractionEvent)-[:ABOUT]->(o:Entity) "
        "WHERE e.domain = $domain "
        "RETURN s.name AS subject, e.relation AS relation, o.name AS object, "
        "e.source_ref AS source_ref, e.confidence AS confidence, "
        "e.evidence AS evidence, e.scope_conditions AS scope_conditions, "
        "e.domain AS domain, e.raw_relation AS raw_relation "
        "ORDER BY e.created_at"
    )
    # Append-only receipt: CREATE (never MERGE) makes each run a fresh verdict;
    # rows are only MATCHed — no SET touches row or arrow properties.
    _VERDICT_CREATE_QUERY = (
        "MATCH (s:Entity {name: $subject})-[:REPORTED]->"
        "(e:ExtractionEvent {relation: $relation, source_ref: $source_ref})"
        "-[:ABOUT]->(o:Entity {name: $object}) "
        "CREATE (v:Verdict {id: $verdict_id, verdict: $verdict, "
        "confidence: $confidence, evidence_urls: $evidence_urls, "
        "model: $model, search_provider: $search_provider, "
        "reasoning: $reasoning, created_at: $created_at}) "
        "CREATE (v)-[:CHECKS]->(e) "
        "RETURN v.id AS id"
    )
    _VERDICTS_FOR_SOURCE_QUERY = (
        "MATCH (src:Source {id: $source_id})-[:FROM_SOURCE]->"
        "(e:ExtractionEvent)<-[:REPORTED]-(s:Entity), "
        "(e)-[:ABOUT]->(o:Entity), (v:Verdict)-[:CHECKS]->(e) "
        "RETURN s.name AS subject, e.relation AS relation, o.name AS object, "
        "e.source_ref AS source_ref, v.verdict AS verdict, "
        "v.confidence AS confidence, v.evidence_urls AS evidence_urls, "
        "v.model AS model, v.search_provider AS search_provider, "
        "v.reasoning AS reasoning, toString(v.created_at) AS created_at "
        "ORDER BY v.created_at, v.id"
    )

    def rows_for_domain(self, domain: str) -> list[LedgerRow]:
        """All ledger rows carrying a domain tag (ADR-0002 optional field)."""
        with self.driver.session(database=self.database) as session:
            return [
                LedgerRow(
                    record["subject"], _relation(record["relation"]),
                    record["object"], record["source_ref"] or "",
                    float(record["confidence"] or 0.0),
                    record["evidence"] or "", record["scope_conditions"] or "",
                    record["domain"] or "",
                    raw_relation=record["raw_relation"] or "",
                )
                for record in session.run(
                    self._FACTCHECK_ROWS_FOR_DOMAIN_QUERY, domain=domain)
            ]

    def save_verdicts(self, receipts) -> dict[str, int]:
        """Write append-only :Verdict nodes CHECKS-wired to their rows (ADR-0005).

        Each receipt becomes a new node (CREATE, not MERGE) so re-runs append
        fresh verdicts; rows and arrows are never mutated. Receipts whose row
        identity no longer exists are counted, not written.
        """
        import uuid
        created = 0
        not_found = 0
        with self.driver.session(database=self.database) as session:
            for receipt in receipts:
                record = session.run(
                    self._VERDICT_CREATE_QUERY,
                    verdict_id=str(uuid.uuid4()),
                    subject=receipt.subject, relation=_relation(receipt.relation),
                    object=receipt.object, source_ref=receipt.source_ref,
                    verdict=receipt.verdict, confidence=receipt.confidence,
                    evidence_urls=list(receipt.evidence_urls),
                    model=receipt.model, search_provider=receipt.search_provider,
                    reasoning=receipt.reasoning, created_at=receipt.created_at,
                ).single()
                if record is None:
                    not_found += 1
                else:
                    created += 1
        return {"verdicts_created": created, "rows_not_found": not_found}

    def verdicts_for_source(self, source_id: str) -> list[dict]:
        """Verdicts over everything one source claimed (per-source walking)."""
        with self.driver.session(database=self.database) as session:
            return [
                dict(record)
                for record in session.run(
                    self._VERDICTS_FOR_SOURCE_QUERY, source_id=source_id)
            ]

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

    # Issue #99: token-overlap prefilter for containment matching. The exact
    # containment subset check happens in resolution.containment_matches; this
    # query only narrows the candidate set (shared token or token inside name).
    def containment_candidates(self, name: str, entity_type: str) -> Sequence[Entity]:
        tokens = [t for t in normalize_name(name).split() if t]
        query = (
            "MATCH (e:Entity {type: $type}) "
            "WHERE any(t IN $tokens WHERE t IN split(toLower(e.name), ' ')) "
            "OR any(t IN split(toLower(e.name), ' ') WHERE t IN $name) "
            "RETURN e AS node LIMIT 100"
        )
        with self.driver.session(database=self.database) as session:
            return [_entity(row["node"]) for row in session.run(
                query, type=entity_type, tokens=tokens, name=normalize_name(name))]

    def add_alias(self, entity: Entity, alias: str) -> None:
        query = (
            "MATCH (e:Entity {name: $name, type: $type}) "
            "SET e.aliases = coalesce(e.aliases, []) "
            "+ [a IN [$alias] WHERE NOT a IN coalesce(e.aliases, [])]"
        )
        with self.driver.session(database=self.database) as session:
            session.run(query, name=entity.name, type=entity.type, alias=alias)

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
