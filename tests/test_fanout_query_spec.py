import unittest
from pathlib import Path


SPEC = (Path(__file__).parents[1] / "docs/fanout-query.md").read_text()


class FanoutQuerySpecTests(unittest.TestCase):
    def test_spec_defines_input_seed_matching_and_candidate_shape(self):
        for term in ("seed", "normalized name", "aliases", "candidate direction", "confidence", "scope conditions"):
            self.assertIn(term, SPEC)

    def test_spec_defines_bounded_ranking_and_top_k(self):
        for term in ("top-K", "breadth", "deduplicate", "descending"):
            self.assertIn(term, SPEC)

    def test_spec_defines_markdown_and_json_output(self):
        self.assertIn("Markdown", SPEC)
        self.assertIn("JSON", SPEC)
        self.assertIn("no matching seeds", SPEC)


if __name__ == "__main__":
    unittest.main()
