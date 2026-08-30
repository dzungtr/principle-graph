"""Mode-2 terminal review of a proposed graph delta."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Protocol


class _GraphWriter(Protocol):
    def record_rejected(self, record: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class GraphEntity:
    name: str
    entity_type: str
    embedding: tuple[float, ...] | None = None


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
    # Per-source candidates, pre-reduction. Ledger writers commit these as one
    # :ExtractionEvent row each; merged edges above remain the review rendering.
    raw_candidates: list[GraphEdge] = field(default_factory=list)


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
        + (f" (source: {edge.source_ref})" if edge.source_ref else "")
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
            rejected.extend(_rejection_records(delta))
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
            delta = replace(
                delta,
                new_edges=[edited, *delta.new_edges[1:]],
                raw_candidates=_propagate_edit_to_candidates(delta.raw_candidates, edited),
            )
            print(render_delta(delta))
            continue
        print("Choose approve, reject, or edit.")


def _propagate_edit_to_candidates(candidates: list[GraphEdge], edited: GraphEdge) -> list[GraphEdge]:
    """Propagate a Mode-2 confidence edit into the per-source ledger write unit.

    The edited edge is the merged rendering of every raw candidate sharing its
    (subject, relation, object) triple; ledger writers commit raw_candidates, so
    the edit collapses those candidates into one row at the edited confidence,
    keeping the first candidate's provenance. Without this the edit would only
    change the review rendering and silently drop from the committed ledger.
    """
    key = (edited.subject, edited.relation.upper(), edited.object)
    propagated: list[GraphEdge] = []
    replaced = False
    for candidate in candidates:
        if (candidate.subject, candidate.relation.upper(), candidate.object) != key:
            propagated.append(candidate)
        elif not replaced:
            propagated.append(replace(candidate, confidence=edited.confidence))
            replaced = True
    return propagated


def _rejection_records(delta: GraphDelta) -> list[dict[str, object]]:
    """Build auditable records while retaining the candidate's provenance."""
    edges = delta.new_edges
    if not edges:
        return [{"delta": delta, "decision": "rejected", "reason": "rejected by reviewer"}]
    return [
        {
            "delta": delta,
            "candidate": edge,
            "subject": edge.subject,
            "relation": edge.relation,
            "object": edge.object,
            "source_ref": edge.source_ref,
            "evidence": edge.evidence,
            "scope_conditions": edge.scope_conditions,
            "decision": "rejected",
            "reason": "rejected by reviewer",
        }
        for edge in edges
    ]


def review_and_commit(delta: GraphDelta, writer: _GraphWriter,
                      input_fn: Callable[[str], str] = input) -> ReviewResult:
    """Review a complete delta and commit only the approved verdict."""
    from .reduction import commit_delta

    result = review_delta(delta, input_fn=input_fn)
    for record in result.rejected:
        writer.record_rejected(record)
    if result.approved != GraphDelta():
        commit_delta(result.approved, writer)
    return result
