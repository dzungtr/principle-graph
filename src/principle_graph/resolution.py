"""Entity resolution against a permanent graph and a disposable session registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import logging
import re
from typing import Any, Iterable, Protocol, Sequence

from .label_registry import LabelRegistry

logger = logging.getLogger(__name__)

NAME_SIMILARITY = 0.90
EMBEDDING_SIMILARITY = 0.85
EMBEDDING_MARGIN = 0.05


def normalize_name(value: str) -> str:
    """Normalize names for exact alias matching without changing the claim."""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    type: str
    aliases: tuple[str, ...] = ()
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class SimilarEntity:
    entity: Entity
    score: float


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> Sequence[float]: ...


class EntityStore(Protocol):
    def find_entities(self, name: str, entity_type: str) -> Sequence[Entity]: ...
    def search_similar(self, embedding: Sequence[float], entity_type: str, limit: int = 10) -> Sequence[SimilarEntity]: ...
    def structural_corroboration(self, entity: Entity, neighbors: Sequence[tuple[str, str]]) -> Any: ...
    # Issue #99: containment prefilter + alias accumulation on canonical nodes.
    def containment_candidates(self, name: str, entity_type: str) -> Sequence[Entity]: ...
    def add_alias(self, entity: Entity, alias: str) -> None: ...


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(a * a for a in right) ** 0.5
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


def containment_matches(name: str, entities: Iterable[Entity]) -> list[Entity]:
    """Entities whose name/alias tokens strictly contain the candidate's tokens,
    or are strictly contained by them (``Merz`` ⊂ ``Friedrich Merz``), issue #99."""
    candidate_tokens = set(normalize_name(name).split())
    if not candidate_tokens:
        return []
    out = []
    for entity in entities:
        entity_tokens = set(normalize_name(entity.name).split())
        for alias in entity.aliases:
            entity_tokens.update(normalize_name(alias).split())
        if entity_tokens and entity_tokens != candidate_tokens and (
            candidate_tokens <= entity_tokens or entity_tokens <= candidate_tokens
        ):
            out.append(entity)
    return out


@dataclass(frozen=True)
class ResolutionMatch:
    entity: Entity
    score: float
    method: str
    structural_evidence: Any = None


@dataclass(frozen=True)
class AmbiguityItem:
    candidate: str
    entity_type: str
    source_ref: str
    matches: tuple[ResolutionMatch, ...]
    structural_evidence: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Resolution:
    outcome: str
    candidate: str
    entity_type: str
    canonical: Entity | None = None
    matches: tuple[ResolutionMatch, ...] = ()
    ambiguity: AmbiguityItem | None = None


@dataclass
class SessionRegistry:
    """Scratch identities introduced during one sequential ingestion session."""

    entities: dict[tuple[str, str], Entity] = field(default_factory=dict)
    aliases: dict[tuple[str, str], Entity] = field(default_factory=dict)

    def add(self, entity: Entity) -> None:
        self.entities[(normalize_name(entity.name), entity.type)] = entity
        for alias in entity.aliases:
            self.aliases[(normalize_name(alias), entity.type)] = entity

    def lookup(self, name: str, entity_type: str) -> Entity | None:
        key = (normalize_name(name), entity_type)
        return self.entities.get(key) or self.aliases.get(key)

    def fuzzy_matches(self, name: str, entity_type: str) -> list[ResolutionMatch]:
        normalized = normalize_name(name)
        matches = []
        for entity in self.entities.values():
            if entity.type != entity_type:
                continue
            score = max(SequenceMatcher(None, normalized, normalize_name(n)).ratio()
                        for n in (entity.name,) + entity.aliases)
            if score >= NAME_SIMILARITY:
                matches.append(ResolutionMatch(entity, score, "session-fuzzy"))
        return sorted(matches, key=lambda m: m.score, reverse=True)

    def containment_entities(self, name: str, entity_type: str) -> list[Entity]:
        return containment_matches(name, (
            entity for (key, entity) in self.entities.items() if key[1] == entity_type
        ))

    def add_alias(self, entity: Entity, alias: str) -> None:
        self.aliases[(normalize_name(alias), entity.type)] = entity

    def semantic_matches(self, embedding: Sequence[float], entity_type: str) -> list[ResolutionMatch]:
        matches = []
        for entity in self.entities.values():
            if entity.type == entity_type and entity.embedding is not None:
                score = cosine(embedding, entity.embedding)
                if score >= EMBEDDING_SIMILARITY:
                    matches.append(ResolutionMatch(entity, score, "session-embedding"))
        return sorted(matches, key=lambda m: m.score, reverse=True)


