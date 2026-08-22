import re
import unittest
from pathlib import Path


POLICY = (Path(__file__).parents[1] / "docs/confidence-policy.md").read_text()


class ConfidencePolicyTests(unittest.TestCase):
    def test_policy_makes_repeat_edge_identity_and_aggregation_explicit(self):
        self.assertIn("single relationship", POLICY)
        self.assertIn("1 -", POLICY)
        self.assertIn("independent", POLICY)
        self.assertRegex(POLICY, r"0\.0.*1\.0")

    def test_policy_retains_accepted_evidence_and_rejected_delta(self):
        self.assertIn("evidence", POLICY)
        self.assertIn("considered and rejected", POLICY)
        self.assertIn("never committed", POLICY)

    def test_policy_preserves_scope_and_source_provenance(self):
        self.assertIn("scope_conditions", POLICY)
        self.assertIn("source_ref", POLICY)
        self.assertIn("provenance", POLICY)


if __name__ == "__main__":
    unittest.main()
