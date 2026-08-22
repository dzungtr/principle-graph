import unittest
from pathlib import Path


SPEC = (Path(__file__).parents[1] / "docs/entity-resolution.md").read_text()


class EntityResolutionSpecTests(unittest.TestCase):
    def test_spec_defines_three_layer_order_and_thresholds(self):
        self.assertIn("Alias/name", SPEC)
        self.assertIn("0.90", SPEC)
        self.assertIn("Semantic similarity", SPEC)
        self.assertIn("0.85", SPEC)
        self.assertIn("Structural corroboration", SPEC)

    def test_spec_defines_outcomes_and_ambiguity_queue(self):
        for outcome in ("auto-resolve", "create", "ambiguity queue"):
            self.assertIn(outcome, SPEC)
        self.assertIn("human\nconfirmation", SPEC)

    def test_spec_distinguishes_within_session_resolution(self):
        self.assertIn("Within-session", SPEC)
        self.assertIn("permanent graph", SPEC)
        self.assertIn("scratch registry", SPEC)


if __name__ == "__main__":
    unittest.main()
