"""Pure ledger policy-core tests — no database dependency (issue #58 AC 1).

The ledger module owns the complement aggregate and keep-first write planning
(ADR-0002). Prior art: tests/test_confidence_policy.py.
"""
from principle_graph.ledger import (
    ArrowUpdate,
    LedgerRow,
    complement_aggregate,
    plan_ledger_writes,
)


def row(subject="a", relation="SUPPORTS", object="b", source_ref="s1", confidence=0.5,
        evidence="evidence", scope_conditions="", domain=""):
    return LedgerRow(subject, relation, object, source_ref, confidence, evidence,
                     scope_conditions, domain)


def test_single_row_aggregates_to_itself():
    assert complement_aggregate([0.5]) == 0.5


def test_two_rows_use_the_complement_formula():
    assert complement_aggregate([0.6, 0.5]) == 0.8


def test_three_rows_use_the_complement_formula():
    assert complement_aggregate([0.5, 0.5, 0.5]) == 0.875


def test_out_of_range_inputs_are_clamped():
    assert complement_aggregate([2.0, 2.0]) == 1.0
    assert complement_aggregate([-1.0, 0.5]) == 0.5


def test_output_is_clamped_to_unit_interval():
    assert 0.0 <= complement_aggregate([0.9, 0.9, 0.9, 0.9, 0.9]) <= 1.0


def test_no_rows_aggregate_to_zero():
    assert complement_aggregate([]) == 0.0


def test_aggregate_is_monotonic_in_each_row():
    running = 0.0
    for confidences in ([0.2], [0.2, 0.3], [0.2, 0.3, 0.1]):
        aggregate = complement_aggregate(confidences)
        assert aggregate >= running
        running = aggregate


def test_identity_is_subject_relation_object_source_ref():
    assert row(source_ref="doc:chunk-2").identity == ("a", "SUPPORTS", "b", "doc:chunk-2")


def test_relation_is_normalized_to_uppercase():
    assert row(relation="supports").relation == "SUPPORTS"


def test_row_confidence_must_be_within_bounds():
    try:
        row(confidence=1.5)
    except ValueError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("expected ValueError for out-of-bounds confidence")


def test_new_identity_creates_row_and_updates_arrow():
    plan = plan_ledger_writes([], [row(confidence=0.7, scope_conditions="when armed")])
    assert [r.identity for r in plan.rows_to_create] == [("a", "SUPPORTS", "b", "s1")]
    assert plan.skipped_identities == ()
    assert plan.arrow_updates == (
        ArrowUpdate("a", "SUPPORTS", "b", 0.7, "when armed"),
    )


def test_matched_identity_is_skipped_and_values_never_overwritten():
    existing = [row(source_ref="s1", confidence=0.5, evidence="first")]
    candidate = row(source_ref="s1", confidence=0.9, evidence="second")
    plan = plan_ledger_writes(existing, [candidate])
    assert plan.rows_to_create == ()
    assert plan.skipped_identities == (("a", "SUPPORTS", "b", "s1"),)
    # Aggregate recomputed from existing rows only: keep-first means no wobble.
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.5, ""),)


def test_repeated_identity_within_batch_creates_once():
    plan = plan_ledger_writes([], [row(source_ref="s1", confidence=0.5),
                                   row(source_ref="s1", confidence=0.9)])
    assert [r.confidence for r in plan.rows_to_create] == [0.5]
    assert plan.skipped_identities == (("a", "SUPPORTS", "b", "s1"),)
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.5, ""),)


def test_corroboration_aggregates_existing_and_new_rows():
    existing = [row(source_ref="s1", confidence=0.6)]
    plan = plan_ledger_writes(existing, [row(source_ref="s2", confidence=0.5)])
    assert [r.source_ref for r in plan.rows_to_create] == ["s2"]
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.8, ""),)


def test_relation_case_does_not_split_identity():
    existing = [row(relation="supports", source_ref="s1", confidence=0.6)]
    plan = plan_ledger_writes(existing, [row(relation="SUPPORTS", source_ref="s1")])
    assert plan.rows_to_create == ()
    assert plan.skipped_identities == (("a", "SUPPORTS", "b", "s1"),)


def test_newest_non_empty_scope_wins_on_the_arrow():
    existing = [row(source_ref="s1", confidence=0.5, scope_conditions="old scope")]
    plan = plan_ledger_writes(existing, [row(source_ref="s2", scope_conditions="new scope")])
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.75, "new scope"),)


def test_empty_scope_on_new_row_keeps_existing_arrow_scope():
    existing = [row(source_ref="s1", confidence=0.5, scope_conditions="old scope")]
    plan = plan_ledger_writes(existing, [row(source_ref="s2", scope_conditions="")])
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.75, ""),)


def test_distinct_triples_get_distinct_arrow_updates():
    candidates = [row(subject="a", object="b", source_ref="s1", confidence=0.5),
                  row(subject="c", object="d", source_ref="s1", confidence=0.9)]
    plan = plan_ledger_writes([], candidates)
    assert {u.subject for u in plan.arrow_updates} == {"a", "c"}
    by_subject = {u.subject: u for u in plan.arrow_updates}
    assert by_subject["a"].aggregate_confidence == 0.5
    assert by_subject["c"].aggregate_confidence == 0.9


# ---------------------------------------------------------------------------
# Repeat modes (issue #59): keep-first default vs opt-in refresh.
# ---------------------------------------------------------------------------

