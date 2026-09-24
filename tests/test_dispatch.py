"""Mis-shape dispatch: guards classify, Jev decides, pipeline executes (issue #101).

Network-free and database-free. Guard tests use one real demo case per class
from the PRD #95 mis-shape taxonomy; executor tests use recorded Jev responses
over the transport fake (prior art: tests/test_novelty.py).
"""
from __future__ import annotations

import json

import pytest

from principle_graph.dispatch import (
    CATEGORY_MENUS,
    DispatchError,
    DispatchStep,
    Dropped,
    GUARD_CLASSES,
    JevDispatchClient,
    classify_candidate,
    dispatch,
    flagged_endpoint,
)
from principle_graph.extraction_contract import Chunk

CHUNK = Chunk("chunk-1", "France and Canada are building infrastructure.",
              (), (), "s.md:chunk-1")


def _candidate(subject="US Treasury", relation="borrows", object_="$1 trillion",
               evidence="US Treasury is borrowing $1 trillion short-term",
               **overrides):
    candidate = {
        "subject": subject, "subject_type": "organization",
        "relation": relation, "object": object_, "object_type": "quantity",
        "confidence": 0.8, "evidence": evidence,
        "scope_conditions": "", "source_ref": "s.md:chunk-1",
    }
    candidate.update(overrides)
    return candidate


class _FakeDispatcher:
    """Record dispatch requests; replay scripted DispatchSteps or raise."""

    def __init__(self, steps=None, error=None):
        self.calls = []
        self._steps = list(steps or [])
        self._error = error

    def decide(self, candidate, guard_class, chunk):
        self.calls.append((dict(candidate), guard_class, chunk))
        if self._error is not None:
            raise self._error
        return self._steps.pop(0)


# --- guards: one real demo case per class -----------------------------------

def test_numeric_endpoint_demo_case():
    classes = classify_candidate(_candidate())  # "$1 trillion" endpoint
    assert classes[0] == "numeric_endpoint"


def test_qualitative_endpoint_demo_case():
    classes = classify_candidate(_candidate(object_="elevated",
                                            evidence="yields remain elevated"))
    assert "qualitative_endpoint" in classes


def test_clause_demo_case():
    clause = "France and Canada building infrastructure and trade architecture bypassing the United States"
    classes = classify_candidate(_candidate(subject=clause,
                                            evidence=clause))
    assert classes[0] == "clause"


def test_list_demo_case():
    classes = classify_candidate(_candidate(object_="the US, Japan and Australia",
                                            evidence="sanctions involve the US, Japan and Australia"))
    assert "list" in classes


def test_compound_actor_demo_case():
    classes = classify_candidate(_candidate(object_="France and Canada",
                                            evidence="France and Canada signed a deal"))
    assert "compound_actor" in classes
    assert "clause" not in classes


def test_deictic_demo_case():
    classes = classify_candidate(_candidate(object_="previous video",
                                            evidence="as discussed in the previous video"))
    assert "deictic" in classes


def test_self_loop_demo_case():
    classes = classify_candidate(_candidate(subject="Bank of Japan",
                                            object_="bank of japan",
                                            evidence="the Bank of Japan itself"))
    assert "self_loop" in classes


def test_description_in_name_demo_case():
    classes = classify_candidate(_candidate(subject="the ECB (European Central Bank)",
                                            evidence="the ECB (European Central Bank) meets"))
    assert "description_in_name" in classes
    classes = classify_candidate(_candidate(subject="Germany's chancellor",
                                            evidence="Germany's chancellor spoke"))
    assert "description_in_name" in classes


def test_well_formed_candidate_classifies_empty():
    assert classify_candidate(_candidate(subject="Friedrich Merz",
                                         object_="Christian Democratic Union")) == ()


def test_classification_is_only_classification():
    # Guards never dispose: they return classes, the candidate is untouched.
    candidate = _candidate()
    assert classify_candidate(candidate) == ("numeric_endpoint",)
    assert candidate["object"] == "$1 trillion"


