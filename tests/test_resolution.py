from principle_graph.resolution import Entity, EntityResolver, SessionRegistry, SimilarEntity


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