from principle_graph.ledger import REPEAT_MODES, resolve_repeat_mode


def test_resolve_repeat_mode_accepts_canonical_and_variations():
    assert resolve_repeat_mode("keep-first") == "keep-first"
    assert resolve_repeat_mode("refresh") == "refresh"
    assert resolve_repeat_mode("REFRESH") == "refresh"
    assert resolve_repeat_mode(" Keep-First ") == "keep-first"


def test_resolve_repeat_mode_rejects_unknown_values_with_clear_error():
    try:
        resolve_repeat_mode("overwrite")
    except ValueError as exc:
        assert "overwrite" in str(exc)
        assert "keep-first" in str(exc) and "refresh" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown repeat mode")


def test_repeat_mode_vocabulary_is_exactly_the_two_documented_modes():
    assert REPEAT_MODES == ("keep-first", "refresh")


def test_explicit_keep_first_mode_is_identical_to_the_default_plan():
    existing = [row(source_ref="s1", confidence=0.5, scope_conditions="old")]
    candidates = [row(source_ref="s1", confidence=0.9, evidence="second")]
    assert (plan_ledger_writes(existing, candidates, mode="keep-first")
            == plan_ledger_writes(existing, candidates))


def test_refresh_replaces_matched_row_values_instead_of_skipping():
    existing = [row(source_ref="s1", confidence=0.5, evidence="first",
                    scope_conditions="old scope")]
    candidate = row(source_ref="s1", confidence=0.9, evidence="refined",
                    scope_conditions="new scope")
    plan = plan_ledger_writes(existing, [candidate], mode="refresh")
    assert plan.rows_to_create == ()
    assert plan.skipped_identities == ()
    assert plan.rows_to_update == (candidate,)
    # The refreshed row is the newest row: its scope becomes the arrow's scope
    # and its confidence is what the aggregate recomputes from.
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.9, "new scope"),)


def test_keep_first_and_refresh_produce_different_plans_for_same_input():
    existing = [row(source_ref="s1", confidence=0.5, evidence="first")]
    candidates = [row(source_ref="s1", confidence=0.9, evidence="second")]
    keep = plan_ledger_writes(existing, candidates)
    refresh = plan_ledger_writes(existing, candidates, mode="refresh")
    assert keep.skipped_identities == (("a", "SUPPORTS", "b", "s1"),)
    assert keep.rows_to_update == ()
    assert refresh.skipped_identities == ()
    assert [r.confidence for r in refresh.rows_to_update] == [0.9]
    assert keep.arrow_updates[0].aggregate_confidence == 0.5
    assert refresh.arrow_updates[0].aggregate_confidence == 0.9


def test_refresh_recompute_uses_refreshed_value_over_all_rows():
    existing = [row(source_ref="s1", confidence=0.5), row(source_ref="s2", confidence=0.5)]
    plan = plan_ledger_writes(existing, [row(source_ref="s1", confidence=0.9)],
                              mode="refresh")
    # 1 - (1 - 0.9)(1 - 0.5): the other row still corroborates.
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.95, ""),)


def test_refresh_within_batch_last_candidate_wins():
    plan = plan_ledger_writes([], [row(source_ref="s1", confidence=0.5, evidence="a"),
                                   row(source_ref="s1", confidence=0.9, evidence="b")],
                              mode="refresh")
    assert plan.rows_to_create == ()
    assert [r.confidence for r in plan.rows_to_update] == [0.9]
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.9, ""),)


def test_refresh_still_creates_rows_for_new_identities():
    existing = [row(source_ref="s1", confidence=0.5)]
    plan = plan_ledger_writes(existing, [row(source_ref="s2", confidence=0.5)],
                              mode="refresh")
    assert [r.source_ref for r in plan.rows_to_create] == ["s2"]
    assert plan.rows_to_update == ()
    assert plan.arrow_updates == (ArrowUpdate("a", "SUPPORTS", "b", 0.75, ""),)


def test_unknown_mode_raises_before_any_planning():
    try:
        plan_ledger_writes([], [row()], mode="overwrite")
    except ValueError as exc:
        assert "repeat mode" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown mode")


# ---------------------------------------------------------------------------
# Source-id prefix rule (issue #79, ADR-0004): the :Source identity grammar.
# ---------------------------------------------------------------------------

from principle_graph.ledger import source_id_of


def test_source_id_is_the_prefix_before_the_first_colon():
    assert source_id_of("book-1:chapter-2/section-3/page-14") == "book-1"


def test_source_id_takes_only_the_first_segment():
    assert source_id_of("doc:chunk-1:extra") == "doc"


def test_source_id_accepts_common_id_shapes():
    for ref in ("markdown:chunk-0", "pdf:p3", "demo-doc:1", "a.b_c-d:x"):
        assert source_id_of(ref) == ref.split(":", 1)[0]


def test_source_id_rejects_refs_without_a_prefix():
    try:
        source_id_of("chunk-1")
    except ValueError as exc:
        assert "chunk-1" in str(exc) and "source id" in str(exc)
    else:
        raise AssertionError("expected ValueError for prefix-less source_ref")


def test_source_id_rejects_empty_and_malformed_prefixes():
    for ref in ("", ":chunk-1", "bad id:x", "book 1:x", "   :x"):
        try:
            source_id_of(ref)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {ref!r}")
