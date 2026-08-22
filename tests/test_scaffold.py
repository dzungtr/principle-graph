import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class ScaffoldTests(unittest.TestCase):
    def test_project_declares_pg_entrypoint_and_runtime_dependencies(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('name = "principle-graph"', pyproject)
        self.assertIn('pg = "principle_graph.cli:main"', pyproject)
        self.assertIn('neo4j', pyproject)

    def test_cli_help_runs_without_external_services(self):
        result = subprocess.run(
            [sys.executable, "-m", "principle_graph.cli", "--help"],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check", result.stdout)

    def test_compose_uses_neo4j_community_and_persists_data(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("neo4j:5", compose)
        self.assertIn("NEO4J_AUTH", compose)
        self.assertIn("neo4j_data", compose)

    def test_config_reads_environment_defaults(self):
        from principle_graph.config import Settings

        with patch.dict(os.environ, {"NEO4J_URI": "bolt://example:7687", "NEO4J_USER": "alice", "NEO4J_PASSWORD": "secret"}, clear=False):
            settings = Settings.from_env()
        self.assertEqual(settings.uri, "bolt://example:7687")
        self.assertEqual(settings.user, "alice")
        self.assertEqual(settings.password, "secret")


if __name__ == "__main__":
    unittest.main()
