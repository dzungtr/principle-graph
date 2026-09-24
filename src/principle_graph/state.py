"""State tracking: candidate schema, ledger planning, and claim rendering (issue #98).

Pure module — no database dependency (the ledger.py precedent). States follow
the ADR-0002 two-layer precedent (ADR-0007): append-only ``:StateEvent`` rows
(identity = entity + state_key + source_ref, keep-first) plus a denormalized
current-state map recomputed from rows — latest ``as_of`` wins, then
confidence; disagreement rows remain queryable. States pass the novelty gate
rendered as plain claims, deduped by (entity, state_key, value) — one Jev call
per unique assertion (PRD #95, Implementation Decisions → State storage).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .ledger import REPEAT_MODES, resolve_repeat_mode


@dataclass(frozen=True)
class StateEvent:
    """One append-only state assertion: a row, not the book (ADR-0002 precedent).

    Values are strings so numeric (``42``) and qualitative (``elevated``)
    measurements share one schema; the unit carries the numeric dimension.
    """

    entity: str
    entity_type: str
    state_key: str
    value: str
    unit: str
    as_of: str
    confidence: float
    evidence: str
    scope_conditions: str
    source_ref: str
    # Set at the write boundary when the key is not in the state registry —
    # unknown keys pass flagged, never rejected (PRD #95 never-reject stance).
    unknown_key: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("state confidence must be between 0.0 and 1.0")
        # Identity fields must be strings (values may arrive numeric from JSON).
        for field_name in ("entity", "state_key", "value", "source_ref"):
            object.__setattr__(self, field_name, str(getattr(self, field_name)))

    @property
    def identity(self) -> tuple[str, str, str]:
        """Ledger identity: (entity, state_key, source_ref)."""
        return (self.entity, self.state_key, self.source_ref)


@dataclass(frozen=True)
class EntityState:
    """Current-state read model: the winning row for one (entity, state_key)."""

    entity: str
    state_key: str
    value: str
    unit: str
    as_of: str
    confidence: float
    source_ref: str


@dataclass(frozen=True)
class StateWritePlan:
    """What a state-ledger writer should execute for one batch of candidates."""

    rows_to_create: tuple[StateEvent, ...]
    skipped_identities: tuple[tuple[str, str, str], ...]
    # Denormalized current-state map updates, keyed by entity then state_key.
    current_state: Mapping[str, Mapping[str, Mapping[str, Any]]]


def _current_state_for(entity: str, state_key: str,
                       rows: Sequence[StateEvent]) -> dict[str, Any] | None:
    """Recompute one current-state entry: latest as_of wins, then confidence."""
    relevant = [row for row in rows
                if row.entity == entity and row.state_key == state_key]
    if not relevant:
        return None
    winner = max(relevant, key=lambda row: (row.as_of, row.confidence))
    return {
        "value": winner.value,
        "unit": winner.unit,
        "as_of": winner.as_of,
        "confidence": winner.confidence,
        "source_ref": winner.source_ref,
    }


def plan_state_writes(
    existing_rows: Sequence[StateEvent],
    candidates: Sequence[StateEvent],
    mode: str = "keep-first",
) -> StateWritePlan:
    """Plan the writes for one batch of state candidates under a repeat mode.

    ``keep-first`` (default, ADR-0002 idempotency): identity is
    ``(entity, state_key, source_ref)``; a candidate matching an existing row is
    skipped, so same-source re-ingest is a structural no-op. A second source
    asserting a different value appends a new row — disagreement stays
    queryable — and the current-state map recomputes from all rows.
    """
    repeat_mode = resolve_repeat_mode(mode)
    if repeat_mode != "keep-first":
        raise ValueError("state ledger supports keep-first only")
    existing = {row.identity: row for row in existing_rows}
    creates: list[StateEvent] = []
    skipped: list[tuple[str, str, str]] = []
    seen = set(existing)
    for candidate in candidates:
        if candidate.identity in seen:
            skipped.append(candidate.identity)
            continue
        seen.add(candidate.identity)
        creates.append(candidate)
    all_rows = list(existing_rows) + creates
    current: dict[str, dict[str, dict[str, Any]]] = {}
    for row in all_rows:
        entry = _current_state_for(row.entity, row.state_key, all_rows)
        if entry is not None:
            current.setdefault(row.entity, {})[row.state_key] = entry
    return StateWritePlan(tuple(creates), tuple(skipped), current)


def render_state_claim(state: StateEvent) -> str:
    """Render a state assertion as a bare claim: ``Entity's state_key is value``."""
    return f"{state.entity}'s {state.state_key} is {state.value}"


class NoveltyFilter(Protocol):
    def classify(self, claims: Sequence[Mapping[str, Any]]) -> list[Any]: ...


@dataclass(frozen=True)
class StateNoveltyStats:
    """State-side novelty aggregates, shaped like novelty.NoveltyStats."""

    novelty_calls: int = 0
    filtered_noise: int = 0
    filtered_common_sense: int = 0


def apply_state_novelty_filter(
    states: Sequence[StateEvent],
    filter: NoveltyFilter,
) -> tuple[list[StateEvent], StateNoveltyStats]:
    """Gate states as rendered claims, deduped by (entity, state_key, value).

    One Jev call per unique assertion; duplicates of a kept assertion share its
    verdict and flow on. ``noise`` and ``common_sense`` drop. Failure inside the
    filter propagates — the caller aborts before any commit (ADR-0006 stance).
    """
    if not states:
        return [], StateNoveltyStats()
    unique: dict[tuple[str, str, str], StateEvent] = {}
    order: list[tuple[str, str, str]] = []
    for state in states:
        key = (state.entity.casefold(), state.state_key.casefold(),
               state.value.casefold())
        if key not in unique:
            unique[key] = state
            order.append(key)
    claims = [{"claim": render_state_claim(unique[key]),
               "evidence": unique[key].evidence} for key in order]
    verdicts = filter.classify(claims)
    verdict_by_key = dict(zip(order, verdicts))
    kept: list[StateEvent] = []
    noise = common_sense = 0
    for state in states:
        key = (state.entity.casefold(), state.state_key.casefold(),
               state.value.casefold())
        choice = verdict_by_key[key].choice
        if choice == "novel":
            kept.append(state)
        elif choice == "noise":
            noise += 1
        else:
            common_sense += 1
    return kept, StateNoveltyStats(novelty_calls=len(order),
                                   filtered_noise=noise,
                                   filtered_common_sense=common_sense)


__all__ = [
    "EntityState",
    "REPEAT_MODES",
    "StateEvent",
    "StateNoveltyStats",
    "StateWritePlan",
    "apply_state_novelty_filter",
    "plan_state_writes",
    "render_state_claim",
    "resolve_repeat_mode",
]
