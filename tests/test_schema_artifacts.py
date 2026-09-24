import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
DDL = (ROOT / "src/principle_graph/data/neo4j-schema.cypher").read_text()
SPEC = (ROOT / "docs/schema/neo4j-schema.md").read_text()


class SchemaArtifactTests(unittest.TestCase):
    def test_ddl_defines_entity_identity_constraint(self):
        self.assertRegex(
            DDL,
            re.compile(
                r"CREATE CONSTRAINT entity_name_type_unique IF NOT EXISTS.*"
                r"FOR \(entity:Entity\).*"
                r"REQUIRE \(entity\.name, entity\.type\) IS UNIQUE",
                re.DOTALL,
            ),
        )

    def test_ddl_defines_voyage_vector_index(self):
        self.assertIn("CREATE VECTOR INDEX entity_embedding IF NOT EXISTS", DDL)
        self.assertIn("`vector.dimensions`: 1024", DDL)
        self.assertIn("`vector.similarity_function`: 'cosine'", DDL)

    def test_ddl_defines_extraction_event_source_ref_index(self):
        self.assertRegex(
            DDL,
            re.compile(
                r"CREATE RANGE INDEX extraction_event_source_ref IF NOT EXISTS\s+"
                r"FOR \(event:ExtractionEvent\)\s+ON \(event\.source_ref\)",
                re.DOTALL,
            ),
        )

    def test_ddl_defines_source_id_index(self):
        self.assertRegex(
            DDL,
            re.compile(
                r"CREATE RANGE INDEX source_id IF NOT EXISTS\s+"
                r"FOR \(source:Source\)\s+ON \(source\.id\)",
                re.DOTALL,
            ),
        )

    def test_ddl_defines_state_event_source_ref_index(self):
        self.assertRegex(
            DDL,
            re.compile(
                r"CREATE RANGE INDEX state_event_source_ref IF NOT EXISTS\s+"
                r"FOR \(event:StateEvent\)\s+ON \(event\.source_ref\)",
                re.DOTALL,
            ),
        )

    def test_spec_defines_the_state_ledger(self):
        # Issue #98 schema doc coverage (PR #108 P2-1).
        self.assertIn("### `StateEvent`", SPEC)
        self.assertIn("HAS_STATE_EVENT", SPEC)
        self.assertIn("`state` | map<string, map>", SPEC)

    def test_spec_defines_the_source_node_and_from_source_link(self):
        self.assertIn("### `Source`", SPEC)
        self.assertIn("FROM_SOURCE", SPEC)
        for property_name in ("id", "first_seen"):
            self.assertIn(f"`{property_name}`", SPEC)

    def test_spec_defines_the_two_layer_model(self):
        self.assertIn("### `ExtractionEvent`", SPEC)
        self.assertIn("REPORTED", SPEC)
        self.assertIn("ABOUT", SPEC)
        for property_name in (
            "relation",
            "source_ref",
            "confidence",
            "evidence",
            "scope_conditions",
            "domain",
            "created_at",
            "updated_at",
        ):
            self.assertIn(f"`{property_name}`", SPEC)

    def test_spec_moves_evidence_off_the_arrow(self):
        self.assertNotIn("`evidence` | list<string> | yes", SPEC)

    def test_spec_defines_entity_aliases_and_type_canonicalization(self):
        # Issue #99: merged surface forms accumulate on the canonical entity;
        # type canonicalization happens before matching and at the write boundary.
        self.assertIn("`aliases`", SPEC)
        self.assertIn("entity-registry.yaml", SPEC)
        self.assertIn("before matching", SPEC)


if __name__ == "__main__":
    unittest.main()
