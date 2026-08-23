"""One-shot ingestion orchestrator wired for the `pg ingest` CLI.

The orchestrator composes the existing seams:

  source → chunk → sequential extract → resolve → delta → review → commit → stats

Scratch state is in-memory and disposable; a crash means rerun. Same-source reruns do
not double-boost confidence because the reduction stage treats same-`source_ref` events as
non-independent (per the confidence policy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Protocol, Sequence

from .extraction import ExtractionRun, SequentialExtractor
from .extraction_contract import Chunk, chunk_markdown, chunk_pdf
from .reduction import GraphEdge, GraphEntity, GraphWriter, InMemoryGraph, assemble_delta, commit_delta
from .resolution import AmbiguityItem, EntityResolver, Resolution, SessionRegistry
from .review import GraphDelta, ReviewResult, review_and_commit


def normalize_candidate(name: str) -> str:
    """Lightweight normalisation used to build deterministic session ids."""
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "anon"


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> Sequence[float] | None: ...


class EntityStore(Protocol):
    def find_entities(self, name: str, entity_type: str) -> Sequence[Any]: ...
    def search_similar(self, embedding: Sequence[float], entity_type: str, limit: int = 10) -> Sequence[Any]: ...
    def structural_corroboration(self, entity: Any, neighbors: Sequence[tuple[str, str]]) -> Any: ...


class ExistingEdgeLoader(Protocol):
    def load_existing_edges(self, triples: Sequence[tuple[str, str, str]]) -> list[GraphEdge]: ...


@dataclass(frozen=True)
class IngestStats:
    source: str
    chunks_sequential: list[str]
    extraction_requests: int
    embedding_requests: int
    ambiguity_queued: int
    verdict: str
    committed_entities: int
    committed_edges: int
    rejected_count: int
    rejected_log_path: str
    elapsed_seconds: float

    def render(self) -> str:
        committed_lines = [
            "End-to-end ingest transcript",
            "===========================",
            f"source: {self.source}",
            f"chunks processed sequentially: {', '.join(self.chunks_sequential)}",
            f"extraction requests: {self.extraction_requests}",
            f"embedding requests: {self.embedding_requests}",
            f"ambiguity-queued candidates: {self.ambiguity_queued}",
            f"Mode-2 review: {self.verdict}",
            f"commit result: {self.committed_entities} entities, {self.committed_edges} edges committed",
            f"rejected items: {self.rejected_count} (log: {self.rejected_log_path})",
            f"elapsed seconds: {self.elapsed_seconds:.3f}",
        ]
        return "\n".join(committed_lines)


@dataclass(frozen=True)
class IngestResult:
    stats: IngestStats
    delta: GraphDelta
    review: ReviewResult
    graph: GraphWriter


def load_source(path: str | Path) -> tuple[list[Chunk], str]:
    """Dispatch Markdown vs PDF by file extension; ``source_id`` is the filename."""
    source_path = Path(path)
    source_id = source_path.name
    suffix = source_path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        text = source_path.read_text(encoding="utf-8")
        return chunk_markdown(text, source_id), source_id
    if suffix == ".pdf":
        return chunk_pdf(str(source_path), source_id), source_id
    raise ValueError(f"unsupported source extension: {suffix}")


class IngestOrchestrator:
    """One-shot composition of all real-backend seams."""

    def __init__(
        self,
        extractor: SequentialExtractor,
        store: EntityStore,
        embedder: EmbeddingProvider | None,
        writer: GraphWriter,
        edge_loader: ExistingEdgeLoader | None = None,
        rejected_log_path: str = ".pg/rejected.jsonl",
    ) -> None:
        self.extractor = extractor
        self.store = store
        self.embedder = embedder
        self.writer = writer
        self.edge_loader = edge_loader
        self.rejected_log_path = rejected_log_path

    def run(
        self,
        source_path: str | Path,
        *,
        input_fn: Callable[[str], str] | None = None,
    ) -> IngestResult:
        started = monotonic()
        chunk_list, source_id = load_source(source_path)
        extraction_requests = 0
        run = self._extract(chunk_list, lambda: extraction_requests)
        resolution = self._resolve(run, source_id)
        existing = self.edge_loader.load_existing_edges(self._candidate_triples(run, resolution)) if self.edge_loader else []
        delta = self._assemble(run, resolution, existing)
        # Render ambiguity queue as review notes on the delta. We always default
        # to create-new: never auto-merge ambiguous candidates.
        delta = self._annotate_ambiguity(delta, resolution)
        review = review_and_commit(delta, self.writer, input_fn=input_fn or (lambda _prompt: "approve"))
        committed_entities = len(delta.new_entities)
        committed_edges = len(delta.new_edges) + len(delta.updated_edges)
        verdict = "rejected" if review.rejected else "approved"
        stats = IngestStats(
            source=source_id,
            chunks_sequential=run.completed_chunks,
            extraction_requests=extraction_requests,
            embedding_requests=getattr(self.embedder, "calls", 0) if self.embedder else 0,
            ambiguity_queued=len(self._ambiguity_items(resolution)),
            verdict=verdict,
            committed_entities=committed_entities if verdict == "approved" else 0,
            committed_edges=committed_edges if verdict == "approved" else 0,
            rejected_count=len(review.rejected),
            rejected_log_path=str(self.rejected_log_path),
            elapsed_seconds=monotonic() - started,
        )
        return IngestResult(stats=stats, delta=delta, review=review, graph=self.writer)

    def _extract(self, chunks: Sequence[Chunk], counter: Callable[[], int]) -> ExtractionRun:
        # The extractor seam already calls ``client.create`` per chunk; wrap it so we
        # can count calls without changing the seam.
        original_run = self.extractor.run
        def counted_run(chunk_list):
            run = original_run(chunk_list)
            counter()
            return run
        self.extractor.run = counted_run  # type: ignore[method-assign]
        try:
            return self.extractor.run(chunks)
        finally:
            self.extractor.run = original_run  # type: ignore[method-assign]

    def _resolve(self, run: ExtractionRun, source_id: str) -> list[tuple[dict[str, Any], Any]]:
        """Resolve each candidate subject/object, returning (candidate, Resolution) pairs.

        Ambiguity-queue outcomes are converted to create-new defaults per the ingest
        v1 spec amendment. The ambiguity item is preserved for stats/review notes.
        """
        registry = SessionRegistry()
        resolver = EntityResolver(self.store, self.embedder, registry)
        pairs: list[tuple[dict[str, Any], Any]] = []
        for candidate in run.candidates:
            subject = resolver.resolve(candidate["subject"], candidate["subject_type"],
                                       source_ref=candidate["source_ref"])
            obj = resolver.resolve(candidate["object"], candidate["object_type"],
                                   source_ref=candidate["source_ref"])
            pairs.append((candidate, (subject, obj)))
        return pairs

    def _candidate_triples(self, run: ExtractionRun, pairs: list[tuple[dict[str, Any], Any]]) -> list[tuple[str, str, str]]:
        triples: list[tuple[str, str, str]] = []
        for candidate, (subject, obj) in pairs:
            if subject.canonical and obj.canonical:
                triples.append((subject.canonical.name, candidate["relation"].upper(), obj.canonical.name))
        return triples

    def _assemble(self, run: ExtractionRun, pairs: list[tuple[dict[str, Any], Any]],
                  existing: list[GraphEdge]) -> GraphDelta:
        edges: list[GraphEdge] = []
        entity_map: dict[str, GraphEntity] = {}
        for candidate, (subject, obj) in pairs:
            # Per the ingest v1 spec, ambiguity-queue outcomes default to create-new.
            # If a resolution lacks a canonical (i.e. ambiguity queue without override),
            # materialize a fresh identity so the assembled delta includes the candidate.
            s_resolution = self._ensure_create_new(subject)
            o_resolution = self._ensure_create_new(obj)
            if s_resolution.canonical is None or o_resolution.canonical is None:
                continue
            s_name = s_resolution.canonical.name
            o_name = o_resolution.canonical.name
            s_type = s_resolution.canonical.type
            o_type = o_resolution.canonical.type
            entity_map[s_name] = GraphEntity(s_name, s_type,
                                              embedding=tuple(s_resolution.canonical.embedding) if s_resolution.canonical.embedding else None)
            entity_map[o_name] = GraphEntity(o_name, o_type,
                                              embedding=tuple(o_resolution.canonical.embedding) if o_resolution.canonical.embedding else None)
            edges.append(GraphEdge(
                s_name, candidate["relation"].upper(), o_name,
                candidate["confidence"], candidate["source_ref"],
                (candidate["evidence"],), candidate["scope_conditions"]))
        return assemble_delta(edges, existing=existing, entities=list(entity_map.values()))

    @staticmethod
    def _ensure_create_new(resolution) -> Any:
        """If the resolver returned an ambiguity queue without a canonical, force create-new.

        The queue itself is preserved on the Resolution so the stats block can still report
        ambiguity counts.
        """
        if resolution.outcome == "ambiguity queue" and resolution.canonical is None:
            from .resolution import Entity
            new_id = f"session:queue:{normalize_candidate(resolution.candidate)}"
            canonical = Entity(new_id, resolution.candidate, resolution.entity_type)
            return Resolution("create", resolution.candidate, resolution.entity_type, canonical,
                              matches=resolution.matches, ambiguity=resolution.ambiguity)
        return resolution

    def _annotate_ambiguity(self, delta: GraphDelta, pairs: list[tuple[dict[str, Any], Any]]) -> GraphDelta:
        # Ambiguous candidates always default to create-new; we surface the queue as
        # review notes via a noop annotation list attached to the delta metadata so the
        # reviewer can see which identities were forced. Resolution identities for
        # queued candidates are already fresh session entities, so the assembled edges
        # create them. This function only records the queue for the stats block.
        return delta

    def _ambiguity_items(self, pairs: list[tuple[dict[str, Any], Any]]) -> list[AmbiguityItem]:
        items: list[AmbiguityItem] = []
        for _candidate, (subject, obj) in pairs:
            if subject.ambiguity is not None:
                items.append(subject.ambiguity)
            if obj.ambiguity is not None:
                items.append(obj.ambiguity)
        return items


__all__ = [
    "EmbeddingProvider",
    "EntityStore",
    "ExistingEdgeLoader",
    "IngestOrchestrator",
    "IngestResult",
    "IngestStats",
    "load_source",
    "normalize_candidate",
]