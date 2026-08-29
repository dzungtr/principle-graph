import unittest

from principle_graph.config import Settings
from principle_graph.fanout import (
    QUERY_SEED_SIMILARITY,
    query_directions,
)
from principle_graph.resolution import EMBEDDING_SIMILARITY, Entity


def _graph_with_embeddings():
    entities = [
        Entity("topic:Rates", "bank's lending rate", "topic", embedding=(1.0, 0.0)),
        Entity("topic:Stocks", "equities market", "topic", embedding=(0.0, 1.0)),
        Entity("topic:Bonds", "sovereign debt", "topic"),
    ]
    return entities


class ListGraph:
    def __init__(self, entities):
        self._entities = entities

    def entities(self):
        return list(self._entities)

    def edges_for(self, entity):
        return []


class FakeEmbedder:
    """Deterministic embedder for testing; embeds to a fixed vector."""

    def __init__(self, vector):
        self.vector = vector
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        return self.vector


class UnavailableEmbedder:
    def embed(self, text):
        return None


class QuerySeedTests(unittest.TestCase):
    def test_semantic_seeding_fires_below_entity_resolution_threshold(self):
        graph = ListGraph(_graph_with_embeddings())
        # cosine((1,0),(1,0)) = 1.0 here; use a 0.70-similar vector to prove
        # seeding fires at 0.60 where entity resolution (0.85) would not.
        embedder = FakeEmbedder((0.8, 0.5))  # ~cos 0.70 to (1,0)
        seeds, _ = query_directions("interest rates are rising", graph, embedder=embedder)
        self.assertEqual([s.entity.name for s in seeds], ["bank's lending rate"])
        self.assertGreaterEqual(seeds[0].score, QUERY_SEED_SIMILARITY)
        self.assertLess(seeds[0].score, EMBEDDING_SIMILARITY)

    def test_exact_name_match_ranks_first_at_1(self    ):
        graph = ListGraph(_graph_with_embeddings())
        embedder = FakeEmbedder((0.8, 0.5))
        seeds, _ = query_directions("bank's lending rate", graph, embedder=embedder)
        self.assertEqual(seeds[0].score, 1.0)

    def test_threshold_is_independent_and_configurable(self):
        graph = ListGraph(_graph_with_embeddings())
        embedder = FakeEmbedder((0.8, 0.5))
        seeds, _ = query_directions("interest rates are rising", graph,
                                    embedder=embedder, threshold=0.95)
        self.assertEqual(seeds, [])
        # settings carries its own default distinct from resolution's 0.85
        self.assertEqual(Settings.query_seed_similarity, QUERY_SEED_SIMILARITY)
        self.assertNotEqual(QUERY_SEED_SIMILARITY, EMBEDDING_SIMILARITY)

    def test_embedder_unavailable_degrades_with_notice(self):
        graph = ListGraph(_graph_with_embeddings())
        notices = []
        seeds, directions = query_directions("SOFR", graph,
                                             embedder=UnavailableEmbedder(),
                                             notices=notices)
        self.assertEqual(seeds, [])  # no exact match in this graph
        self.assertIn("Ollama embedder unavailable", notices[0])

    def test_exact_match_still_works_without_embedder(self):
        graph = ListGraph(_graph_with_embeddings())
        notices = []
        seeds, _ = query_directions("bank's lending rate", graph, notices=notices)
        self.assertEqual(seeds[0].score, 1.0)
        # exact match short-circuits before the embedder is consulted, no notice
        self.assertEqual(notices, [])


if __name__ == "__main__":
    unittest.main()
