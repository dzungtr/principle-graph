import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
DDL = (ROOT / "docs/schema/neo4j-schema.cypher").read_text()
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

    def test_spec_covers_edge_properties(self):
        for property_name in (
            "confidence",
            "evidence",
            "scope_conditions",
            "source_ref",
            "created_at",
            "updated_at",
        ):
            self.assertIn(f"`{property_name}`", SPEC)


if __name__ == "__main__":
    unittest.main()
