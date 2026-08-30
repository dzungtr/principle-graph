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
    # Refresh mode only: matched identities replaced by the candidate row.
    rows_to_update: tuple[LedgerRow, ...] = ()


# Repeat-extraction behavior for a matched (triple, source_ref) identity.
# keep-first (default): re-ingest is a structural no-op (ADR-0002 idempotency).
# refresh (opt-in): a source deliberately refining its claim replaces its row.
REPEAT_MODES = ("keep-first", "refresh")


def resolve_repeat_mode(value: str) -> str:
    """Validate and normalize a repeat-mode value; raises ``ValueError`` on junk."""
    normalized = str(value).strip().casefold()
    if normalized not in REPEAT_MODES:
        raise ValueError(
            f"invalid repeat mode {value!r}: expected one of {', '.join(REPEAT_MODES)}"
        )
    return normalized


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
    mode: str = "keep-first",
) -> LedgerWritePlan:
    """Plan the writes for one batch of candidates under a repeat mode.

    ``keep-first`` (default, ADR-0002): identity is the duplicate-skip rule —
    a candidate whose ``(subject, relation, object, source_ref)`` identity matches
    an existing row is skipped and matched rows are never overwritten; re-ingest
    can only leave the arrow unchanged, never wobble it.

    ``refresh`` (opt-in): a matched identity is *replaced* by the candidate — the
    refreshed row lands in ``rows_to_update``, the arrow recompute uses the
    refreshed value, and the refreshed row is the newest row for scope purposes.
    Within one batch the last candidate for an identity wins.

    Under both modes new identities append, and every touched arrow recomputes
    its aggregate from all rows for the triple (with refreshed values applied).
    """
    repeat_mode = resolve_repeat_mode(mode)
    existing_by_identity = {row.identity: row for row in existing_rows}
    creates: list[LedgerRow] = []
    skipped: list[tuple[str, str, str, str]] = []
    refreshed: list[LedgerRow] = []
    refreshed_by_identity: dict[tuple[str, str, str, str], LedgerRow] = {}
    created_identities: set[tuple[str, str, str, str]] = set()
    seen: set[tuple[str, str, str, str]] = set(existing_by_identity)
    triple_order: list[tuple[str, str, str]] = []
    newest_scope: dict[tuple[str, str, str], str] = {}
    for candidate in candidates:
        triple = candidate.identity[:3]
        if candidate.identity in seen:
            if repeat_mode == "refresh":
                # Deliberate refinement: a repeat either replaces a pending
                # create (last wins, still one write) or becomes a row update.
                if candidate.identity in created_identities:
                    creates = [row for row in creates
                               if row.identity != candidate.identity]
                    created_identities.discard(candidate.identity)
                    refreshed.append(candidate)
                    refreshed_by_identity[candidate.identity] = candidate
                else:
                    refreshed = [row for row in refreshed
                                 if row.identity != candidate.identity]
                    refreshed.append(candidate)
                    refreshed_by_identity[candidate.identity] = candidate
                if triple not in newest_scope:
                    triple_order.append(triple)
                newest_scope[triple] = candidate.scope_conditions
            else:
                skipped.append(candidate.identity)
                # Skipped writes still recompute their arrow (idempotently) but
                # never move its scope: no newer row to take scope from.
                if triple not in newest_scope:
                    triple_order.append(triple)
                    newest_scope[triple] = ""
            continue
        seen.add(candidate.identity)
        creates.append(candidate)
        created_identities.add(candidate.identity)
        if triple not in newest_scope:
            triple_order.append(triple)
        newest_scope[triple] = candidate.scope_conditions
    updates: list[ArrowUpdate] = []
    for triple in triple_order:
        confidences = [refreshed_by_identity.get(
                           row.identity, row).confidence for row in existing_rows
                       if row.identity[:3] == triple]
        confidences += [row.confidence for row in creates
                        if row.identity[:3] == triple]
        # Refreshed rows that replaced a batch-created row are no longer in
        # `creates` and have no existing row to substitute for.
        confidences += [row.confidence for row in refreshed
                        if row.identity[:3] == triple
                        and row.identity not in existing_by_identity]
        updates.append(ArrowUpdate(triple[0], triple[1], triple[2],
                                   complement_aggregate(confidences),
                                   newest_scope[triple]))
    return LedgerWritePlan(tuple(creates), tuple(skipped), tuple(updates),
                           tuple(refreshed))


__all__ = [
    "REPEAT_MODES",
    "ArrowUpdate",
    "LedgerRow",
    "LedgerWritePlan",
    "complement_aggregate",
    "plan_ledger_writes",
    "resolve_repeat_mode",
]
