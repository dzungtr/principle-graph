"""State tracking: candidate schema, ledger planning, novelty gate (issue #98).

Prior art: tests/test_novelty.py (Jev transport fakes) and
tests/test_ledger_write_path.py (plan-driven fakes, no database). States follow
the ADR-0002 two-layer precedent: append-only :StateEvent rows (identity =
(entity, state_key, source_ref), keep-first) plus a denormalized current-state
map recomputed from rows (latest as_of wins, then confidence).
"""
from __future__ import annotations

import pytest

from principle_graph.novelty import NoveltyVerdict
from principle_graph.state import (
    StateEvent,
    apply_state_novelty_filter,
    plan_state_writes,
    render_state_claim,
)


def _state(**overrides) -> StateEvent:
    values = dict(
        entity="Friedrich Merz", entity_type="person", state_key="approval_rating",
        value="42", unit="percent", as_of="2026-09-01", confidence=0.9,
        evidence="Polls put Merz at 42%.", scope_conditions="", source_ref="note-1:chunk-1",
    )
    values.update(overrides)
    return StateEvent(**values)


# --- candidate schema ------------------------------------------------------

def test_state_identity_is_entity_key_source_ref():
    assert _state().identity == ("Friedrich Merz", "approval_rating", "note-1:chunk-1")


def test_state_value_may_be_qualitative():
    state = _state(state_key="yield_level", value="elevated", unit="")
    assert state.value == "elevated"


def test_state_confidence_bounds_enforced():
    with pytest.raises(ValueError):
        _state(confidence=1.5)


def test_render_state_claim_is_a_plain_claim():
    assert render_state_claim(_state()) == "Friedrich Merz's approval_rating is 42"
    assert render_state_claim(_state(state_key="yield_level", value="elevated")) == \
        "Friedrich Merz's yield_level is elevated"


# --- ledger planning (ADR-0002 precedent) ----------------------------------

def test_new_identity_appends_row_and_recomputes_current_state():
    plan = plan_state_writes([], [_state()])
    assert [row.identity for row in plan.rows_to_create] == \
        [("Friedrich Merz", "approval_rating", "note-1:chunk-1")]
    assert plan.current_state["Friedrich Merz"]["approval_rating"]["value"] == "42"


def test_same_source_reingest_is_structural_noop():
    plan = plan_state_writes([], [_state()])
    second = plan_state_writes(plan.rows_to_create, [_state()])
    assert second.rows_to_create == ()
    assert second.skipped_identities == (("Friedrich Merz", "approval_rating", "note-1:chunk-1"),)


def test_second_source_different_value_keeps_both_rows_current_is_latest_as_of():
    rows = [_state()]
    newer = _state(value="38", as_of="2026-10-01", source_ref="note-2:chunk-1")
    plan = plan_state_writes(rows, [newer])
    assert len(plan.rows_to_create) == 1
    current = plan.current_state["Friedrich Merz"]["approval_rating"]
    assert current["value"] == "38"
    assert current["source_ref"] == "note-2:chunk-1"
    # Both rows persist: identity includes source_ref, so nothing is skipped.
    assert plan.skipped_identities == ()


def test_same_as_of_tie_breaks_on_higher_confidence():
    rows = [_state(as_of="2026-09-01", confidence=0.5)]
    candidate = _state(value="38", as_of="2026-09-01", confidence=0.8,
                       source_ref="note-2:chunk-1")
    plan = plan_state_writes(rows, [candidate])
    assert plan.current_state["Friedrich Merz"]["approval_rating"]["value"] == "38"


def test_lower_confidence_does_not_win_on_tie():
    rows = [_state(as_of="2026-09-01", confidence=0.9)]
    candidate = _state(value="38", as_of="2026-09-01", confidence=0.5,
                       source_ref="note-2:chunk-1")
    plan = plan_state_writes(rows, [candidate])
    assert plan.current_state["Friedrich Merz"]["approval_rating"]["value"] == "42"


def test_disagreement_rows_remain_queryable():
    rows = [_state()]
    plan = plan_state_writes(rows, [_state(value="38", as_of="2026-10-01",
                                           source_ref="note-2:chunk-1")])
    all_rows = list(rows) + list(plan.rows_to_create)
    assert {(row.value, row.source_ref) for row in all_rows} == \
        {("42", "note-1:chunk-1"), ("38", "note-2:chunk-1")}


def test_invalid_repeat_mode_rejected():
    with pytest.raises(ValueError):
        plan_state_writes([], [_state()], mode="overwrite")


# --- novelty gate: states as rendered claims --------------------------------

class FakeFilter:
    """Recording fake with one verdict per rendered claim (prior art: test_novelty)."""

    def __init__(self, verdict_by_claim):
        self.verdict_by_claim = verdict_by_claim
        self.claims: list[str] = []

    def classify(self, claims):
        verdicts = []
        for claim in claims:
            self.claims.append(claim["claim"])
            verdicts.append(NoveltyVerdict(claim, self.verdict_by_claim.get(claim["claim"], "novel"),
                                           0.9, {}))
        return verdicts


def test_states_classified_once_per_unique_entity_key_value():
    states = [_state(), _state(), _state(value="38", source_ref="note-2:chunk-1")]
    kept, stats = apply_state_novelty_filter(states, FakeFilter({"Friedrich Merz's approval_rating is 42": "novel"}))
    # Unique assertions: (merz, approval_rating, 42) and (merz, approval_rating, 38).
    assert stats.novelty_calls == 2
    assert len(kept) == 3
    assert {s.source_ref for s in kept} == {"note-1:chunk-1", "note-2:chunk-1"}


def test_noise_state_dropped_and_common_sense_counted():
    states = [_state(), _state(value="elevated", state_key="yield_level",
                               unit="", evidence="Yields remain elevated.")]
    fake = FakeFilter({
        "Friedrich Merz's approval_rating is 42": "noise",
        "Friedrich Merz's yield_level is elevated": "novel",
    })
    kept, stats = apply_state_novelty_filter(states, fake)
    assert [s.state_key for s in kept] == ["yield_level"]
    assert stats.filtered_noise == 1
    assert stats.filtered_common_sense == 0
    assert fake.claims == ["Friedrich Merz's approval_rating is 42",
                           "Friedrich Merz's yield_level is elevated"]


def test_empty_states_short_circuit_no_calls():
    kept, stats = apply_state_novelty_filter([], FakeFilter({}))
    assert kept == [] and stats.novelty_calls == 0


def test_gate_failure_propagates():
    class FailingFilter:
        def classify(self, claims):
            raise RuntimeError("jev unreachable")
    with pytest.raises(RuntimeError):
        apply_state_novelty_filter([_state()], FailingFilter())
