"""Fan-out contract equality: output identical pre- and post-migration.

PRD #57 Testing Decisions group 4 — the non-destructiveness proof. The legacy
side carries provenance on edge properties (pre-#58 shape); the migrated side
strips it and reconstructs provenance through the Arrow→ledger hop using the
same assembly the Neo4j adapter executes (``assemble_provenance``). The output
contract — per rank: relation, neighbor, confidence, scope_conditions,
source_ref, evidence — is pinned to the epic baseline JSON (#57 handoff,
comment 5467062690; full capture in PR #67 thread).
"""
from __future__ import annotations

import json
import unittest

from principle_graph.fanout import query_directions
from principle_graph.ledger import LedgerRow
from principle_graph.neo4j import assemble_provenance
from principle_graph.resolution import Entity
from principle_graph.review import GraphEdge


# Shape fixture: one ranked direction exactly as the CLI emits it for the
# demo graph (epic #57 baseline, query "interest rates", rank 1).
BASELINE_DIRECTION_KEYS = {
    "rank", "seed", "relation", "neighbor", "confidence",
    "scope_conditions", "source_ref", "evidence",
}
BASELINE_DIRECTION_SAMPLE = {
    "rank": 1,
    "seed": "interbank rate",
    "relation": "MAY_DESCRIBE",
    "neighbor": "central bank administered target",
    "confidence": 0.95,
    "scope_conditions": "Term is used loosely; one of several possible meanings.",
    "source_ref": "global-interbank-interest-principal.md:chunk-2",
    "evidence": ["\"A rate may describe ... a central bank's administered target\""],
}


class LegacyGraph:
    """Pre-#58 shape: provenance lives on the arrow's own properties.

    Faithful to the demo graph the #60 backfill actually migrated: one
    evidence string per edge, so the single-row backfill is lossless.
    """

    def __init__(self):
        self.es = [
            Entity("e1", "Rates", "topic", ("interest rates",)),
            Entity("e2", "Target", "topic"),
            Entity("e3", "Other", "topic"),
        ]

    def entities(self):
        return self.es

    def edges_for(self, entity):
        return [
            GraphEdge("Rates", "MAY_DESCRIBE", "Target", 0.95, "doc:chunk-2",
                      ("first claim",), "loose usage"),
            GraphEdge("Other", "HEDGES", "Rates", 0.9, "book:2", ("hedge claim",), "crisis only"),
        ]


class MigratedGraph:
    """Post-#60 shape: arrows carry only derived confidence + scope; provenance
    is reconstructed per arrow through the ledger hop.

    ``rows`` mirrors the backfill contract — one row per edge, seeded from the
    edge's own confidence/evidence/source_ref (arrow values unmoved) — while
    ``corroborated_rows`` exercises the #58 ingest path: two sources, two rows.
    """

    def __init__(self, corroborated: bool = False):
        self.es = LegacyGraph().es
        # Insertion order stands in for created_at ordering (newest last).
        self.rows = [
            LedgerRow("Rates", "MAY_DESCRIBE", "Target", "doc:chunk-2", 0.95, "first claim", "loose usage"),
            LedgerRow("Other", "HEDGES", "Rates", "book:2", 0.9, "hedge claim", "crisis only"),
        ]
        if corroborated:
            self.rows = [
                LedgerRow("Rates", "MAY_DESCRIBE", "Target", "doc:chunk-1", 0.6, "first claim"),
                LedgerRow("Rates", "MAY_DESCRIBE", "Target", "doc:chunk-2", 0.5, "second claim"),
            ]
        self.arrows = [
            # Derived confidence; scope denormalized; no provenance properties.
            GraphEdge("Rates", "MAY_DESCRIBE", "Target",
                      0.8 if corroborated else 0.95, "", (), "loose usage"),
            GraphEdge("Other", "HEDGES", "Rates", 0.9, "", (), "crisis only"),
        ]

    def entities(self):
        return self.es

    def edges_for(self, entity):
        edges = []
        for arrow in self.arrows:
            if entity.name not in (arrow.subject, arrow.object):
                continue
            rows = [r for r in self.rows
                    if (r.subject, r.relation.upper(), r.object)
                    == (arrow.subject, arrow.relation, arrow.object)]
            source_ref, evidence = assemble_provenance(
                [r.source_ref for r in rows], [r.evidence for r in rows],
                arrow.source_ref, arrow.evidence,
            )
            edges.append(GraphEdge(arrow.subject, arrow.relation, arrow.object,
                                   arrow.confidence, source_ref, evidence,
                                   arrow.scope_conditions))
        return edges


