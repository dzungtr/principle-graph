"""Mode-2 terminal review of a proposed graph delta."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable


@dataclass(frozen=True)
class GraphEntity:
    name: str
    entity_type: str


@dataclass(frozen=True)
class GraphEdge:
    subject: str
    relation: str
    object: str
    confidence: float
    source_ref: str = ""
    evidence: tuple[str, ...] = ()
    scope_conditions: str = ""


@dataclass(frozen=True)
class GraphDelta:
    new_entities: list[GraphEntity] = field(default_factory=list)
    new_edges: list[GraphEdge] = field(default_factory=list)
    merges: list[tuple[str, str]] = field(default_factory=list)
    confidence_changes: list[tuple[str, str, str, float, float]] = field(default_factory=list)
    # Full merged edges for confidence changes, including provenance.
    updated_edges: list[GraphEdge] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewResult:
    approved: GraphDelta
    rejected: list[dict[str, object]]


def render_delta(delta: GraphDelta) -> str:
    lines = ["Graph delta review (Mode 2)", "=" * 28]
    lines.append("New entities:")
    lines.extend(f"  + {entity.name} [{entity.entity_type}]" for entity in delta.new_entities)
    lines.append("New edges:")
    lines.extend(
        f"  + {edge.subject} -[{edge.relation}, confidence {edge.confidence:.2f}]-> {edge.object}"
        for edge in delta.new_edges
    )
    lines.append("Entity merges:")
    lines.extend(f"  + {source} -> {canonical}" for source, canonical in delta.merges)
    lines.append("Confidence changes:")
    lines.extend(
        f"  + {subject} -[{relation}]-> {obj}: {before:.2f} -> {after:.2f}"
        for subject, relation, obj, before, after in delta.confidence_changes
    )
    return "\n".join(lines)


def review_delta(delta: GraphDelta, input_fn: Callable[[str], str] = input) -> ReviewResult:
    print(render_delta(delta))
    rejected: list[dict[str, object]] = []
    while True:
        decision = input_fn("[a]pprove, [r]eject, or [e]dit confidence: ").strip().lower()
        if decision in {"a", "approve"}:
            return ReviewResult(delta, rejected)
        if decision in {"r", "reject"}:
            rejected.append({"delta": delta, "decision": "rejected", "reason": "rejected by reviewer"})
            return ReviewResult(GraphDelta(), rejected)
        if decision in {"e", "edit"}:
            if not delta.new_edges:
                print("No editable edges in this delta.")
                continue
            try:
                confidence = float(input_fn("New confidence (0.0-1.0): "))
            except ValueError:
                print("Confidence must be a number.")
                continue
            if not 0 <= confidence <= 1:
                print("Confidence must be between 0.0 and 1.0.")
                continue
            edited = replace(delta.new_edges[0], confidence=confidence)
            delta = replace(delta, new_edges=[edited, *delta.new_edges[1:]])
            print(render_delta(delta))
            continue
        print("Choose approve, reject, or edit.")
