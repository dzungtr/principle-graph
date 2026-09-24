"""Two-pass scan tests (issue #102): database-free fakes, recorded LLM responses."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from principle_graph.extraction import SequentialExtractor
from principle_graph.extraction_contract import chunk_markdown
from principle_graph.label_registry import load_label_registry
from principle_graph.orchestrator import IngestOrchestrator
from principle_graph.reduction import InMemoryGraph
from principle_graph.resolution import Entity
from principle_graph.scan import (
    RosterEntry,
    ScanError,
    ScanResult,
    SourceScanner,
    append_proposed_verbs,
    normalize_verb,
    scan_source,
)


@dataclass
class ToolUse:
    type: str
    name: str
    input: dict


@dataclass
class Response:
    content: list


@dataclass
class FakeClient:
    responses: list
    calls: list = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            return Response([])
        return self.responses.pop(0)


@dataclass
class FakeScanStore:
    """Resolution store + list_relation_types seam with canned rows."""
    entities: list = field(default_factory=list)
    relation_types: tuple = ()

    def find_entities(self, name, entity_type):
        return [e for e in self.entities if e.type == entity_type]

    def search_similar(self, embedding, entity_type, limit=10):
        return []

    def structural_corroboration(self, entity, neighbors):
        return 0

    def list_relation_types(self):
        return self.relation_types


def _registry(tmp_path):
    path = tmp_path / "relation-registry.yaml"
    path.write_text(
        "version: 1\n"
        "labels:\n"
        "  reduces:\n"
        "    aliases: [lowers, cuts]\n"
        "    description: pushes down.\n"
        "proposed:\n"
        "  labels: {}\n",
        encoding="utf-8",
    )
    return path


def _scan_response(verbs=(), entities=()):
    return Response([ToolUse("tool_use", "scan_candidates",
                             {"verbs": list(verbs), "entities": list(entities)})])


def _cluster_response(verbs=(), entities=()):
    return Response([ToolUse("tool_use", "consolidate_candidates",
                             {"verbs": list(verbs), "entities": list(entities)})])


def _chunks():
    return chunk_markdown("# A\nrates reduce borrowing.\n\n# B\ninflation follows.\n", "book")


def _scan(tmp_path, client, store=None, registry_path=None):
    return scan_source(
        _chunks(), client,
        relation_registry=load_label_registry(_registry(tmp_path)),
        store=store,
        registry_path=registry_path or _registry(tmp_path),
    )


# --- batched lightweight inventory -------------------------------------------

def test_scan_batches_chunks_not_per_chunk(tmp_path):
    client = FakeClient([_scan_response(verbs=["reduces"]), _scan_response()])
    result = _scan(tmp_path, client)
    assert len(client.calls) == 1  # 2 chunks, batch_size 4 → one batched call
    assert result.scan_calls == 1
    assert "rates reduce borrowing" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["tools"][0]["name"] == "scan_candidates"


def test_scan_uses_one_call_per_batch(tmp_path):
    client = FakeClient([_scan_response(), _scan_response()])
    scan_source(
        _chunks(), client, batch_size=1,
        relation_registry=load_label_registry(_registry(tmp_path)),
        registry_path=_registry(tmp_path),
    )
    assert len(client.calls) == 2


# --- deterministic consolidation: verbs --------------------------------------

def test_registry_alias_collapses_into_verb_menu(tmp_path):
    client = FakeClient([_scan_response(verbs=["Cuts"])])
    result = _scan(tmp_path, client)
    assert result.verb_menu == ("reduces",)
    assert result.consolidation_calls == 0  # fully anchored: no LLM clustering call
    assert result.staged_verbs == ()


def test_live_graph_verb_anchors_via_list_relation_types(tmp_path):
    store = FakeScanStore(relation_types=("BOOSTS",))
    client = FakeClient([_scan_response(verbs=["boosts"])])
    result = scan_source(
        _chunks(), client,
        relation_registry=load_label_registry(_registry(tmp_path)),
        store=store, registry_path=_registry(tmp_path),
    )
    assert "boosts" in result.verb_menu
    assert result.consolidation_calls == 0


def test_unmatched_verbs_get_one_clustering_call_and_stage_new_verbs(tmp_path):
    registry_path = _registry(tmp_path)
    client = FakeClient([
        _scan_response(verbs=["reduces", "weaponizes"]),
        _cluster_response(verbs=[{"canonical": "weaponizes", "aliases": ["arms"]}]),
    ])
    result = scan_source(
        _chunks(), client,
        relation_registry=load_label_registry(registry_path),
        registry_path=registry_path,
    )
    assert result.consolidation_calls == 1
    assert "reduces" in result.verb_menu and "weaponizes" in result.verb_menu
    assert result.staged_verbs == ("weaponizes",)
    # Auto-append to the registry's proposed: staging (git is the review gate).
    reloaded = load_label_registry(registry_path)
    assert reloaded.is_known("weaponizes")  # participates in canonicalization immediately
    assert "weaponizes" in reloaded.staged_labels()
    # Idempotent: re-appending does not duplicate.
    assert append_proposed_verbs(registry_path, ["weaponizes"]) == ()
    assert load_label_registry(registry_path).staged_labels() == ("weaponizes",)


def test_clustered_known_verb_is_not_staged(tmp_path):
    client = FakeClient([
        _scan_response(verbs=["cuts"]),
        _cluster_response(verbs=[{"canonical": "reduces", "aliases": ["cuts"]}]),
    ])
    result = _scan(tmp_path, client)
    assert result.staged_verbs == ()
    assert "reduces" in result.verb_menu


# --- deterministic consolidation: entities -----------------------------------

def test_graph_entity_anchors_via_resolution_lookup(tmp_path):
    store = FakeScanStore(entities=[Entity("e1", "Friedrich Merz", "person", aliases=("Merz",))])
    client = FakeClient([_scan_response(entities=[{"name": "Merz", "entity_type": "person"}])])
    result = scan_source(
        _chunks(), client,
        relation_registry=load_label_registry(_registry(tmp_path)),
        store=store, registry_path=_registry(tmp_path),
    )
    assert result.entity_roster == (RosterEntry("Friedrich Merz", "person", ("Merz",)),)
    assert result.consolidation_calls == 0


def test_unmatched_entities_cluster_with_alias_hints(tmp_path):
    client = FakeClient([
        _scan_response(entities=[{"name": "BOJ", "entity_type": "organization"}]),
        _cluster_response(entities=[{"canonical": "Bank of Japan", "entity_type": "organization",
                                     "aliases": ["BOJ"]}]),
    ])
    result = _scan(tmp_path, client)
    assert result.consolidation_calls == 1
    assert RosterEntry("Bank of Japan", "organization", ("BOJ",)) in result.entity_roster


# --- failure semantics --------------------------------------------------------

def test_malformed_scan_response_raises_scan_error(tmp_path):
    client = FakeClient([Response([])])
    with pytest.raises(ScanError):
        _scan(tmp_path, client)


def test_malformed_consolidation_response_raises_scan_error(tmp_path):
    client = FakeClient([_scan_response(verbs=["oddity"]), Response([])])
    with pytest.raises(ScanError):
        _scan(tmp_path, client)


# --- prompt injection (injection point from #100) -----------------------------

def test_chunk_prompt_carries_verb_menu_and_entity_roster():
    chunks = chunk_markdown("# One\nrates reduce borrowing.\n# Two\nmore.\n", "book")
    client = FakeClient([Response([]), Response([])])
    roster = [RosterEntry("Friedrich Merz", "person", ("Merz",)).render()]
    run = SequentialExtractor(client).run(chunks, verb_menu=("reduces", "weaponizes"),
                                          entity_roster=tuple(roster))
    assert run.completed_chunks == ["chunk-1", "chunk-2"]
    for call in client.calls:
        content = call["messages"][0]["content"]
        before, after = content.split("\n\nchunk text:\n", 1)
        assert "verb menu: reduces, weaponizes" in before
        assert "entity roster: Friedrich Merz (person; aka: Merz)" in before
        assert content.index("section_path") < content.index("verb menu") < content.index("chunk text")


def test_prompt_without_scan_outputs_is_unchanged():
    chunks = chunk_markdown("# One\nrates reduce borrowing.\n", "book")
    client = FakeClient([Response([])])
    SequentialExtractor(client).run(chunks)
    assert client.calls[0]["messages"][0]["content"].endswith(
        "\n\nchunk text:\n# One\nrates reduce borrowing.")
    assert "verb menu" not in client.calls[0]["messages"][0]["content"]


# --- orchestrator wiring -------------------------------------------------------

@dataclass
class RecordingScanner:
    result: object
    calls: list = field(default_factory=list)

    def scan(self, chunks):
        self.calls.append(list(chunks))
        return self.result


class BoomScanner:
    def scan(self, chunks):
        raise ScanError("scan backend down")


def _triple(source_ref):
    return {
        "subject": "rates", "subject_type": "concept", "relation": "reduces",
        "object": "borrowing", "object_type": "concept", "confidence": .9,
        "evidence": "rates reduce borrowing", "scope_conditions": "",
        "source_ref": source_ref,
    }


def _write_source(tmp_path):
    path = tmp_path / "src.md"
    path.write_text("# A\nrates reduce borrowing.\n", encoding="utf-8")
    return path


@dataclass
class RecordingExtractor:
    calls: list = field(default_factory=list)

    def run(self, chunks, *, verb_menu=(), entity_roster=()):
        self.calls.append((list(chunks), verb_menu, tuple(entity_roster)))
        from principle_graph.extraction import ExtractionRun
        run = ExtractionRun()
        for chunk in chunks:
            run.candidates.append(_triple(chunk.source_ref))
            run.completed_chunks.append(chunk.id)
        return run


def test_orchestrator_scans_before_extraction_and_injects_outputs(tmp_path):
    chunks = chunk_markdown("# A\nrates reduce borrowing.\n", Path(_write_source(tmp_path)).name)
    scanner = RecordingScanner(ScanResult(
        verb_menu=("reduces", "weaponizes"),
        entity_roster=(RosterEntry("Friedrich Merz", "person", ("Merz",)),),
        scan_calls=1, consolidation_calls=1))
    extractor = RecordingExtractor()
    orch = IngestOrchestrator(extractor, FakeScanStore(), None, InMemoryGraph(),
                              scanner=scanner)
    result = orch.run(_write_source(tmp_path), input_fn=lambda _: "approve")
    assert scanner.calls and extractor.calls  # scan ran
    assert scanner.calls[0][0].id == chunks[0].id
    assert extractor.calls[0][1] == ("reduces", "weaponizes")
    assert extractor.calls[0][2] == ("Friedrich Merz (person; aka: Merz)",)
    assert result.stats.scan_calls == 1 and result.stats.consolidation_calls == 1
    assert "scan calls: 1 (consolidation calls: 1)" in result.stats.render()


def test_scan_failure_aborts_ingest_with_no_partial_state(tmp_path):
    graph = InMemoryGraph()
    extractor = RecordingExtractor()
    orch = IngestOrchestrator(extractor, FakeScanStore(), None, graph, scanner=BoomScanner())
    with pytest.raises(ScanError):
        orch.run(_write_source(tmp_path), input_fn=lambda _: "approve")
    assert not extractor.calls  # extraction never started
    assert graph.edges == {} and graph.entities == []  # no partial state


def test_orchestrator_without_scanner_keeps_one_phase_shape(tmp_path):
    chunks = chunk_markdown("# A\nrates reduce borrowing.\n", Path(_write_source(tmp_path)).name)
    client = FakeClient([Response([ToolUse("tool_use", "propose_triple",
                                           _triple(chunks[0].source_ref))])])
    orch = IngestOrchestrator(SequentialExtractor(client), FakeScanStore(), None, InMemoryGraph())
    result = orch.run(_write_source(tmp_path), input_fn=lambda _: "approve")
    assert result.stats.scan_calls == 0
    assert "verb menu" not in client.calls[0]["messages"][0]["content"]


def test_normalize_verb_snake_cases():
    assert normalize_verb("Weaponizes Supply") == "weaponizes_supply"
    assert normalize_verb("  Cuts ") == "cuts"