def test_flagged_endpoint_picks_slot():
    assert flagged_endpoint(_candidate(), "numeric_endpoint") == "object"
    assert flagged_endpoint(_candidate(subject="the ECB (European Central Bank)"),
                            "description_in_name") == "subject"
    assert flagged_endpoint(_candidate(subject="X", object_="x"), "self_loop") == "object"


# --- Jev decision client: grounding, menu scope, transport failure ----------

class _FakeTransport:
    def __init__(self, responses=None, errors=None):
        self.requests = []
        self._responses = list(responses or [])
        self._errors = list(errors or [])

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, dict(headers), body))
        if self._errors:
            raise self._errors.pop(0)
        return self._responses.pop(0)


def _step_body(choice="to_state", payload=None):
    return json.dumps({
        "answers": {"repair": {"type": "choice", "choice": choice,
                               "payload": payload or {}}},
        "usage": {"input_tokens": 90, "output_tokens": 20, "cost": 0.000004},
    }).encode()


def test_client_sends_claim_evidence_and_original_chunk():
    transport = _FakeTransport([_step_body()])
    client = JevDispatchClient(api_key="k", transport=transport)
    client.decide(_candidate(), "numeric_endpoint", CHUNK)
    payload = json.loads(transport.requests[0][2])
    assert payload["state"]["claim"] == "US Treasury borrows $1 trillion"
    assert payload["state"]["evidence"] == "US Treasury is borrowing $1 trillion short-term"
    assert payload["state"]["chunk"] == CHUNK.text  # grounding preserved
    # menu scoped to the guard class (issue #101 authoritative table)
    assert set(payload["questions"]["repair"]["criteria"]) == \
        set(CATEGORY_MENUS["numeric_endpoint"])


def test_client_malformed_answer_is_hard_abort():
    transport = _FakeTransport([json.dumps({
        "answers": {"repair": {"type": "choice", "choice": "to_state",
                               "payload": "not an object"}}}).encode()])
    client = JevDispatchClient(api_key="k", transport=transport)
    with pytest.raises(DispatchError):
        client.decide(_candidate(), "numeric_endpoint", CHUNK)


def test_client_choice_outside_menu_is_hard_abort():
    transport = _FakeTransport([_step_body(choice="decompose")])
    client = JevDispatchClient(api_key="k", transport=transport)
    with pytest.raises(DispatchError):
        client.decide(_candidate(), "numeric_endpoint", CHUNK)


def test_transport_failure_is_hard_abort():
    transport = _FakeTransport(errors=[OSError("connection refused")])
    client = JevDispatchClient(api_key="k", transport=transport)
    with pytest.raises(DispatchError):
        client.decide(_candidate(), "numeric_endpoint", CHUNK)


# --- executors: one test per menu step ---------------------------------------

def _step(choice, payload=None):
    return _FakeDispatcher([DispatchStep(choice, payload or {})])


def test_to_state_executor_produces_state_candidate():
    result = dispatch(_candidate(), "numeric_endpoint", CHUNK, _step(
        "to_state", {"entity": "US Treasury", "state_key": "short_term_borrowing",
                     "value": "$1 trillion", "unit": "usd", "as_of": "2026-09"}))
    assert result.step == "to_state" and not result.triples
    state = result.states[0]
    assert state["entity"] == "US Treasury"
    assert state["state_key"] == "short_term_borrowing"
    assert state["value"] == "$1 trillion"
    assert state["evidence"] == "US Treasury is borrowing $1 trillion short-term"
    assert state["source_ref"] == "s.md:chunk-1"  # provenance from the original


def test_reflexive_state_executor_produces_state_candidate():
    result = dispatch(_candidate(subject="Bank of Japan", object_="bank of japan",
                                 evidence="BOJ funds itself"),
                      "self_loop", CHUNK,
                      _step("reflexive_state", {"entity": "Bank of Japan",
                                                "state_key": "funding", "value": "self-funded",
                                                "unit": "", "as_of": "2026-09"}))
    assert result.states[0]["entity"] == "Bank of Japan"


