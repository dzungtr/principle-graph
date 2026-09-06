"""One-off relation normalization: pure plan over the existing ledger (ADR-0003).

The pass re-canonicalizes every ledger row through the relation registry:
alias collapse, inverse-pair direction collapse (the row moves to the flipped
triple), and same-source verb-variant collapse onto one identity. Rewritten
rows preserve their original verb as ``raw_relation``; rows already canonical
or carrying unknown verbs are left untouched, so a second run is an empty
plan — full idempotency. The Neo4j writer (:meth:`Neo4jGraphWriter.
normalize_relations`) executes the plan; this module only decides what the
writes are, mirroring the ledger-core/plan split (PRD #57, ADR-0002).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from .label_registry import LabelRegistry
from .ledger import ArrowUpdate, LedgerRow, complement_aggregate


@dataclass(frozen=True)
class NormalizationPlan:
    """What a normalization pass should execute for the current ledger."""

    rows_to_create: tuple[LedgerRow, ...]
    # Old identities removed because their canonical row replaces them
    # (includes same-source verb variants collapsed onto an existing identity).
    rows_to_delete: tuple[tuple[str, str, str, str], ...]
    # Old triples whose arrows lose every row and must be removed.
    arrows_to_delete: tuple[tuple[str, str, str], ...]
    # Recomputed aggregates for target triples touched by the rewrite.
    arrow_updates: tuple[ArrowUpdate, ...]
    # Distinct unknown verbs seen (passed through, flagged for consolidation).
    unknown_flagged: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.rows_to_create or self.rows_to_delete
                    or self.arrows_to_delete or self.arrow_updates)


def plan_normalization(
    existing_rows: Sequence[LedgerRow], registry: LabelRegistry
) -> NormalizationPlan:
    """Plan the canonicalization of *existing_rows* under *registry*.

    All rows of a rewritten triple move together (canonicalization is a
    function of the relation alone), so rewritten triples are emptied and their
    arrows removed; target triples recompute their aggregate from the final row
    set. Rows whose triple is unchanged — canonical verbs, unknown verbs — are
    kept untouched, which is what makes the re-run an empty plan.
    """
    kept: list[LedgerRow] = []
    rewritten: list[tuple[LedgerRow, tuple[str, str, str, str]]] = []
    deleted: list[tuple[str, str, str, str]] = []
    unknown: list[str] = []
    for row in existing_rows:
        canon = registry.canonicalize(row.subject, row.relation, row.object)
        if canon.unknown:
            if canon.raw_relation not in unknown:
                unknown.append(canon.raw_relation)
        target_triple = (canon.subject, canon.relation.upper(), canon.object)
        if target_triple == (row.subject, row.relation, row.object):
            kept.append(row)
            continue
        canonical_row = LedgerRow(
            canon.subject, canon.relation, canon.object, row.source_ref,
            row.confidence, row.evidence, row.scope_conditions, row.domain,
            raw_relation=row.raw_relation or row.relation,
        )
        deleted.append(row.identity)
        rewritten.append((canonical_row, row.identity))

    # A canonical identity that already exists untouched (kept) wins: its
    # verb-variant siblings are deleted without replacement, so the pattern
    # MERGE never has two candidates for one identity.
    kept_identities = {row.identity for row in kept}
    created: list[LedgerRow] = []
    claimed: set[tuple[str, str, str, str]] = set(kept_identities)
    for canonical_row, _source_identity in rewritten:
        if canonical_row.identity in claimed:
            # Same-source verb variants collapse: the existing (kept or first
            # rewritten) row holds the canonical identity; later variants are
            # deleted with no replacement.
            continue
        claimed.add(canonical_row.identity)
        created.append(canonical_row)

    rewritten_triples = {identity[:3] for identity in deleted}
    final_rows: dict[tuple[str, str, str], list[LedgerRow]] = defaultdict(list)
    for row in kept:
        final_rows[row.identity[:3]].append(row)
    for row in created:
        final_rows[row.identity[:3]].append(row)

    updates: list[ArrowUpdate] = []
    for triple in sorted({row.identity[:3] for row in created}):
        rows = final_rows[triple]
        # The rewrite is the newest statement for the triple; when it carries a
        # scope condition it becomes the arrow's denormalized scope ("" keeps
        # the arrow's existing scope, matching the ledger write path).
        scope = next((row.scope_conditions for row in created
                      if row.identity[:3] == triple and row.scope_conditions), "")
        updates.append(ArrowUpdate(
            triple[0], triple[1], triple[2],
            complement_aggregate([row.confidence for row in rows]),
            scope,
        ))
    return NormalizationPlan(
        rows_to_create=tuple(created),
        rows_to_delete=tuple(deleted),
        arrows_to_delete=tuple(sorted(rewritten_triples)),
        arrow_updates=tuple(updates),
        unknown_flagged=tuple(unknown),
    )


__all__ = ["NormalizationPlan", "plan_normalization"]
