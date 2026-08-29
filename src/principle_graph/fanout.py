"""Fan-out graph queries for ranked reasoning directions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence
import math
import re

from .resolution import Entity, normalize_name
from .review import GraphEdge

# Seeding free-form queries against entity names tops out well below the
# entity-resolution similarity: measured sentence-to-name cosine on the demo
# graph peaks around 0.72, so the shared 0.85 constant would never fire.
QUERY_SEED_SIMILARITY = 0.60


def _embedder_unavailable_notice() -> str:
    return "Ollama embedder unavailable; degraded to exact-name seed matching."


@dataclass(frozen=True)
class Seed:
    entity: Entity
    score: float


@dataclass(frozen=True)
class Direction:
    rank: int
    seed: str
    relation: str
    neighbor: str
    confidence: float
    scope_conditions: str = ""
    source_ref: str = ""
    evidence: tuple[str, ...] = ()
    seed_score: float = 1.0


class QueryGraph(Protocol):
    def entities(self) -> Sequence[Entity]: ...
    def edges_for(self, entity: Entity) -> Sequence[GraphEdge]: ...


class QueryEmbedder(Protocol):
    def embed(self, text: str) -> Sequence[float] | None: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def match_seeds(query: str, graph: QueryGraph, embedder: QueryEmbedder | None = None, *,
                threshold: float = QUERY_SEED_SIMILARITY,
                notices: list[str] | None = None) -> list[Seed]:
    """Match exact names/aliases first (score 1.0), then semantic entities above
    the query-seeding threshold, which is independent of the entity-resolution
    threshold. When the embedder is missing or fails, degrade to exact-name
    matching and record the degradation in *notices* when provided."""
    normalized = normalize_name(query)
    entities = list(graph.entities())
    exact = [e for e in entities if normalize_name(e.name) == normalized or
             any(normalize_name(alias) == normalized for alias in e.aliases)]
    if exact:
        return [Seed(e, 1.0) for e in sorted(exact, key=lambda x: x.id)]
    vector = embedder.embed(query) if embedder is not None else None
    if vector is None:
        if notices is not None:
            notices.append(_embedder_unavailable_notice())
        return []
    matches = [Seed(e, _cosine(vector, e.embedding)) for e in entities if e.embedding is not None]
    return sorted((m for m in matches if m.score >= threshold),
                  key=lambda m: (-m.score, m.entity.id))


def query_directions(query: str, graph: QueryGraph, *, top_k: int = 5,
                     max_edges_per_seed: int = 20,
                     embedder: QueryEmbedder | None = None,
                     threshold: float = QUERY_SEED_SIMILARITY,
                     notices: list[str] | None = None) -> tuple[list[Seed], list[Direction]]:
    if top_k <= 0 or max_edges_per_seed <= 0:
        raise ValueError("top_k and max_edges_per_seed must be positive")
    seeds = match_seeds(query, graph, embedder, threshold=threshold, notices=notices)
    candidates: dict[tuple[str, str, str], Direction] = {}
    for seed in seeds:
        for edge in list(graph.edges_for(seed.entity))[:max_edges_per_seed]:
            if edge.subject == seed.entity.name:
                neighbor = edge.object
            elif edge.object == seed.entity.name:
                neighbor = edge.subject
            else:
                continue
            key = (seed.entity.id, edge.relation.upper(), neighbor)
            candidate = Direction(0, seed.entity.name, edge.relation.upper(), neighbor,
                                  edge.confidence, edge.scope_conditions, edge.source_ref,
                                  tuple(edge.evidence), seed.score)
            prior = candidates.get(key)
            if prior is None or (candidate.confidence, candidate.seed_score) > (prior.confidence, prior.seed_score):
                candidates[key] = candidate
    ranked = sorted(candidates.values(), key=lambda d: (-d.confidence, -d.seed_score, d.neighbor))[:top_k]
    return seeds, [Direction(i, d.seed, d.relation, d.neighbor, d.confidence,
                             d.scope_conditions, d.source_ref, d.evidence, d.seed_score)
                   for i, d in enumerate(ranked, 1)]


def render_markdown(query: str, seeds: Sequence[Seed], directions: Sequence[Direction]) -> str:
    if not seeds:
        return f"No matching seeds for: {query}"
    lines = [f"Fan-out directions for: {query}", ""]
    for d in directions:
        scope = d.scope_conditions or "none"
        source = d.source_ref or "unknown"
        lines.append(f"{d.rank}. **{d.seed}** -[{d.relation}]-> **{d.neighbor}** "
                     f"(confidence {d.confidence:.2f}; scope: {scope}; source: {source})")
    if not directions:
        lines.append("No committed directions found.")
    return "\n".join(lines)