def test_to_scope_executor_extends_scope_conditions():
    result = dispatch(_candidate(object_="elevated", evidence="yields remain elevated"),
                      "qualitative_endpoint", CHUNK,
                      _step("to_scope", {"scope": "yields since 2024"}))
    triple = result.triples[0]
    assert "yields since 2024" in triple["scope_conditions"]
    assert triple["source_ref"] == "s.md:chunk-1"


def test_decompose_executor_injects_triples_with_shared_evidence():
    result = dispatch(_candidate(subject="France and Canada building infrastructure and trade architecture bypassing the United States"),
                      "clause", CHUNK, _step("decompose", {"triples": [
                          {"subject": "France", "subject_type": "country",
                           "relation": "builds", "object": "infrastructure",
                           "object_type": "asset"},
                          {"subject": "Canada", "subject_type": "country",
                           "relation": "builds", "object": "trade architecture",
                           "object_type": "asset"},
                      ]}))
    assert len(result.triples) == 2
    assert all(t["evidence"] == "US Treasury is borrowing $1 trillion short-term"
               for t in result.triples)  # shared evidence from the original candidate
    assert all(t["source_ref"] == "s.md:chunk-1" for t in result.triples)


def test_decompose_per_member_executor():
    result = dispatch(_candidate(object_="the US, Japan and Australia"),
                      "list", CHUNK, _step("decompose_per_member", {"triples": [
                          {"subject": "US", "subject_type": "country",
                           "relation": "joins", "object": "sanctions",
                           "object_type": "policy"}]}))
    assert result.step == "decompose_per_member"
    assert result.triples[0]["subject"] == "US"


def test_pairwise_joint_edge_executor():
    result = dispatch(_candidate(object_="France and Canada"),
                      "compound_actor", CHUNK,
                      _step("pairwise_joint_edge", {"edges": [
                          {"subject": "France", "subject_type": "country",
                           "relation": "signs", "object": "deal",
                           "object_type": "agreement"},
                          {"subject": "Canada", "subject_type": "country",
                           "relation": "signs", "object": "deal",
                           "object_type": "agreement"}]}))
    assert len(result.triples) == 2
    assert result.triples[0]["evidence"] == result.triples[1]["evidence"]


def test_joint_with_scope_executor_applies_scope_to_edges():
    result = dispatch(_candidate(object_="the US, Japan and Australia"),
                      "list", CHUNK,
                      _step("joint_with_scope",
                            {"edges": [{"subject": "US", "subject_type": "country",
                                        "relation": "joins", "object": "pact",
                                        "object_type": "agreement"}],
                             "scope_conditions": "magnitude: $2bn"}))
    assert result.triples[0]["scope_conditions"] == "magnitude: $2bn"


def test_named_group_executor_replaces_compound_endpoint():
    result = dispatch(_candidate(subject="France and Canada"),
                      "compound_actor", CHUNK,
                      _step("named_group", {"group_name": "Franco-Canadian partnership",
                                            "group_type": "partnership"}))
    triple = result.triples[0]
    assert triple["subject"] == "Franco-Canadian partnership"
    assert triple["subject_type"] == "partnership"


def test_to_source_executor_flags_for_review_never_writes():
    result = dispatch(_candidate(object_="previous video"),
                      "deictic", CHUNK, _step("to_source", {"note": "links to source video 3"}))
    assert not result.triples and not result.states
    record = result.flagged[0]
    assert "source video 3" in record["reason"]
    assert record["guard_class"] == "deictic"


def test_normalize_name_executor_strips_qualifier_into_scope():
    result = dispatch(_candidate(subject="the ECB (European Central Bank)"),
                      "description_in_name", CHUNK,
                      _step("normalize_name", {"name": "ECB",
                                               "qualifier": "European Central Bank"}))
    triple = result.triples[0]
    assert triple["subject"] == "ECB"
    assert "European Central Bank" in triple["scope_conditions"]


