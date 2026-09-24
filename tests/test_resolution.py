from principle_graph.resolution import Entity, EntityResolver, SessionRegistry, SimilarEntity, normalize_name

import math


class Store:
    def __init__(self, entities=(), similar=(), evidence=0):
        self.entities, self.similar, self.evidence = list(entities), list(similar), evidence
        self.embedding_calls = 0

    def find_entities(self, name, entity_type):
        return [e for e in self.entities if e.type == entity_type]

    def search_similar(self, embedding, entity_type, limit=10):
        self.embedding_calls += 1
        return [x for x in self.similar if x.entity.type == entity_type][:limit]

    def structural_corroboration(self, entity, neighbors):
        return self.evidence.get(entity.id, 0) if isinstance(self.evidence, dict) else self.evidence


class Embedder:
    def embed(self, text):
        return [1.0, 0.0]


def test_exact_alias_and_fuzzy_name_auto_resolve():
    entity = Entity("e1", "Federal Reserve", "organization", ("the Fed",))
    resolver = EntityResolver(Store([entity]))
    assert resolver.resolve(" THE FED! ", "organization").canonical == entity
    assert resolver.resolve("Federal Reserv", "organization").outcome == "auto-resolve"


def test_semantic_requires_margin_and_queues_ambiguous_matches():
    first = Entity("e1", "Alpha", "concept")
    second = Entity("e2", "Alfa", "concept")
    store = Store(similar=[SimilarEntity(first, .90), SimilarEntity(second, .88)])
    result = EntityResolver(store, Embedder()).resolve("unknown", "concept")
    assert result.outcome == "ambiguity queue"
    assert result.ambiguity and result.ambiguity.source_ref == ""


def test_structural_corroboration_confirms_semantic_candidate_after_margin_check():
    first = Entity("e1", "Alpha", "concept")
    second = Entity("e2", "Alfa", "concept")
    store = Store(similar=[SimilarEntity(first, .86), SimilarEntity(second, .84)],
                  evidence={"e1": 2, "e2": 0})
    result = EntityResolver(store, Embedder()).resolve("unknown", "concept", neighbors=[("neighbor", "supports")])
    assert result.outcome == "auto-resolve"
    assert result.canonical == first


def test_session_fuzzy_match_reuses_created_identity():
    resolver = EntityResolver(Store(), Embedder())
    first = resolver.resolve("Federal Reserve", "organization")
    second = resolver.resolve("Fedral Reserve", "organization")
    assert first.outcome == "create"
    assert second.outcome == "auto-resolve"
    assert second.canonical == first.canonical


def test_session_semantic_match_reuses_created_identity():
    class FixedEmbedder:
        def embed(self, text):
            return [1.0, 0.0] if text == "first" else [0.99, 0.1]
    resolver = EntityResolver(Store(), FixedEmbedder())
    first = resolver.resolve("first", "concept")
    second = resolver.resolve("second", "concept")
    assert second.outcome == "auto-resolve"
    assert second.canonical == first.canonical


def test_new_entity_is_created_and_exact_session_alias_reuses_it_without_embedding():
    store = Store()
    resolver = EntityResolver(store, Embedder(), SessionRegistry())
    first = resolver.resolve("Novel", "concept")
    second = resolver.resolve(" novel ", "concept")
    assert first.outcome == "create" and second.outcome == "auto-resolve"
    assert second.canonical == first.canonical
    assert store.embedding_calls == 1  # first lookup; the session hit avoids a second lookup


# --- issue #99: type canonicalization, containment matching, alias accumulation ---


def _registry(politician_is_person=True):
    from principle_graph.label_registry import LabelEntry, LabelRegistry
    entries = {
        "person": LabelEntry("person", ("politician",) if politician_is_person else (), None, ""),
    }
    return LabelRegistry(1, entries)


class V2Store(Store):
    """Store fake extended with the containment + alias seams (issue #99)."""

    def __init__(self, entities=(), similar=(), evidence=0, containment=()):
        super().__init__(entities, similar, evidence)
        self.containment = list(containment)
        self.containment_queries = []
        self.alias_calls = []

    def containment_candidates(self, name, entity_type):
        self.containment_queries.append((name, entity_type))
        return [e for e in self.containment if e.type == entity_type]

    def add_alias(self, entity, alias):
        self.alias_calls.append((entity.id, alias))


def _unit(vectors):
    n = math.sqrt(sum(x * x for x in vectors))
    return [x / n for x in vectors]


def test_type_canonicalization_applies_before_matching():
    store = V2Store(entities=[Entity("e1", "Friedrich Merz", "person")])
    resolver = EntityResolver(store, entity_registry=_registry())
    result = resolver.resolve("Friedrich Merz", "politician")
    assert result.outcome == "auto-resolve"
    assert result.canonical.type == "person"
    assert store.containment_queries == []  # exact match on canonicalized type; no containment pass
    assert store.entities and True


def test_type_canonicalization_queries_store_with_canonical_type():
    store = V2Store()
    resolver = EntityResolver(store, entity_registry=_registry())
    resolver.resolve("Someone New", "politician")
    assert store.containment_queries and store.containment_queries[0][1] == "person"


def test_unknown_entity_type_passes_through_flagged():
    store = V2Store()
    resolver = EntityResolver(store, entity_registry=_registry())
    result = resolver.resolve("Obscurity", "xenosophy")
    assert result.canonical.type == "xenosophy"
    assert resolver.unknown_type_counts == {"xenosophy": 1}


