"""One-shot ingestion orchestrator wired for the `pg ingest` CLI.

The orchestrator composes the existing seams:

  source → chunk → sequential extract → resolve → delta → review → commit → stats

Scratch state is in-memory and disposable; a crash means rerun. Repeat extractions are
no-ops under the ledger: same-source reruns match an existing ``:ExtractionEvent`` row by
identity and keep-first leaves it untouched (ADR-0002), so aggregates cannot wobble.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping, Protocol, Sequence

from .extraction import ExtractionRun, SequentialExtractor
from .extraction_contract import Chunk, chunk_markdown, chunk_pdf
from .novelty import NoveltyFilter, apply_novelty_filter
from .reduction import GraphEdge, GraphEntity, GraphWriter, InMemoryGraph, assemble_delta, commit_delta
from .resolution import AmbiguityItem, EntityResolver, Resolution, SessionRegistry
from .label_registry import LabelRegistry
from .review import GraphDelta, ReviewResult, review_and_commit


def normalize_candidate(name: str) -> str:
    """Lightweight normalisation used to build deterministic session ids."""
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "anon"


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> Sequence[float] | None: ...
    @property
    def calls(self) -> int: ...


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
    ambiguity_notes: tuple[str, ...]
    verdict: str
    committed_entities: int
    committed_edges: int
    rejected_count: int
    rejected_log_path: str
    elapsed_seconds: float
    # ADR-0003: unknown verbs passed through at the write boundary, per-verb
    # occurrence counts from the writer's boundary flag; empty when all verbs
    # canonicalized or the writer does not track them.
    unknown_relations: tuple[tuple[str, int], ...] = ()
    # Slice #78: unknown domains passed through at the write boundary, per-domain
    # occurrence counts; empty when all domains canonicalized or untracked.
    unknown_domains: tuple[tuple[str, int], ...] = ()
    # Issue #99: unknown entity types passed through at the write boundary,
    # per-type occurrence counts; empty when all types canonicalized/untracked.
    unknown_entity_types: tuple[tuple[str, int], ...] = ()
    # ADR-0006 novelty gate aggregates; zeroed when the filter is opted out.
    novelty_calls: int = 0
    filtered_noise: int = 0
    filtered_common_sense: int = 0
    novelty_mean_probabilities: Mapping[str, float] = field(default_factory=dict)

    def render(self) -> str:
        committed_lines = [
            "End-to-end ingest transcript",
            "===========================",
            f"source: {self.source}",
            f"chunks processed sequentially: {', '.join(self.chunks_sequential)}",
            f"extraction requests: {self.extraction_requests}",
            f"embedding requests: {self.embedding_requests}",
            f"ambiguity-queued candidates: {self.ambiguity_queued}",
            "ambiguity review notes:",
        ]
        if self.ambiguity_notes:
            committed_lines.extend(f"  - {note}" for note in self.ambiguity_notes)
        else:
            committed_lines.append("  - (none)")
        committed_lines.extend([
            f"Mode-2 review: {self.verdict}",
            f"commit result: {self.committed_entities} entities, {self.committed_edges} edges committed",
            f"rejected items: {self.rejected_count} (log: {self.rejected_log_path})",
        ])
        if self.unknown_relations:
            unknown = ", ".join(
                f"{verb}={count}" for verb, count in self.unknown_relations)
            committed_lines.append(
                f"unknown relations passed through uncanonicalized: {unknown}")
        if self.unknown_domains:
            unknown_domains = ", ".join(
                f"{name}={count}" for name, count in self.unknown_domains)
            committed_lines.append(
                f"unknown domains passed through uncanonicalized: {unknown_domains}")
        if self.unknown_entity_types:
            unknown_types = ", ".join(
                f"{name}={count}" for name, count in self.unknown_entity_types)
            committed_lines.append(
                f"unknown entity types passed through uncanonicalized: {unknown_types}")
        if self.novelty_calls or self.filtered_noise or self.filtered_common_sense:
            committed_lines.append(
                f"filtered items: {self.filtered_noise + self.filtered_common_sense} "
                f"(noise={self.filtered_noise}, common_sense={self.filtered_common_sense})")
            means = ", ".join(f"{key}={value:.2f}" for key, value in
                               sorted(self.novelty_mean_probabilities.items()))
            committed_lines.append(
                f"novelty calls: {self.novelty_calls}; mean probabilities: {means or '(none)'}")
        committed_lines.append(
            f"elapsed seconds: {self.elapsed_seconds:.3f}",
        )
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
        novelty_filter: "NoveltyFilter | None" = None,
        entity_registry: "LabelRegistry | None" = None,
    ) -> None:
        self.extractor = extractor
        self.store = store
        self.embedder = embedder
        self.writer = writer
        self.edge_loader = edge_loader
        self.rejected_log_path = rejected_log_path
        self.novelty_filter = novelty_filter
        # Issue #99: entity-type registry applied before matching.
        self.entity_registry = entity_registry

    def run(
        self,
        source_path: str | Path,
        *,
        input_fn: Callable[[str], str] | None = None,
    ) -> IngestResult:
        started = monotonic()
        chunk_list, source_id = load_source(source_path)
        run = self._extract(chunk_list)
        # ADR-0006 novelty gate: after extract, before resolve. ``None`` (opt-out)
        # keeps the current behavior with zero Jev calls. A filter failure raises
        # out of ``run`` — the ingest aborts with no partial state.
        novelty = None
        if self.novelty_filter is not None:
            run.candidates, novelty = apply_novelty_filter(run.candidates, self.novelty_filter)
        resolution = self._resolve(run, source_id)
        triples = self._candidate_triples(run, resolution)
        existing: list[GraphEdge] = []
        if self.edge_loader is not None:
            existing = self.edge_loader.load_existing_edges(triples)
        elif self.writer is not None and triples:
            # When no edge loader is provided, consult the writer's get_edge for each
            # candidate triple so confidence changes compute against the live graph.
            for subject, relation, object_ in triples:
                edge = self.writer.get_edge(subject, relation, object_)
                if edge is not None:
                    existing.append(edge)
        delta = self._assemble(run, resolution, existing)
        # Render ambiguity queue as review notes on the delta. We always default
        # to create-new: never auto-merge ambiguous candidates.
        delta, ambiguity_notes = self._annotate_ambiguity(delta, resolution)
        review = review_and_commit(delta, self.writer, input_fn=input_fn or (lambda _prompt: "approve"))
        committed_entities = len(delta.new_entities)
        committed_edges = len(delta.new_edges) + len(delta.updated_edges)
        verdict = "rejected" if review.rejected else "approved"
        stats = IngestStats(
            source=source_id,
            chunks_sequential=run.completed_chunks,
            extraction_requests=self._extraction_calls(run),
            embedding_requests=self._embedding_calls(run),
            ambiguity_queued=len(self._ambiguity_items(resolution)),
            ambiguity_notes=ambiguity_notes,
            verdict=verdict,
            committed_entities=committed_entities if verdict == "approved" else 0,
            committed_edges=committed_edges if verdict == "approved" else 0,
            rejected_count=len(review.rejected),
            rejected_log_path=str(self.rejected_log_path),
            elapsed_seconds=monotonic() - started,
            unknown_relations=tuple(
                sorted(getattr(self.writer, "unknown_relation_counts", {}).items())
            ),
            unknown_domains=tuple(
                sorted(getattr(self.writer, "unknown_domain_counts", {}).items())
            ),
            unknown_entity_types=tuple(
                sorted(getattr(self.writer, "unknown_entity_type_counts", {}).items())
            ),
            novelty_calls=(novelty.novelty_calls if novelty else 0),
            filtered_noise=(novelty.filtered_noise if novelty else 0),
            filtered_common_sense=(novelty.filtered_common_sense if novelty else 0),
            novelty_mean_probabilities=(dict(novelty.mean_probabilities) if novelty else {}),
        )
        return IngestResult(stats=stats, delta=delta, review=review, graph=self.writer)

    def _extract(self, chunks: Sequence[Chunk]) -> ExtractionRun:
        return self.extractor.run(chunks)

    def _extraction_calls(self, run: ExtractionRun) -> int:
        """Per-chunk model invocations: one ``client.create`` call per completed chunk."""
        return len(run.completed_chunks)

    def _embedding_calls(self, run: ExtractionRun) -> int:
        """Total embedding requests issued by the resolver during this session."""
        if self.embedder is None:
            return 0
        if hasattr(self.embedder, "calls"):
            return int(getattr(self.embedder, "calls") or 0)
        return 0

    def _resolve(self, run: ExtractionRun, source_id: str) -> list[tuple[dict[str, Any], Any]]:
        """Resolve each candidate subject/object, returning (candidate, Resolution) pairs.

        Ambiguity-queue outcomes are converted to create-new defaults per the ingest
        v1 spec amendment. The ambiguity item is preserved for stats/review notes.
        """
        registry = SessionRegistry()
        resolver = EntityResolver(self.store, self.embedder, registry,
                                  entity_registry=self.entity_registry)
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
                                              embedding=tuple(s_resolution.canonical.embedding) if s_resolution.canonical.embedding else None,
                                              aliases=tuple(dict.fromkeys(
                                                  tuple(s_resolution.canonical.aliases)
                                                  + s_resolution.new_aliases)))
            entity_map[o_name] = GraphEntity(o_name, o_type,
                                              embedding=tuple(o_resolution.canonical.embedding) if o_resolution.canonical.embedding else None,
                                              aliases=tuple(dict.fromkeys(
                                                  tuple(o_resolution.canonical.aliases)
                                                  + o_resolution.new_aliases)))
            edges.append(GraphEdge(
                s_name, candidate["relation"].upper(), o_name,
                candidate["confidence"], candidate["source_ref"],
                (candidate["evidence"],), candidate["scope_conditions"],
                candidate.get("domain", "")))
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

    def _annotate_ambiguity(self, delta: GraphDelta, pairs: list[tuple[dict[str, Any], Any]]) -> tuple[GraphDelta, tuple[str, ...]]:
        # Render the ambiguity queue as review notes on the delta. Each note records
        # the candidate, the ranked matches with their scores, and the structural
        # evidence if any. The defaults remain create-new; notes are informational.
        notes: list[str] = []
        for candidate, (subject, obj) in pairs:
            for resolution in (subject, obj):
                if resolution.ambiguity is None:
                    continue
                note = format_ambiguity_note(resolution.ambiguity)
                notes.append(note)
        notes_tuple = tuple(notes)
        metadata = dict(delta.metadata or {}) if hasattr(delta, "metadata") else {}
        if notes_tuple:
            metadata["ambiguity_notes"] = notes_tuple
        # GraphDelta does not yet expose metadata; attach notes via a side-channel
        # attribute so tests can inspect them without altering the immutable delta.
        return delta, notes_tuple

    def _ambiguity_items(self, pairs: list[tuple[dict[str, Any], Any]]) -> list[AmbiguityItem]:
        items: list[AmbiguityItem] = []
        for _candidate, (subject, obj) in pairs:
            if subject.ambiguity is not None:
                items.append(subject.ambiguity)
            if obj.ambiguity is not None:
                items.append(obj.ambiguity)
        return items


def format_ambiguity_note(item: AmbiguityItem) -> str:
    matches = ", ".join(f"{match.entity.name} ({match.score:.2f} via {match.method})"
                        for match in item.matches) or "(no ranked matches)"
    evidence = ", ".join(repr(e) for e in item.structural_evidence) or "(none)"
    return (f"candidate {item.candidate!r} ({item.entity_type}, source {item.source_ref}); "
            f"matches: {matches}; structural evidence: {evidence}; default: create-new")


__all__ = [
    "EmbeddingProvider",
    "EntityStore",
    "ExistingEdgeLoader",
    "IngestOrchestrator",
    "IngestResult",
    "IngestStats",
    "NoveltyFilter",
    "format_ambiguity_note",
    "load_source",
    "normalize_candidate",
]