def test_missing_endpoint_executor_repairs_self_loop():
    result = dispatch(_candidate(subject="Bank of Japan", object_="bank of japan"),
                      "self_loop", CHUNK,
                      _step("missing_endpoint", {"endpoint": "Japan",
                                                 "endpoint_type": "country"}))
    triple = result.triples[0]
    assert triple["object"] == "Japan" and triple["object_type"] == "country"


def test_pass_flagged_executor_surfaces_in_review_visibility():
    result = dispatch(_candidate(), "numeric_endpoint", CHUNK,
                      _step("pass_flagged", {}))
    assert not result.triples and not result.states
    assert result.flagged[0]["step"] == "pass_flagged"


def test_drop_noise_executor_drops_with_verdict():
    result = dispatch(_candidate(), "numeric_endpoint", CHUNK, _step("drop_noise", {}))
    assert result.verdict == "drop_noise"
    assert result.flagged


def test_every_menu_step_has_an_executor_outcome():
    # Guard against a table entry silently losing its executor.
    steps = {step for menu in CATEGORY_MENUS.values() for step in menu}
    assert steps == {"to_state", "to_scope", "drop_noise", "decompose",
                     "decompose_per_member", "pairwise_joint_edge",
                     "joint_with_scope", "named_group", "to_source",
                     "normalize_name", "missing_endpoint", "reflexive_state",
                     "pass_flagged"}


# --- invalid payloads: rejected log + flag, never a bad write ----------------

def test_to_state_payload_missing_fields_is_invalid_payload():
    result = dispatch(_candidate(), "numeric_endpoint", CHUNK,
                      _step("to_state", {"entity": "US Treasury"}))
    assert result.verdict == "invalid_payload"
    assert result.flagged  # nothing written, nothing silently lost


def test_state_payload_failing_validation_is_invalid_payload():
    result = dispatch(_candidate(), "numeric_endpoint", CHUNK,
                      _step("to_state", {"entity": "", "state_key": "debt",
                                         "value": "$1T", "as_of": "2026"}))
    assert result.verdict == "invalid_payload"


def test_decompose_with_only_invalid_members_is_invalid_payload():
    result = dispatch(_candidate(), "clause", CHUNK,
                      _step("decompose", {"triples": [{"subject": "France"}]}))
    assert result.verdict == "invalid_payload"


# --- orchestrator wiring -----------------------------------------------------

def _orchestrator(candidate_list, dispatcher=None, writer=None, novelty=None):
    from principle_graph.extraction import ExtractionRun
    from principle_graph.orchestrator import IngestOrchestrator

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.candidates.extend(candidate_list)
            run.completed_chunks.append("chunk-1")
            return run

    class _Store:
        def find_entities(self, name, entity_type):
            return []
        def search_similar(self, *a, **k):
            return []
        def structural_corroboration(self, *a, **k):
            return None

    class _Writer:
        unknown_relation_counts = {}
        unknown_domain_counts = {}
        unknown_entity_type_counts = {}

        def __init__(self):
            self.rejected = []
            self.states = []

        def get_edge(self, *a):
            return None

        def upsert_entity(self, entity):
            pass

        def upsert_edge(self, edge):
            pass

        def record_rejected(self, record):
            self.rejected.append(record)

        def upsert_state_event(self, state):
            self.states.append(state)

    class _Novelty:
        def classify(self, claims):
            from principle_graph.novelty import NoveltyVerdict
            return [NoveltyVerdict(claim, "novel", 0.9, {"novel": 0.9}) for claim in claims]

    w = writer if writer is not None else _Writer()
    return IngestOrchestrator(
        _Extractor(), store=_Store(), embedder=None,
        writer=w, novelty_filter=novelty or _Novelty(),
        dispatcher=dispatcher), w


def _source(tmp_path):
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nsome text\n", encoding="utf-8")
    return source


WELL_FORMED = _candidate(subject="Friedrich Merz", relation="leads",
                         object_="Christian Democratic Union",
                         evidence="Merz leads the CDU")