class EntityResolver:
    """Resolve extracted names; graph access is injected to keep this stage testable."""

    def __init__(self, store: EntityStore, embedder: EmbeddingProvider | None = None,
                 registry: SessionRegistry | None = None,
                 entity_registry: LabelRegistry | None = None) -> None:
        self.store = store
        self.embedder = embedder
        self.registry = registry or SessionRegistry()
        # Issue #99: type canonicalization happens BEFORE any matching, so type
        # drift (politician vs person) never blocks resolution merges. Unknown
        # types pass through unchanged and are flagged, never rejected.
        self.entity_registry = entity_registry
        self.unknown_type_counts: dict[str, int] = {}
        self._warned_unknown_type: set[str] = set()
        self._created = 0

    def _canonical_type(self, entity_type: str) -> str:
        if self.entity_registry is None:
            return entity_type
        canonical = self.entity_registry.canonical_for(entity_type)
        if not self.entity_registry.is_known(entity_type):
            self.unknown_type_counts[canonical] = (
                self.unknown_type_counts.get(canonical, 0) + 1)
            if canonical not in self._warned_unknown_type:
                self._warned_unknown_type.add(canonical)
                logger.warning(
                    "unregistered entity type %r passed through uncanonicalized; "
                    "add it (or an alias) to the entity registry to consolidate it",
                    canonical,
                )
        return canonical

    def _store_add_alias(self, entity: Entity, alias: str) -> None:
        """Persist a merged surface form on the canonical entity (issue #99)."""
        if normalize_name(alias) == normalize_name(entity.name) or any(
            normalize_name(alias) == normalize_name(a) for a in entity.aliases
        ):
            return
        add = getattr(self.store, "add_alias", None)
        if add is not None:
            add(entity, alias)
        # Register the alias in the session too, so later lookups reuse it
        # without another store round-trip.
        self.registry.add_alias(entity, alias)

    def resolve(self, candidate: str, raw_entity_type: str, *, embedding: Sequence[float] | None = None,
                neighbors: Sequence[tuple[str, str]] = (), source_ref: str = "") -> Resolution:
        entity_type = self._canonical_type(raw_entity_type)
        session_entity = self.registry.lookup(candidate, entity_type)
        if session_entity:
            match = ResolutionMatch(session_entity, 1.0, "session")
            return Resolution("auto-resolve", candidate, entity_type, session_entity, (match,))

        # Apply the same thresholds to identities created earlier in this session.
        session_fuzzy = self.registry.fuzzy_matches(candidate, entity_type)
        if len(session_fuzzy) == 1:
            return Resolution("auto-resolve", candidate, entity_type, session_fuzzy[0].entity, tuple(session_fuzzy))
        if len(session_fuzzy) > 1:
            return self._queue(candidate, entity_type, source_ref, session_fuzzy)
        if embedding is None and self.embedder is not None:
            embedding = self.embedder.embed(candidate)
        session_semantic = self.registry.semantic_matches(embedding, entity_type) if embedding is not None else []
        if session_semantic:
            if len(session_semantic) == 1 or session_semantic[0].score - session_semantic[1].score >= EMBEDDING_MARGIN:
                return Resolution("auto-resolve", candidate, entity_type, session_semantic[0].entity, tuple(session_semantic))
            return self._queue(candidate, entity_type, source_ref, session_semantic)

        exact = list(self.store.find_entities(candidate, entity_type))
        normalized = normalize_name(candidate)
        exact_matches = [e for e in exact if normalize_name(e.name) == normalized or
                         any(normalize_name(a) == normalized for a in e.aliases)]
        if len(exact_matches) == 1:
            match = ResolutionMatch(exact_matches[0], 1.0, "exact")
            return Resolution("auto-resolve", candidate, entity_type, exact_matches[0], (match,))
        # exact-alias hits already carry the surface form; nothing to accumulate.
        if len(exact_matches) > 1:
            return self._queue(candidate, entity_type, source_ref,
                               [ResolutionMatch(e, 1.0, "exact") for e in exact_matches])

        fuzzy = []
        for entity in exact:
            names = (entity.name,) + entity.aliases
            score = max(SequenceMatcher(None, normalized, normalize_name(n)).ratio() for n in names)
            if score >= NAME_SIMILARITY:
                fuzzy.append(ResolutionMatch(entity, score, "fuzzy"))
        if len(fuzzy) == 1:
            self._store_add_alias(fuzzy[0].entity, candidate)
            return Resolution("auto-resolve", candidate, entity_type, fuzzy[0].entity, tuple(fuzzy))
        if len(fuzzy) > 1:
            return self._queue(candidate, entity_type, source_ref, sorted(fuzzy, key=lambda m: m.score, reverse=True))

        # Issue #99: name-containment matching (Merz ⊂ Friedrich Merz). A
        # containment merge requires embedding corroboration; without it the
        # candidate queues via the existing ambiguity machinery (create-new
        # default preserved downstream).
        if embedding is None and self.embedder is not None:
            embedding = self.embedder.embed(candidate)
        store_containment = containment_matches(
            candidate, getattr(self.store, "containment_candidates", lambda _n, _t: [])(
                candidate, entity_type))
        contained = [ResolutionMatch(
            e, cosine(embedding, e.embedding) if embedding is not None and e.embedding else 0.0,
            "containment") for e in store_containment]
        corroborated = [m for m in contained
                        if m.score >= EMBEDDING_SIMILARITY]
        if len(corroborated) == 1:
            self._store_add_alias(corroborated[0].entity, candidate)
            return Resolution("auto-resolve", candidate, entity_type,
                              corroborated[0].entity, tuple(corroborated +
                              [m for m in contained if m not in corroborated]))
        if contained:
            return self._queue(candidate, entity_type, source_ref, contained)

        # Session-registry containment, same corroboration rule.
        session_contained = [ResolutionMatch(
            e, cosine(embedding, e.embedding) if embedding is not None and e.embedding else 0.0,
            "session-containment")
            for e in self.registry.containment_entities(candidate, entity_type)]
        session_ok = [m for m in session_contained if m.score >= EMBEDDING_SIMILARITY]
        if len(session_ok) == 1:
            return Resolution("auto-resolve", candidate, entity_type, session_ok[0].entity,
                              tuple(session_ok))
        if session_contained:
            return self._queue(candidate, entity_type, source_ref, session_contained)
        semantic: list[ResolutionMatch] = []
        if embedding is not None:
            semantic = [ResolutionMatch(item.entity, item.score, "embedding")
                        for item in self.store.search_similar(embedding, entity_type)
                        if item.score >= EMBEDDING_SIMILARITY]
        semantic.sort(key=lambda m: m.score, reverse=True)
        if semantic:
            evidence = [self.store.structural_corroboration(m.entity, neighbors) for m in semantic]
            semantic = [ResolutionMatch(m.entity, m.score, m.method, e)
                        for m, e in zip(semantic, evidence)]
            if len(semantic) == 1 or semantic[0].score - semantic[1].score >= EMBEDDING_MARGIN:
                return Resolution("auto-resolve", candidate, entity_type, semantic[0].entity, tuple(semantic))
            corroborated = [m for m in semantic if self._evidence_count(m.structural_evidence) >= 2]
            if len(corroborated) == 1:
                return Resolution("auto-resolve", candidate, entity_type, corroborated[0].entity, tuple(semantic))
            return self._queue(candidate, entity_type, source_ref, semantic)

        self._created += 1
        created = Entity(f"session:{self._created}", candidate, entity_type,
                         embedding=tuple(embedding) if embedding is not None else None)
        self.registry.add(created)
        return Resolution("create", candidate, entity_type, created)

    @staticmethod
    def _evidence_count(evidence: Any) -> int:
        if isinstance(evidence, int):
            return evidence
        if evidence is None:
            return 0
        try:
            return len(evidence)
        except TypeError:
            return 0

    @staticmethod
    def _queue(candidate: str, entity_type: str, source_ref: str,
               matches: Iterable[ResolutionMatch]) -> Resolution:
        ordered = tuple(sorted(matches, key=lambda m: m.score, reverse=True))
        item = AmbiguityItem(candidate, entity_type, source_ref, ordered,
                             tuple(m.structural_evidence for m in ordered if m.structural_evidence is not None))
        return Resolution("ambiguity queue", candidate, entity_type, matches=ordered, ambiguity=item)
