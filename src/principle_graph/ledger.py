"""Ledger policy core: complement aggregation and keep-first write planning.

Pure module — no database dependency (PRD #57 Implementation Decisions). The
Neo4j writer executes :class:`LedgerWritePlan` objects as reified upserts;
this module decides what the writes are. Schema context: ADR-0002.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class LedgerRow:
    """One append-only extraction event: a row, not the book (ADR-0002)."""

    subject: str
    relation: str
    object: str
    source_ref: str
    confidence: float
    evidence: str
    scope_conditions: str = ""
    domain: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("row confidence must be between 0.0 and 1.0")
        object.__setattr__(self, "relation", str(self.relation).upper())

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """Ledger identity: (subject, relation, object, source_ref)."""
        return (self.subject, self.relation, self.object, self.source_ref)


@dataclass(frozen=True)
class ArrowUpdate:
    """Recomputed current state for one Arrow — never set independently."""

    subject: str
    relation: str
    object: str
    aggregate_confidence: float
    # Newest row's scope; "" means "keep the arrow's existing scope conditions".
    scope_conditions: str


@dataclass(frozen=True)
class LedgerWritePlan:
    """What a ledger writer should execute for one batch of candidates."""

    rows_to_create: tuple[LedgerRow, ...]
    skipped_identities: tuple[tuple[str, str, str, str], ...]
    arrow_updates: tuple[ArrowUpdate, ...]


def complement_aggregate(confidences: Sequence[float]) -> float:
    """Independent-evidence complement aggregate ``1 − Π(1 − ci)``, clamped to [0, 1].

    One row aggregates to itself; no rows aggregate to 0.0; the result is
    monotonic in each row confidence (docs/confidence-policy.md, ADR-0002).
    """
    product = 1.0
    for value in confidences:
        value = max(0.0, min(1.0, float(value)))
        product *= 1.0 - value
    return max(0.0, min(1.0, 1.0 - product))


def plan_ledger_writes(
    existing_rows: Sequence[LedgerRow],
    candidates: Sequence[LedgerRow],
) -> LedgerWritePlan:
    """Keep-first plan (ADR-0002): identity is the duplicate-skip rule.

    A candidate whose ``(subject, relation, object, source_ref)`` identity matches
    an existing row is skipped — matched rows are never overwritten. New
    identities append; every touched arrow recomputes its aggregate from all
    rows for the triple (existing plus appended), so re-ingest can only leave
    the arrow unchanged, never wobble it.
    """
    existing_by_identity = {row.identity: row for row in existing_rows}
    creates: list[LedgerRow] = []
    skips: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set(existing_by_identity)
    triple_order: list[tuple[str, str, str]] = []
    newest_scope: dict[tuple[str, str, str], str] = {}
    for candidate in candidates:
        triple = candidate.identity[:3]
        if candidate.identity in seen:
            skips.append(candidate.identity)
            # Skipped writes still recompute their arrow (idempotently) but never
            # move its scope: there is no newer row to take scope from.
            if triple not in newest_scope:
                triple_order.append(triple)
                newest_scope[triple] = ""
            continue
        seen.add(candidate.identity)
        creates.append(candidate)
        if triple not in newest_scope:
            triple_order.append(triple)
        newest_scope[triple] = candidate.scope_conditions
    updates: list[ArrowUpdate] = []
    for triple in triple_order:
        confidences = [row.confidence for row in existing_rows
                       if row.identity[:3] == triple]
        confidences += [row.confidence for row in creates
                        if row.identity[:3] == triple]
        updates.append(ArrowUpdate(triple[0], triple[1], triple[2],
                                   complement_aggregate(confidences),
                                   newest_scope[triple]))
    return LedgerWritePlan(tuple(creates), tuple(skips), tuple(updates))


__all__ = [
    "ArrowUpdate",
    "LedgerRow",
    "LedgerWritePlan",
    "complement_aggregate",
    "plan_ledger_writes",
]
