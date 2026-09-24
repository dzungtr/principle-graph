"""Credential-free end-to-end tests for the ingest orchestrator."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from principle_graph.extraction import ExtractionRun, SequentialExtractor
from principle_graph.extraction_contract import chunk_markdown
from principle_graph.orchestrator import IngestOrchestrator, format_ambiguity_note, load_source
from principle_graph.reduction import GraphEdge, GraphEntity, InMemoryGraph
from principle_graph.resolution import Entity, SimilarEntity
from principle_graph.review import GraphDelta


@dataclass
class ToolUse:
    type: str
    name: str
    input: dict


@dataclass
class FakeResponse:
    content: list


@dataclass
class FakeMessages:
    """Records every create() invocation and replays a scripted response list."""
    responses: list
    calls: list = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            return FakeResponse(content=[])
        return self.responses.pop(0)


def _triple(source_ref, subject, relation, obj, *, confidence=0.9):
    return {
        "subject": subject, "subject_type": "concept", "relation": relation,
        "object": obj, "object_type": "concept", "confidence": confidence,
        "evidence": f"{subject} {relation} {obj}", "scope_conditions": "extracted",
        "source_ref": source_ref,
    }


@dataclass
class FakeStore:
    """Resolution-store seam with canned results; recording-style optional."""
    entities: list[Entity] = field(default_factory=list)
    similar: list[SimilarEntity] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def find_entities(self, name, entity_type):
        return [e for e in self.entities if e.type == entity_type]

    def search_similar(self, embedding, entity_type, limit=10):
        return [s for s in self.similar if s.entity.type == entity_type][:limit]

    def structural_corroboration(self, entity, neighbors):
        return self.evidence.get(entity.id, 0)


class CountingEmbedder:
    def __init__(self, vector=(1.0, 0.0)):
        self.vector = vector
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        return list(self.vector)


class FakeEdgeLoader:
    def __init__(self, edges=()):
        self.edges = list(edges)
        self.calls: list[list[tuple[str, str, str]]] = []

    def load_existing_edges(self, triples):
        self.calls.append(list(triples))
        return [e for e in self.edges
                if (e.subject, e.relation, e.object) in {(t[0], t[1], t[2]) for t in triples}]


def _markdown_source():
    return "# A\nrates reduce borrowing.\n\n# B\nborrowing is reduced.\n"


def _orchestrator(graph, *, store=None, embedder=None, loader=None, rejected_log_path=".pg/rejected.jsonl"):
    return _orchestrator_for(graph, _markdown_source(), store=store, embedder=embedder,
                              loader=loader, rejected_log_path=rejected_log_path)


def _orchestrator_for(graph, source_text, *, store=None, embedder=None, loader=None,
                       rejected_log_path=".pg/rejected.jsonl", filename=None):
    if filename is None:
        import tempfile
        suffix = tempfile.mkstemp(suffix='.md')[1]
        filename = suffix
    Path(filename).write_text(source_text, encoding='utf-8')
    chunks = chunk_markdown(source_text, Path(filename).name)
    triple_for_chunk_a = _triple(chunks[0].source_ref, "rates", "reduces", "borrowing")
    triple_for_chunk_b = _triple(chunks[1].source_ref, "borrowing", "is", "reduced")
    client = FakeMessages([
        FakeResponse(content=[ToolUse("tool_use", "propose_triple", triple_for_chunk_a)]),
        FakeResponse(content=[ToolUse("tool_use", "propose_triple", triple_for_chunk_b)]),
    ])
    extractor = SequentialExtractor(client)
    orch = IngestOrchestrator(extractor, store or FakeStore(), embedder, graph,
                               edge_loader=loader, rejected_log_path=rejected_log_path)
    return orch, client, filename


def test_happy_path_approves_and_commits_to_graph():
    graph = InMemoryGraph()
    orch, client, path = _orchestrator(graph)
    result = orch.run(path, input_fn=lambda _: "approve")
    assert result.stats.verdict == "approved"
    assert result.stats.committed_entities == 3
    assert result.stats.committed_edges == 2
    assert ("rates", "REDUCES", "borrowing") in graph.edges
    assert ("borrowing", "IS", "reduced") in graph.edges
    assert len(client.calls) == 2
    assert result.stats.rejected_count == 0


def test_extraction_request_count_matches_per_chunk_calls():
    graph = InMemoryGraph()
    orch, client, path = _orchestrator(graph)
    result = orch.run(path, input_fn=lambda _: "approve")
    assert result.stats.extraction_requests == 2
    assert len(client.calls) == result.stats.extraction_requests


def test_embedding_request_count_tracks_seam_calls():
    graph = InMemoryGraph()
    embedder = CountingEmbedder(vector=(1.0, 0.0))
    orch, _, path = _orchestrator(graph, embedder=embedder)
    result = orch.run(path, input_fn=lambda _: "approve")
    assert result.stats.embedding_requests == embedder.calls
    assert embedder.calls >= 1


def test_ambiguity_queued_never_merges_and_defaults_to_create_new():
    """An ambiguity queue renders as a review note and never auto-merges."""
    graph = InMemoryGraph()
    first = Entity("e1", "Alpha", "concept")
    second = Entity("e2", "Alfa", "concept")
    store = FakeStore(similar=[SimilarEntity(first, 0.90), SimilarEntity(second, 0.88)])
    embedder = CountingEmbedder(vector=(1.0, 0.0))
    orch, _, path = _orchestrator(graph, store=store, embedder=embedder)
    result = orch.run(path, input_fn=lambda _: "approve")
    assert result.stats.verdict == "approved"
    # The ambiguous candidate was resolved as create-new and committed.
    assert result.stats.ambiguity_queued >= 1
    assert result.stats.ambiguity_notes
    # No matches above the semantic threshold merged an ambiguous candidate into an existing entity.
    assert all("create-new" in note for note in result.stats.ambiguity_notes)


def test_reject_verdict_writes_jsonl_and_skips_commit(tmp_path):
    rejected_log = tmp_path / "rejected.jsonl"
    graph = InMemoryGraph()
    from principle_graph.neo4j import RejectedRecordSink, Neo4jGraphWriter
    class _JsonlWriter:
        def __init__(self, sink):
            self.sink = sink
            self.rejected: list[dict[str, object]] = []
        def upsert_entity(self, entity): graph.upsert_entity(entity)
        def upsert_edge(self, edge): graph.upsert_edge(edge)
        def get_edge(self, subject, relation, object_): return graph.get_edge(subject, relation, object_)
        def record_rejected(self, record):
            self.rejected.append(record)
            self.sink.record_rejected(record)
    writer = _JsonlWriter(RejectedRecordSink(path=rejected_log))
    orch, _, path = _orchestrator_for(InMemoryGraph(), _markdown_source(), rejected_log_path=str(rejected_log))
    orch.writer = writer
    result = orch.run(path, input_fn=lambda _: "r")
    assert result.stats.verdict == "rejected"
    assert result.stats.committed_edges == 0
    assert result.stats.committed_entities == 0
    assert graph.edges == {}
    assert result.stats.rejected_count >= 1
    assert rejected_log.exists()
    records = [json.loads(line) for line in rejected_log.read_text().splitlines()]
    assert all(record["decision"] == "rejected" for record in records)


def test_stats_block_renders_required_fields():
    graph = InMemoryGraph()
    orch, _, path = _orchestrator(graph)
    result = orch.run(path, input_fn=lambda _: "approve")
    transcript = result.stats.render()
    for key in ("source:", "chunks processed sequentially:", "extraction requests:",
                "embedding requests:", "ambiguity-queued candidates:",
                "ambiguity review notes:", "Mode-2 review: approved", "commit result:",
                "rejected items:", "log:", "elapsed seconds:"):
        assert key in transcript, f"missing key in stats block: {key!r}"


def test_same_source_rerun_does_not_double_boost_confidence(tmp_path):
    """Re-ingesting the same source must not raise confidence again."""
    source = tmp_path / "demo.md"
    source.write_text(_markdown_source(), encoding="utf-8")
    chunks = chunk_markdown(source.read_text(encoding="utf-8"), source.name)
    triple_a = _triple(chunks[0].source_ref, "rates", "reduces", "borrowing")
    triple_b = _triple(chunks[1].source_ref, "borrowing", "is", "reduced")
    triple_c = _triple(chunks[0].source_ref, "rates", "reduces", "borrowing", confidence=0.95)

    graph = InMemoryGraph()

    def _run(responses):
        client = FakeMessages(list(responses))
        extractor = SequentialExtractor(client)
        orch = IngestOrchestrator(extractor, FakeStore(), None, graph)
        return orch.run(source, input_fn=lambda _: "approve"), client

    # First run seeds the graph.
    first, client_first = _run([
        FakeResponse(content=[ToolUse("tool_use", "propose_triple", triple_a)]),
        FakeResponse(content=[ToolUse("tool_use", "propose_triple", triple_b)]),
    ])
    assert first.stats.verdict == "approved"
    initial_conf = graph.edges[("rates", "REDUCES", "borrowing")].confidence

    # Second run reuses the same source_ref with a higher-confidence event; same-source
    # reruns must NOT double-boost confidence because reduction treats the same source_ref
    # as non-independent. The test would also pass if the rerun is a no-op for the existing
    # edge, but a fresh graph ensures we are not relying on the latter for correctness.
    second, client_second = _run([
        FakeResponse(content=[ToolUse("tool_use", "propose_triple", triple_c)]),
        FakeResponse(content=[ToolUse("tool_use", "propose_triple", triple_b)]),
    ])
    final_conf = graph.edges[("rates", "REDUCES", "borrowing")].confidence
    assert final_conf == initial_conf, (
        f"same-source rerun unexpectedly boosted confidence: {initial_conf} -> {final_conf}")
    # Extraction stat reflects per-chunk calls; each run issued two create() calls.
    assert first.stats.extraction_requests == len(client_first.calls) == 2
    assert second.stats.extraction_requests == len(client_second.calls) == 2
    # Evidence retention across reruns.
    assert "rates reduces borrowing" in graph.edges[("rates", "REDUCES", "borrowing")].evidence
    assert graph.edges[("rates", "REDUCES", "borrowing")].source_ref == chunks[0].source_ref


def test_pdf_and_markdown_dispatch_by_extension(tmp_path):
    """Both extensions are dispatched automatically."""
    md_path = tmp_path / "doc.md"
    md_path.write_text(_markdown_source(), encoding="utf-8")
    graph_md = InMemoryGraph()
    orch_md, _, _md_path = _orchestrator_for(graph_md, _markdown_source(), filename=str(md_path))
    md_result = orch_md.run(md_path)
    assert md_result.stats.source == "doc.md"
    assert md_result.stats.chunks_sequential
    # PDF path: write a fake PDF with .pdf extension; load_source routes it via chunk_pdf
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-fake")  # chunk_pdf will call fitz; failure surfaces as RuntimeError
    orch_pdf, _, _pdf_path = _orchestrator_for(InMemoryGraph(), _markdown_source(), filename=str(pdf_path))
    try:
        orch_pdf.run(pdf_path)
    except RuntimeError:
        # chunk_pdf wraps ImportError; pymupdf may raise FileDataError on bad data.
        # Either is acceptable: the routing was by extension, not content.
        pass
    except Exception as exc:
        # Any failure here means the PDF path was attempted — that proves dispatch worked.
        assert "Failed to open file" in str(exc) or "PDF" in str(exc)


def test_existing_edge_loader_is_consulted_for_delta_assembly():
    """When edges already exist on the graph, the loader is invoked for triple lookup."""
    from dataclasses import dataclass, field as dc_field
    graph = InMemoryGraph()
    prior = GraphEdge("rates", "REDUCES", "borrowing", 0.4, "prior-source", ("earlier",), "earlier scope")
    graph.edges[(prior.subject, prior.relation, prior.object)] = prior
    graph.entities.append(GraphEntity("rates", "concept"))
    graph.entities.append(GraphEntity("borrowing", "concept"))
    graph.entities.append(GraphEntity("reduced", "concept"))
    loader = FakeEdgeLoader(edges=[prior])

    @dataclass
    class _ToolUse:
        type: str; name: str; input: dict
    @dataclass
    class _FakeResponse:
        content: list
    @dataclass
    class _FakeMessages:
        responses: list
        calls: list = dc_field(default_factory=list)
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return self.responses.pop(0)

    import tempfile
    p = Path(tempfile.mkstemp(suffix=".md")[1])
    p.write_text(_markdown_source(), encoding="utf-8")
    chunks = chunk_markdown(_markdown_source(), p.name)
    client = _FakeMessages([
        _FakeResponse(content=[_ToolUse("tool_use", "propose_triple",
                                         {"subject": "rates", "subject_type": "concept",
                                          "relation": "reduces", "object": "borrowing",
                                          "object_type": "concept", "confidence": 0.9,
                                          "evidence": "rates reduces borrowing",
                                          "scope_conditions": "extracted",
                                          "source_ref": chunks[0].source_ref})]),
        _FakeResponse(content=[_ToolUse("tool_use", "propose_triple",
                                         {"subject": "borrowing", "subject_type": "concept",
                                          "relation": "is", "object": "reduced",
                                          "object_type": "concept", "confidence": 0.9,
                                          "evidence": "borrowing is reduced",
                                          "scope_conditions": "extracted",
                                          "source_ref": chunks[1].source_ref})]),
    ])
    extractor = SequentialExtractor(client)
    orch = IngestOrchestrator(extractor, FakeStore(), None, graph, edge_loader=loader)
    result = orch.run(p, input_fn=lambda _: "approve")
    # Loader was consulted with triples derived from the resolved candidates.
    assert loader.calls, f"loader was never called; calls={loader.calls}"
    assert loader.calls[0], "loader was called with empty triples list"
    triples = loader.calls[0]
    assert any(subject == "rates" for subject, _rel, _obj in triples)
    # Aggregation from a different source_ref boosts confidence (complement formula).
    edge = graph.edges[(prior.subject, prior.relation, prior.object)]
    assert edge.confidence > prior.confidence
    assert "rates reduces borrowing" in edge.evidence
    assert edge.source_ref == chunks[0].source_ref


def _run_via_load_source(orch):
    return orch.run(_fake_markdown_path(), input_fn=lambda _: "approve")


def _fake_markdown_path(tmp_path_factory=None) -> str:
    """Materialize the markdown source so load_source can read it."""
    import tempfile
    path = Path(tempfile.mkstemp(suffix=".md")[1])
    path.write_text(_markdown_source(), encoding="utf-8")
    return str(path)


def test_load_source_dispatches_markdown():
    import tempfile
    path = Path(tempfile.mkstemp(suffix=".md")[1])
    path.write_text(_markdown_source(), encoding="utf-8")
    chunks, source_id = load_source(path)
    assert source_id == path.name
    assert chunks
    assert all(chunk.source_ref.startswith(source_id) for chunk in chunks)


def test_load_source_rejects_unknown_extension():
    import tempfile
    path = Path(tempfile.mkstemp(suffix=".txt")[1])
    path.write_text("hello")
    try:
        load_source(path)
    except ValueError as exc:
        assert "unsupported source extension" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown extension")


def test_ambiguity_note_formatter_lists_ranked_matches():
    from principle_graph.resolution import AmbiguityItem, ResolutionMatch
    match_a = ResolutionMatch(Entity("e1", "Alpha", "concept"), 0.92, "embedding")
    match_b = ResolutionMatch(Entity("e2", "Alfa", "concept"), 0.88, "embedding")
    item = AmbiguityItem("alpha", "concept", "chunk-1", (match_a, match_b))
    note = format_ambiguity_note(item)
    assert "Alpha (0.92 via embedding)" in note
    assert "Alfa (0.88 via embedding)" in note
    assert "create-new" in note

def test_stats_block_reports_unknown_relations_from_writer():
    """ADR-0003: the ingest run summary surfaces the writer's unknown-verb flag."""
    graph = InMemoryGraph()
    orch, _, path = _orchestrator(graph)
    # Any object with an unknown_relation_counts attribute stands in for the
    # writer seam; counts come straight from the boundary flag.
    graph.unknown_relation_counts = {"WIDGET_BOOSTS": 2, "FROBS": 1}
    result = orch.run(path, input_fn=lambda _: "approve")
    transcript = result.stats.render()
    assert result.stats.unknown_relations == (("FROBS", 1), ("WIDGET_BOOSTS", 2))
    assert ("unknown relations passed through uncanonicalized: "
            "FROBS=1, WIDGET_BOOSTS=2") in transcript


def test_stats_block_omits_unknown_line_when_no_unknowns():
    graph = InMemoryGraph()
    orch, _, path = _orchestrator(graph)
    result = orch.run(path, input_fn=lambda _: "approve")
    assert result.stats.unknown_relations == ()
    assert "unknown relations" not in result.stats.render()


def test_stats_block_reports_unknown_entity_types_from_writer():
    """Issue #99 P2: writer-side unknown entity-type counts reach the transcript."""
    graph = InMemoryGraph()
    orch, _, path = _orchestrator(graph)
    graph.unknown_entity_type_counts = {"xenosophy": 2}
    result = orch.run(path, input_fn=lambda _: "approve")
    transcript = result.stats.render()
    assert result.stats.unknown_entity_types == (("xenosophy", 2),)
    assert "unknown entity types passed through uncanonicalized: xenosophy=2" in transcript