def test_happy_path_bypasses_dispatch_with_zero_calls(tmp_path):
    dispatcher = _FakeDispatcher()
    orch, _ = _orchestrator([WELL_FORMED], dispatcher=dispatcher)
    result = orch.run(_source(tmp_path))
    assert dispatcher.calls == []  # zero dispatch calls on the happy path
    assert result.stats.dispatch_bypassed == 1
    assert result.stats.dispatch_calls == 0


def test_repaired_to_state_lands_via_state_path(tmp_path):
    dispatcher = _FakeDispatcher([DispatchStep(
        "to_state", {"entity": "US Treasury", "state_key": "short_term_borrowing",
                     "value": "$1 trillion", "unit": "usd", "as_of": "2026-09"})])
    writer = _orchestrator([_candidate()], dispatcher=dispatcher)[1]
    orch, _ = _orchestrator([_candidate()], dispatcher=dispatcher, writer=writer)
    result = orch.run(_source(tmp_path))
    assert result.stats.dispatch_step_counts == {"to_state": 1}
    assert result.stats.states_committed == 1
    assert writer.states[0].entity == "US Treasury"
    assert writer.states[0].value == "$1 trillion"
    # the numeric-endpoint triple never becomes an edge
    assert result.delta.new_edges == []


def test_drop_noise_lands_in_rejected_log_with_verdict(tmp_path):
    dispatcher = _FakeDispatcher([DispatchStep("drop_noise", {})])
    orch, writer = _orchestrator([_candidate()], dispatcher=dispatcher)
    result = orch.run(_source(tmp_path))
    assert result.stats.dispatch_dropped == 1
    assert result.stats.dispatch_step_counts == {"drop_noise": 1}
    assert len(writer.rejected) == 1
    assert writer.rejected[0]["verdict"] == "drop_noise"
    assert writer.rejected[0]["guard_class"] == "numeric_endpoint"
    assert result.delta.new_edges == []


def test_invalid_payload_lands_in_rejected_log_never_written(tmp_path):
    dispatcher = _FakeDispatcher([DispatchStep("to_state", {"entity": "US Treasury"})])
    orch, writer = _orchestrator([_candidate()], dispatcher=dispatcher)
    result = orch.run(_source(tmp_path))
    assert writer.rejected[0]["verdict"] == "invalid_payload"
    assert result.delta.new_edges == []
    assert writer.states == []


def test_repaired_triple_reenters_stream_before_novelty_gate(tmp_path):
    seen = []

    class _Novelty:
        def classify(self, claims):
            from principle_graph.novelty import NoveltyVerdict, render_claim
            seen.extend(render_claim(claim) for claim in claims)
            return [NoveltyVerdict(claim, "novel", 0.9, {"novel": 0.9}) for claim in claims]

    dispatcher = _FakeDispatcher([DispatchStep("normalize_name", {
        "name": "ECB", "qualifier": "European Central Bank"})])
    orch, _ = _orchestrator(
        [_candidate(subject="the ECB (European Central Bank)", relation="sets",
                    object_="rates", evidence="the ECB sets rates",
                    object_type="concept")],
        dispatcher=dispatcher, novelty=_Novelty())
    result = orch.run(_source(tmp_path))
    assert seen == ["ECB sets rates"]  # repaired shape, before admission
    assert len(result.delta.new_edges) == 1
    assert result.delta.new_edges[0].subject == "ECB"


def test_dispatcher_unreachable_hard_aborts_ingest(tmp_path):
    dispatcher = _FakeDispatcher(error=DispatchError("connection refused"))
    orch, writer = _orchestrator([_candidate()], dispatcher=dispatcher)
    with pytest.raises(DispatchError):
        orch.run(_source(tmp_path))
    assert writer.states == [] and writer.rejected == []  # no partial state


