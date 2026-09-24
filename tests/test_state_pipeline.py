"""propose_state extraction and end-to-end orchestration wiring (issue #98).

Database-free: recording client fakes and an in-memory writer with a state
ledger seam (prior art: tests/test_novelty.py orchestrator fixtures,
tests/test_extraction.py).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from principle_graph.extraction import PROPOSE_STATE_TOOL, SequentialExtractor
from principle_graph.extraction_contract import Chunk, ContractError, validate_state
from principle_graph.extraction import ExtractionRun
from principle_graph.fanout import query_directions, render_markdown, seed_states
from principle_graph.label_registry import (
    default_state_registry_path,
    load_label_registry,
)
from principle_graph.orchestrator import IngestOrchestrator
from principle_graph.reduction import GraphEntity, InMemoryGraph
from principle_graph.resolution import Entity, Resolution
from principle_graph.review import GraphDelta, ReviewResult
from principle_graph.state import StateEvent


def _chunk():
    return Chunk("chunk-1", "Merz's approval is 42%.", (), (), "note-1:chunk-1")


def _tool_block(name, payload):
    return SimpleNamespace(type="tool_use", name=name, input=payload)


def _response(*blocks):
    return SimpleNamespace(content=list(blocks))


def _state_input(**overrides):
    values = dict(
        entity="Friedrich Merz", entity_type="person", state_key="approval_rating",
        value="42", unit="percent", as_of="2026-09-01", confidence=0.9,
        evidence="Polls put Merz at 42%.", scope_conditions="",
        source_ref="note-1:chunk-1",
    )
    values.update(overrides)
    return values


class _FakeClient:
    def __init__(self, blocks):
        self.blocks = list(blocks)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _response(*self.blocks)


# --- candidate schema (state-candidate handoff, #95 Handoffs) ---------------

def test_validate_state_accepts_numeric_and_qualitative_values():
    numeric = validate_state(_state_input())
    assert numeric["value"] == "42"
    qualitative = validate_state(_state_input(state_key="yield_level",
                                              value="elevated", unit=""))
    assert qualitative["value"] == "elevated"


def test_validate_state_rejects_missing_required_fields():
    payload = _state_input()
    del payload["state_key"]
    with pytest.raises(ContractError):
        validate_state(payload)


def test_validate_state_rejects_bad_snake_case_state_key():
    with pytest.raises(ContractError):
        validate_state(_state_input(state_key="Approval Rating"))


def test_validate_state_normalizes_numeric_values_to_strings():
    assert validate_state(_state_input(value=42))["value"] == "42"


# --- extraction tool ---------------------------------------------------------

def test_propose_state_tool_schema_carries_state_fields():
    properties = PROPOSE_STATE_TOOL["input_schema"]["properties"]
    assert {"entity", "entity_type", "state_key", "value", "unit", "as_of",
            "confidence", "evidence", "scope_conditions", "source_ref"} \
        <= set(properties)


def test_extractor_collects_valid_state_candidates():
    client = _FakeClient([
        _tool_block("propose_state", _state_input()),
        _tool_block("propose_triple", {
            "subject": "Merz", "subject_type": "person", "relation": "leads",
            "object": "CDU", "object_type": "organization", "confidence": 0.8,
            "evidence": "e", "scope_conditions": "since 2025", "source_ref": "note-1:chunk-1",
        }),
    ])
    run = SequentialExtractor(client).run([_chunk()])
    assert [c["state_key"] for c in run.state_candidates] == ["approval_rating"]
    assert len(run.candidates) == 1  # triple tool unaffected
    # Both tools offered in the same extraction call.
    assert [t["name"] for t in client.calls[0]["tools"]] == \
        ["propose_triple", "propose_state"]


def test_extractor_rejects_invalid_state_candidate_into_rejected_log():
    client = _FakeClient([_tool_block("propose_state", _state_input(confidence=5))])
    run = SequentialExtractor(client).run([_chunk()])
    assert run.state_candidates == []
    assert len(run.rejected) == 1
    assert "confidence" in run.rejected[0]["reason"]


def test_extractor_rejects_state_with_mismatched_source_ref():
    client = _FakeClient([_tool_block("propose_state",
                                      _state_input(source_ref="other:chunk-1"))])
    run = SequentialExtractor(client).run([_chunk()])
    assert run.state_candidates == []
    assert "source_ref" in run.rejected[0]["reason"]


# --- in-memory writer: state ledger seam (ADR-0007 two-layer) ----------------

def _state(**overrides):
    values = dict(
        entity="Friedrich Merz", entity_type="person", state_key="approval_rating",
        value="42", unit="percent", as_of="2026-09-01", confidence=0.9,
        evidence="e", scope_conditions="", source_ref="note-1:chunk-1",
    )
    values.update(overrides)
    return StateEvent(**values)


def test_in_memory_writer_keeps_state_rows_and_denormalized_map():
    graph = InMemoryGraph()
    graph.upsert_state_event(_state())
    graph.upsert_state_event(_state())  # same source: keep-first no-op
    assert len(graph.state_events) == 1
    graph.upsert_state_event(_state(value="38", as_of="2026-10-01",
                                    source_ref="note-2:chunk-1"))
    assert len(graph.state_events) == 2  # disagreement rows persist
    assert graph.current_state("Friedrich Merz")["approval_rating"]["value"] == "38"


def test_in_memory_writer_states_for_fanout():
    graph = InMemoryGraph()
    graph.upsert_state_event(_state())
    states = graph.states_for(Entity("t:merz", "Friedrich Merz", "person"))
    assert [(s.state_key, s.value) for s in states] == [("approval_rating", "42")]


# --- state-key canonicalization (never-reject) -------------------------------

def test_state_registry_canonicalizes_aliases_and_flags_unknown():
    registry = load_label_registry(default_state_registry_path())
    assert registry.canonical_for("popularity") == "approval_rating"
    assert registry.is_known("approval_rating")
    assert not registry.is_known("shoe_size")  # passes through flagged, not rejected


# --- orchestrator wiring ------------------------------------------------------

class _Store:
    def find_entities(self, name, entity_type):
        return []

    def search_similar(self, *a, **k):
        return []

    def structural_corroboration(self, *a, **k):
        return None


class _StateLedgerWriter:
    """Fake GraphWriter with the state seam; records states like rows."""

    unknown_relation_counts: dict = {}
    unknown_domain_counts: dict = {}

    def __init__(self):
        self.entities: list[GraphEntity] = []
        self.state_events: list[StateEvent] = []

    def get_edge(self, *a):
        return None

    def upsert_entity(self, entity):
        if entity not in self.entities:
            self.entities.append(entity)

    def upsert_edge(self, edge):
        pass

    def upsert_state_event(self, event: StateEvent) -> None:
        self.state_events.append(event)


class _ApprovalFilter:
    """Novelty filter approving triples and states alike."""

    def classify(self, claims):
        from principle_graph.novelty import NoveltyVerdict
        return [NoveltyVerdict(claim, "novel", 0.9, {}) for claim in claims]


def _run_orchestrator(tmp_path, monkeypatch, writer, filter_):
    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.state_candidates.append(_state_input())
            run.completed_chunks.append("chunk-1")
            return run

    source = tmp_path / "s.md"
    source.write_text("# hi\n\nsome text\n", encoding="utf-8")
    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=writer, novelty_filter=filter_)
    monkeypatch.setattr("principle_graph.orchestrator.review_and_commit",
                        lambda delta, writer_, **kw: ReviewResult(approved=delta,
                                                                  rejected=[]))
    return orch.run(source)


def test_orchestrator_commits_state_after_review_approval(tmp_path, monkeypatch):
    writer = _StateLedgerWriter()
    result = _run_orchestrator(tmp_path, monkeypatch, writer, _ApprovalFilter())
    assert [e.state_key for e in writer.state_events] == ["approval_rating"]
    assert result.stats.states_committed == 1
    assert result.stats.state_novelty_calls == 1


def test_orchestrator_skips_states_on_rejected_review(tmp_path, monkeypatch):
    writer = _StateLedgerWriter()
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nx\n", encoding="utf-8")

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.state_candidates.append(_state_input())
            run.completed_chunks.append("chunk-1")
            return run

    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=writer, novelty_filter=_ApprovalFilter())
    monkeypatch.setattr(
        "principle_graph.orchestrator.review_and_commit",
        lambda delta, writer_, **kw: ReviewResult(GraphDelta(),
                                                  [{"reason": "rejected by reviewer"}]))
    result = orch.run(source)
    assert writer.state_events == []
    assert result.stats.states_committed == 0


# --- fan-out state output -----------------------------------------------------

class _StateGraph:
    """Minimal QueryGraph + state seam for fan-out tests."""

    def __init__(self):
        from principle_graph.resolution import Entity
        self.entity = Entity("t:merz", "Friedrich Merz", "person", aliases=())
        from principle_graph.review import GraphEdge
        self.edge = GraphEdge("Friedrich Merz", "LEADS", "CDU", 0.8,
                              "note-1:chunk-1", ("e",), "")

    def entities(self):
        return [self.entity]

    def edges_for(self, entity):
        return [self.edge]

    def states_for(self, entity):
        return [SimpleNamespace(state_key="approval_rating", value="42",
                                unit="percent", as_of="2026-09-01", confidence=0.9)]


def test_query_directions_output_includes_seed_states():
    seeds, directions = query_directions("Friedrich Merz", _StateGraph())
    assert directions  # unchanged direction behavior
    graph = _StateGraph()
    states = {seed.entity.name: list(graph.states_for(seed.entity)) for seed in seeds}
    assert states["Friedrich Merz"][0].state_key == "approval_rating"


def test_render_markdown_includes_seed_states():
    seeds, directions = query_directions("Friedrich Merz", _StateGraph())
    graph = _StateGraph()
    states = {seed.entity.name: list(graph.states_for(seed.entity)) for seed in seeds}
    text = render_markdown("Friedrich Merz", seeds, directions, states=states)
    assert "approval_rating=42 percent" in text
    assert "as_of 2026-09-01" in text


def test_render_markdown_without_states_omits_state_lines():
    seeds, directions = query_directions("Friedrich Merz", _StateGraph())
    text = render_markdown("Friedrich Merz", seeds, directions)
    assert "approval_rating" not in text


def test_seed_states_degrades_without_state_seam():
    # A graph whose class simply lacks the seam: simulate via a bare object.
    class _Bare:
        entities = _StateGraph().entities
        def edges_for(self, entity):
            return []
    assert seed_states(query_directions("Friedrich Merz", _Bare())[0], _Bare()) == {}


def test_orchestrator_flags_unknown_state_keys_but_commits(tmp_path, monkeypatch):
    writer = _StateLedgerWriter()
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nx\n", encoding="utf-8")

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.state_candidates.append(_state_input(state_key="shoe_size"))
            run.completed_chunks.append("chunk-1")
            return run

    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=writer, novelty_filter=_ApprovalFilter())
    monkeypatch.setattr("principle_graph.orchestrator.review_and_commit",
                        lambda delta, writer_, **kw: ReviewResult(approved=delta,
                                                                  rejected=[]))
    result = orch.run(source)
    # Never-reject: the unknown key passes through flagged.
    assert result.stats.unknown_state_keys == (("shoe_size", 1),)
    assert [e.state_key for e in writer.state_events] == ["shoe_size"]
    assert "unknown state keys passed through uncanonicalized: shoe_size=1" \
        in result.stats.render()


def test_orchestrator_filters_noise_states(tmp_path, monkeypatch):
    writer = _StateLedgerWriter()
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nx\n", encoding="utf-8")

    class _NoiseStateFilter:
        def classify(self, claims):
            from principle_graph.novelty import NoveltyVerdict
            return [NoveltyVerdict(claim, "noise", 0.9, {}) for claim in claims]

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.state_candidates.append(_state_input())
            run.completed_chunks.append("chunk-1")
            return run

    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=writer, novelty_filter=_NoiseStateFilter())
    monkeypatch.setattr("principle_graph.orchestrator.review_and_commit",
                        lambda delta, writer_, **kw: ReviewResult(approved=delta,
                                                                  rejected=[]))
    result = orch.run(source)
    assert writer.state_events == []
    assert result.stats.states_filtered_noise == 1
    assert result.stats.states_committed == 0


def test_orchestrator_state_novelty_failure_aborts_before_any_commit(tmp_path, monkeypatch):
    """AC4 gate-failure parity with the triple side (PR #108 P2-3): a novelty
    transport failure aborts the whole ingest — nothing is committed."""
    writer = _StateLedgerWriter()
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nx\n", encoding="utf-8")

    class _BrokenFilter:
        def classify(self, claims):
            raise RuntimeError("novelty gateway down")

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.state_candidates.append(_state_input())
            run.completed_chunks.append("chunk-1")
            return run

    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=writer, novelty_filter=_BrokenFilter())
    monkeypatch.setattr("principle_graph.orchestrator.review_and_commit",
                        lambda delta, writer_, **kw: ReviewResult(approved=delta,
                                                                  rejected=[]))
    with pytest.raises(RuntimeError, match="novelty gateway down"):
        orch.run(source)
    assert writer.state_events == []


def test_orchestrator_resolves_state_entity_through_entity_registry(tmp_path, monkeypatch):
    """P1-2: a state candidate typed with a registry alias (politician →
    person) commits against the canonical entity type, never a fragment."""
    from principle_graph.label_registry import (
        LabelEntry,
        LabelRegistry,
        default_entity_registry_path,
        load_label_registry,
    )
    registry = load_label_registry(default_entity_registry_path())
    assert registry.canonical_for("politician") == "person"
    writer = _StateLedgerWriter()
    source = tmp_path / "s.md"
    source.write_text("# hi\n\nx\n", encoding="utf-8")

    class _Extractor:
        def run(self, chunks):
            run = ExtractionRun()
            run.state_candidates.append(_state_input(entity_type="politician"))
            run.completed_chunks.append("chunk-1")
            return run

    orch = IngestOrchestrator(_Extractor(), store=_Store(), embedder=None,
                              writer=writer, novelty_filter=_ApprovalFilter(),
                              entity_registry=registry)
    monkeypatch.setattr("principle_graph.orchestrator.review_and_commit",
                        lambda delta, writer_, **kw: ReviewResult(approved=delta,
                                                                  rejected=[]))
    result = orch.run(source)
    assert result.stats.states_committed == 1
    assert [e.entity_type for e in writer.state_events] == ["person"]
