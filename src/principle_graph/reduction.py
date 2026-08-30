"""Reduction, delta assembly, and approved graph commits."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .review import GraphDelta, GraphEdge, GraphEntity


def aggregate_confidence(previous: float, event: float, *, independent: bool = True) -> float:
    """Combine extraction support, ignoring repeated events from one source."""
    previous = max(0.0, min(1.0, previous))
    event = max(0.0, min(1.0, event))
    if not independent:
        return previous
    return max(0.0, min(1.0, 1 - (1 - previous) * (1 - event)))


def _key(edge: GraphEdge) -> tuple[str, str, str]:
    return edge.subject, edge.relation.upper(), edge.object


def reduce_edges(edges: Sequence[GraphEdge]) -> list[GraphEdge]:
    """Reduce repeated endpoint/relation triples to one edge and retain evidence."""
    reduced: dict[tuple[str, str, str], GraphEdge] = {}
    seen_sources: dict[tuple[str, str, str], set[str]] = {}
    for edge in edges:
        if not 0 <= edge.confidence <= 1:
            raise ValueError("confidence must be between 0.0 and 1.0")
        key = _key(edge)
        evidence = tuple(edge.evidence) if edge.evidence else (() if not edge.source_ref else (edge.source_ref,))
        if key not in reduced:
            reduced[key] = GraphEdge(edge.subject, edge.relation.upper(), edge.object,
                                     edge.confidence, edge.source_ref, evidence,
                                     edge.scope_conditions)
            seen_sources[key] = {edge.source_ref} if edge.source_ref else set()
            continue
        prior = reduced[key]
        source_is_new = bool(edge.source_ref) and edge.source_ref not in seen_sources[key]
        merged_evidence = prior.evidence + tuple(item for item in evidence if item not in prior.evidence)
        reduced[key] = GraphEdge(
            prior.subject, prior.relation, prior.object,
            aggregate_confidence(prior.confidence, edge.confidence, independent=source_is_new),
            edge.source_ref or prior.source_ref,
            merged_evidence,
            edge.scope_conditions or prior.scope_conditions,
        )
        if edge.source_ref:
            seen_sources[key].add(edge.source_ref)
    return list(reduced.values())


def assemble_delta(candidates: Sequence[GraphEdge], existing: Sequence[GraphEdge] = (),
                   entities: Sequence[GraphEntity] = ()) -> GraphDelta:
    """Build additions and confidence changes without mutating the permanent graph."""
    proposed = reduce_edges(candidates)
    current = {_key(edge): edge for edge in existing}
    new_edges: list[GraphEdge] = []
    changes: list[tuple[str, str, str, float, float]] = []
    updated_edges: list[GraphEdge] = []
    for edge in proposed:
        old = current.get(_key(edge))
        if old is None:
            new_edges.append(edge)
        else:
            merged = reduce_edges([old, edge])[0]
            if (merged.confidence != old.confidence or merged.evidence != old.evidence
                    or merged.scope_conditions != old.scope_conditions):
                changes.append((edge.subject, edge.relation, edge.object, old.confidence, merged.confidence))
                updated_edges.append(merged)
    return GraphDelta(new_entities=list(entities), new_edges=new_edges, confidence_changes=changes,
                      updated_edges=updated_edges, raw_candidates=list(candidates))


class GraphWriter(Protocol):
    def upsert_entity(self, entity: GraphEntity) -> None: ...
    def upsert_edge(self, edge: GraphEdge) -> None: ...
    def get_edge(self, subject: str, relation: str, object_: str) -> GraphEdge | None: ...
    def record_rejected(self, record: dict[str, object]) -> None: ...


def commit_delta(delta: GraphDelta, writer: GraphWriter) -> None:
    """Commit only an approved delta. Rejected review records are never passed here."""
    upsert_extraction = getattr(writer, "upsert_extraction", None)
    if upsert_extraction is not None and delta.raw_candidates:
        # Ledger path (ADR-0002): each accepted per-source candidate becomes its
        # own :ExtractionEvent row and the writer recomputes the arrow aggregate;
        # merged edges above are the review rendering, not the write unit.
        for entity in delta.new_entities:
            writer.upsert_entity(entity)
        for edge in delta.raw_candidates:
            upsert_extraction(edge)
        return
    for entity in delta.new_entities:
        writer.upsert_entity(entity)
    for edge in delta.new_edges:
        writer.upsert_edge(edge)
    for index, (subject, relation, object_, _before, after) in enumerate(delta.confidence_changes):
        if index < len(delta.updated_edges):
            merged = delta.updated_edges[index]
            if merged.confidence != after:
                raise ValueError("updated edge confidence does not match delta")
            writer.upsert_edge(merged)
            continue
        # Backward-compatible deltas retain existing provenance when no full
        # merged edge is supplied.
        existing = writer.get_edge(subject, relation, object_)
        if existing is None:
            raise KeyError(f"confidence change targets missing edge: {subject}, {relation}, {object_}")
        writer.upsert_edge(GraphEdge(existing.subject, existing.relation, existing.object, after,
                                     existing.source_ref, existing.evidence,
                                     existing.scope_conditions))


@dataclass
class InMemoryGraph:
    """Small graph writer used by the prototype and deterministic tests."""
    entities: list[GraphEntity] = field(default_factory=list)
    edges: dict[tuple[str, str, str], GraphEdge] = field(default_factory=dict)
    rejected: list[dict[str, object]] = field(default_factory=list)

    def upsert_entity(self, entity: GraphEntity) -> None:
        if entity not in self.entities:
            self.entities.append(entity)

    def upsert_edge(self, edge: GraphEdge) -> None:
        key = _key(edge)
        self.edges[key] = edge

    def get_edge(self, subject: str, relation: str, object_: str) -> GraphEdge | None:
        return self.edges.get(_key(GraphEdge(subject, relation, object_, 0.0)))

    def record_rejected(self, record: dict[str, object]) -> None:
        self.rejected.append(record)