def test_stats_render_includes_dispatch_aggregates(tmp_path):
    dispatcher = _FakeDispatcher([
        DispatchStep("to_state", {"entity": "US Treasury",
                                  "state_key": "borrowing", "value": "$1T",
                                  "unit": "", "as_of": "2026"}),
        DispatchStep("drop_noise", {}),
    ])
    orch, _ = _orchestrator([_candidate(),
                             _candidate(relation="borrows", object_="€2 trillion",
                                        evidence="ECB reported €2 trillion")],
                            dispatcher=dispatcher)
    result = orch.run(_source(tmp_path))
    transcript = result.stats.render()
    assert "dispatch: 2 calls, 0 bypassed, 1 dropped" in transcript
    assert "to_state=1" in transcript and "drop_noise=1" in transcript


def test_dispatch_opt_out_skips_seam_entirely(tmp_path):
    orch, _ = _orchestrator([_candidate()], dispatcher=None)
    result = orch.run(_source(tmp_path))
    # legacy behavior preserved when the dispatcher is not injected
    assert result.stats.dispatch_calls == 0 and result.stats.dispatch_bypassed == 0
    assert len(result.delta.new_edges) == 1


# --- fix round 1: Mode-2 review visibility + skipped-member audit ------------

def test_pass_flagged_flag_record_carries_claim_and_evidence():
    result = dispatch(_candidate(), "numeric_endpoint", CHUNK,
                      _step("pass_flagged", {}))
    reason = result.flagged[0]["reason"]
    assert "US Treasury" in reason and "borrows" in reason
    assert "$1 trillion" in reason  # claim is visible, not just a count
    assert "US Treasury is borrowing $1 trillion short-term" in reason


def test_to_source_reason_carries_claim_evidence_and_note():
    result = dispatch(_candidate(subject="ECB", relation="warns",
                                 object_="policy", evidence="ECB warns on policy"),
                      "deictic", CHUNK,
                      _step("to_source", {"note": "meta commentary"}))
    reason = result.flagged[0]["reason"]
    assert "meta commentary" in reason
    assert "ECB" in reason and "warns" in reason
    assert "ECB warns on policy" in reason


def test_partially_invalid_decompose_keeps_valid_and_audits_skipped_members():
    good = {"subject": "France", "subject_type": "country", "relation": "trades",
            "object": "Germany", "object_type": "country"}
    bad = {"subject": "France"}  # missing relation/object
    result = dispatch(_candidate(), "clause", CHUNK,
                      _step("decompose", {"triples": [good, bad]}))
    assert not isinstance(result, Dropped)
    assert len(result.triples) == 1
    assert len(result.rejected) == 1
    record = result.rejected[0]
    assert record["verdict"] == "invalid_payload"
    assert record["decision"] == "rejected"
    assert "member 1" in record["reason"]


def test_partially_invalid_edges_keeps_valid_and_audits_skipped_members():
    good = {"subject": "US", "subject_type": "country", "relation": "sanctions",
            "object": "Iran", "object_type": "country"}
    bad = {"relation": "sanctions"}  # missing subject/object
    result = dispatch(_candidate(), "clause", CHUNK,
                      _step("pairwise_joint_edge", {"edges": [good, bad]}))
    assert len(result.triples) == 1
    assert len(result.rejected) == 1
    assert "member 1" in result.rejected[0]["reason"]


def test_skipped_members_land_in_rejected_log_and_stats(tmp_path):
    good = {"subject": "France", "subject_type": "country", "relation": "trades",
            "object": "Germany", "object_type": "country"}
    bad = {"subject": "France"}
    dispatcher = _FakeDispatcher([DispatchStep("decompose", {"triples": [good, bad]})])
    orch, writer = _orchestrator([_candidate()], dispatcher=dispatcher)
    result = orch.run(_source(tmp_path))
    assert len(writer.rejected) == 1
    assert writer.rejected[0]["verdict"] == "invalid_payload"
    assert result.stats.dispatch_step_counts["skipped_members"] == 1
    assert len(result.delta.new_edges) == 1  # the valid member still flows


def test_stats_render_shows_all_bypassed_run(tmp_path):
    orch, _ = _orchestrator([WELL_FORMED], dispatcher=_FakeDispatcher())
    result = orch.run(_source(tmp_path))
    assert "dispatch: 0 calls, 1 bypassed, 0 dropped" in result.stats.render()