def _cli_payload(query, seeds, directions):
    """The exact JSON mapping ``pg query --format json`` emits (cli.py)."""
    return {
        "query": query,
        "seeds": [{"name": s.entity.name, "score": s.score} for s in seeds],
        "directions": [{
            "rank": d.rank, "seed": d.seed, "relation": d.relation,
            "neighbor": d.neighbor, "confidence": d.confidence,
            "scope_conditions": d.scope_conditions, "source_ref": d.source_ref,
            "evidence": list(d.evidence),
        } for d in directions],
    }


class FanoutContractEqualityTests(unittest.TestCase):
    QUERY = "interest rates"

    def test_baseline_direction_sample_conforms_to_contract_shape(self):
        self.assertEqual(set(BASELINE_DIRECTION_SAMPLE), BASELINE_DIRECTION_KEYS)
        self.assertIsInstance(BASELINE_DIRECTION_SAMPLE["evidence"], list)

    def test_ranked_directions_identical_before_and_after_migration(self):
        legacy_seeds, legacy_dirs = query_directions(self.QUERY, LegacyGraph())
        migrated_seeds, migrated_dirs = query_directions(self.QUERY, MigratedGraph())
        self.assertEqual([s.entity.name for s in legacy_seeds],
                         [s.entity.name for s in migrated_seeds])
        self.assertEqual([s.score for s in legacy_seeds], [s.score for s in migrated_seeds])
        self.assertEqual(legacy_dirs, migrated_dirs)

    def test_corroborated_pair_keeps_every_row_queryable_through_the_hop(self):
        # Two sources on one triple (the #58 ingest path): the assembled
        # provenance carries all rows' evidence, newest ref wins, and the
        # derived aggregate makes corroboration visible (PRD stories 1–2, 8).
        # No legacy twin exists: pre-#58, reduce_edges collapsed this state.
        _, dirs = query_directions(self.QUERY, MigratedGraph(corroborated=True))
        may_describe = next(d for d in dirs if d.relation == "MAY_DESCRIBE")
        self.assertEqual(may_describe.confidence, 0.8)  # 1-(1-.6)(1-.5)
        self.assertEqual(may_describe.source_ref, "doc:chunk-2")
        self.assertEqual(may_describe.evidence, ("first claim", "second claim"))

    def test_contract_fields_survive_the_ledger_hop_per_rank(self):
        _, dirs = query_directions(self.QUERY, MigratedGraph())
        may_describe = next(d for d in dirs if d.relation == "MAY_DESCRIBE")
        # Backfilled pair: single row seeded from the edge itself.
        self.assertEqual(may_describe.source_ref, "doc:chunk-2")
        self.assertEqual(may_describe.evidence, ("first claim",))
        self.assertEqual(may_describe.scope_conditions, "loose usage")
        self.assertEqual(may_describe.confidence, 0.95)  # unmoved
        hedges = next(d for d in dirs if d.relation == "HEDGES")
        self.assertEqual(hedges.source_ref, "book:2")
        self.assertEqual(hedges.evidence, ("hedge claim",))

    def test_cli_json_payload_matches_epic_baseline_shape(self):
        seeds, dirs = query_directions(self.QUERY, MigratedGraph())
        payload = _cli_payload(self.QUERY, seeds, dirs)
        self.assertEqual(set(payload), {"query", "seeds", "directions"})
        for direction in payload["directions"]:
            self.assertEqual(set(direction), BASELINE_DIRECTION_KEYS)
        json.dumps(payload)  # serializable, as the contract requires


if __name__ == "__main__":
    unittest.main()