def test_containment_merges_with_embedding_corroboration():
    merz = Entity("e1", "Friedrich Merz", "person", embedding=_unit([1.0, 0.05]))
    store = V2Store(containment=[merz])
    resolver = EntityResolver(store, entity_registry=_registry())
    result = resolver.resolve("Merz", "person", embedding=_unit([1.0, 0.0]))
    assert result.outcome == "auto-resolve"
    assert result.canonical == merz
    assert result.matches[0].method == "containment"
    assert store.alias_calls == [("e1", "Merz")]


def test_containment_without_corroboration_queues():
    merz = Entity("e1", "Friedrich Merz", "person", embedding=_unit([1.0, 0.0]))
    store = V2Store(containment=[merz])
    resolver = EntityResolver(store, entity_registry=_registry())
    result = resolver.resolve("Merz", "person", embedding=_unit([0.0, 1.0]))
    assert result.outcome == "ambiguity queue"
    assert store.alias_calls == []


def test_containment_without_entity_embedding_queues():
    merz = Entity("e1", "Friedrich Merz", "person")
    store = V2Store(containment=[merz])
    resolver = EntityResolver(store, entity_registry=_registry())
    result = resolver.resolve("Merz", "person", embedding=_unit([1.0, 0.0]))
    assert result.outcome == "ambiguity queue"


def test_multiple_containment_candidates_queue():
    a = Entity("e1", "Friedrich Merz", "person", embedding=_unit([1.0, 0.0]))
    b = Entity("e2", "Angela Merz", "person", embedding=_unit([1.0, 0.0]))
    store = V2Store(containment=[a, b])
    resolver = EntityResolver(store, entity_registry=_registry())
    result = resolver.resolve("Merz", "person", embedding=_unit([1.0, 0.0]))
    assert result.outcome == "ambiguity queue"


def test_session_containment_merges_with_corroboration():
    class FixedEmbedder:
        def embed(self, text):
            return _unit([1.0, 0.0]) if text == "Friedrich Merz" else _unit([1.0, 0.02])
    resolver = EntityResolver(V2Store(), FixedEmbedder(), entity_registry=_registry())
    first = resolver.resolve("Friedrich Merz", "person")
    assert first.outcome == "create"
    second = resolver.resolve("Merz", "person")
    assert second.outcome == "auto-resolve"
    assert second.canonical == first.canonical


def test_session_containment_without_corroboration_queues():
    class FixedEmbedder:
        def embed(self, text):
            return _unit([1.0, 0.0]) if text == "Friedrich Merz" else _unit([0.0, 1.0])
    resolver = EntityResolver(V2Store(), FixedEmbedder(), entity_registry=_registry())
    resolver.resolve("Friedrich Merz", "person")
    result = resolver.resolve("Merz", "person")
    assert result.outcome == "ambiguity queue"


def test_merged_surface_form_accumulates_in_session_registry():
    merz = Entity("e1", "Friedrich Merz", "person", embedding=_unit([1.0, 0.0]))
    store = V2Store(containment=[merz])
    registry = SessionRegistry()
    resolver = EntityResolver(store, entity_registry=_registry(), registry=registry)
    resolver.resolve("Merz", "person", embedding=_unit([1.0, 0.0]))
    assert registry.lookup("Merz", "person") == merz


def test_fuzzy_store_merge_accumulates_alias():
    entity = Entity("e1", "Federal Reserve", "organization")
    store = V2Store(entities=[entity])
    resolver = EntityResolver(store)
    resolver.resolve("Federal Reserv", "organization")
    assert store.alias_calls == [("e1", "Federal Reserv")]


def test_exact_name_match_does_not_reaccumulate_alias():
    entity = Entity("e1", "Federal Reserve", "organization")
    store = V2Store(entities=[entity])
    resolver = EntityResolver(store)
    resolver.resolve("Federal Reserve", "organization")
    assert store.alias_calls == []


def test_same_run_session_merge_accumulates_alias_on_resolution():
    """P1 fix: a same-run session merge records the surface form (AC-1/AC-3)."""
    store = V2Store()
    registry = SessionRegistry()
    resolver = EntityResolver(store, registry=registry)
    first = resolver.resolve("Friedrich Merz", "person", embedding=_unit([1.0, 0.05]))
    result = resolver.resolve("Merz", "person", embedding=_unit([1.0, 0.0]))
    assert result.outcome == "auto-resolve" and result.canonical == first.canonical
    assert result.new_aliases == ("Merz",)
    assert registry.lookup("Merz", "person") == first.canonical


class NameKeyedStore(V2Store):
    """Store fake keyed like the real seam: name and alias matches only."""

    def __init__(self, nodes=()):
        super().__init__()
        self.nodes = list(nodes)

    def find_entities(self, name, entity_type):
        normalized = normalize_name(name)
        return [e for e in self.nodes if e.type == entity_type and (
            normalize_name(e.name) == normalized
            or any(normalize_name(a) == normalized for a in e.aliases))]


def test_persisted_alias_round_trip_on_name_keyed_store():
    """AC-3: a recorded alias auto-resolves on a later ingest, no corroboration."""
    merz = Entity("e1", "Friedrich Merz", "person", aliases=("Merz",))
    resolver = EntityResolver(NameKeyedStore(nodes=[merz]))
    result = resolver.resolve("Merz", "person")
    assert result.outcome == "auto-resolve"
    assert result.canonical == merz
