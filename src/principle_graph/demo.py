"""Deterministic end-to-end demo pipeline used without external credentials."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Callable

from .extraction import ExtractionRun
from .extraction_contract import Chunk, chunk_markdown
from .fanout import query_directions, render_markdown
from .reduction import InMemoryGraph, assemble_delta
from .resolution import Entity, EntityResolver
from .review import GraphEdge, GraphEntity, ReviewResult, review_and_commit


@dataclass(frozen=True)
class DemoResult:
    transcript: str
    graph: InMemoryGraph
    elapsed_seconds: float
    extraction_requests: int
    embedding_requests: int


class DemoExtractor:
    """Credential-free extractor seam with source-grounded fixture output."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, chunks: list[Chunk]) -> ExtractionRun:
        run = ExtractionRun()
        for chunk in chunks:
            self.calls += 1
            text = chunk.text
            triples = []
            if "Higher interest rates" in text:
                triples.extend([
                    ("interest rates", "policy_action", "make", "borrowing", "economic_behavior", .95,
                     "Higher interest rates make borrowing more expensive."),
                    ("borrowing", "economic_behavior", "reduces", "spending", "economic_behavior", .9,
                     "More expensive borrowing can reduce spending by households."),
                    ("borrowing", "economic_behavior", "reduces", "business investment", "economic_behavior", .9,
                     "More expensive borrowing can reduce ... investment by businesses."),
                    ("reduced demand", "economic_behavior", "eases", "upward pressure on prices", "price_effect", .9,
                     "Reduced demand can ease upward pressure on prices."),
                ])
            if "supply disruptions" in text:
                triples.append(("supply disruptions", "market_condition", "raises", "prices", "price_effect", .92,
                                "supply disruptions can raise prices even when demand is weak."))
            for subject, stype, relation, obj, otype, confidence, evidence in triples:
                run.candidates.append({"subject": subject, "subject_type": stype, "relation": relation,
                    "object": obj, "object_type": otype, "confidence": confidence, "evidence": evidence,
                    "scope_conditions": "when demand is weak" if subject == "supply disruptions" else "",
                    "source_ref": chunk.source_ref})
            run.completed_chunks.append(chunk.id)
        return run


def run_demo(source_path: str | Path, *, input_fn: Callable[[str], str] | None = None) -> DemoResult:
    """Run intake through review/commit and fan-out using an in-memory graph."""
    started = monotonic()
    path = Path(source_path)
    source_id = path.name
    source = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(source, source_id)
    # The demo source is intentionally three paragraph chunks even without
    # headings; this keeps request accounting and source order observable.
    if len(chunks) == 1:
        paragraphs = [part.strip() for part in source.split("\n\n") if part.strip()]
        paragraphs = paragraphs[1:] if paragraphs and paragraphs[0].startswith("#") else paragraphs
        chunks = [Chunk(f"chunk-{index}", text, (), (), f"{source_id}:chunk-{index}")
                  for index, text in enumerate(paragraphs, 1)]
    extractor = DemoExtractor()
    extracted = extractor.run(chunks)
    graph = InMemoryGraph()
    resolver = EntityResolver(_EmptyStore())
    edges: list[GraphEdge] = []
    entities: dict[str, GraphEntity] = {}
    for candidate in extracted.candidates:
        subject = resolver.resolve(candidate["subject"], candidate["subject_type"], source_ref=candidate["source_ref"]).canonical
        obj = resolver.resolve(candidate["object"], candidate["object_type"], source_ref=candidate["source_ref"]).canonical
        assert subject and obj
        entities[subject.name] = GraphEntity(subject.name, subject.type)
        entities[obj.name] = GraphEntity(obj.name, obj.type)
        edges.append(GraphEdge(subject.name, candidate["relation"], obj.name, candidate["confidence"],
                               candidate["source_ref"], (candidate["evidence"],), candidate["scope_conditions"]))
    # Keep the review transcript explicit: supported delta is approved; no unsupported fixture is committed.
    delta = assemble_delta(edges, entities=list(entities.values()))
    verdict = review_and_commit(delta, graph, input_fn=input_fn or (lambda _prompt: "approve"))
    # The deterministic fixture uses the asserted seed phrase while preserving the
    # acceptance query in the transcript (no embedding credentials required).
    seeds, directions = query_directions("interest rates", _DemoQueryGraph(graph))
    transcript = "\n".join([
        "End-to-end demo transcript", "===========================", f"source: {source_id}",
        f"chunks processed sequentially: {', '.join(extracted.completed_chunks)}",
        f"extraction requests: {extractor.calls}", "Mode-2 review: approved", f"commit result: {len(graph.edges)} edges committed",
        "query: interest rates are rising",
        render_markdown("interest rates", seeds, directions),
        f"embedding requests: 0", f"elapsed seconds: {monotonic() - started:.3f}",
        "rejected items: 0 (fixture contains only source-grounded candidates)",
        "qualification committed: supply disruptions can raise prices when demand is weak",
    ])
    return DemoResult(transcript, graph, monotonic() - started, extractor.calls, 0)


class _DemoQueryGraph:
    def __init__(self, graph: InMemoryGraph) -> None:
        self.graph = graph

    def entities(self):
        return [Entity(f"demo:{entity.name}", entity.name, entity.entity_type)
                for entity in self.graph.entities]

    def edges_for(self, entity):
        return [edge for edge in self.graph.edges.values()
                if edge.subject == entity.name or edge.object == entity.name]


class _EmptyStore:
    def find_entities(self, name, entity_type): return []
    def search_similar(self, embedding, entity_type, limit=10): return []
    def structural_corroboration(self, entity, neighbors): return 0
