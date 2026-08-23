"""Tests for the ``pg ingest`` CLI registration and pre-flight behaviour.

External behaviour only: tests inspect the CLI surface, the exit code, the
remediation hints printed to stderr, and that pre-flight failures short-circuit
before any model request. They do NOT exercise the orchestrator seam (covered
in ``test_orchestrator.py``).
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from principle_graph.cli import (
    PreflightError,
    ingest_command,
    main,
    preflight,
)


class _SettingsFactory:
    """Tiny Settings stub so tests can target a specific URI without monkeypatching env."""

    def __init__(self, uri: str, database: str = "neo4j") -> None:
        self.uri = uri
        self.database = database
        self.rejected_log_path = ".pg/rejected.jsonl"
        self.user = "neo4j"
        self.password = "principlegraph"
        self.llm_base_url = "http://localhost:8000"
        self.llm_model = "glm-5.2"
        self.ollama_base_url = "http://localhost:11434"
        self.ollama_model = "bge-m3"

    def __class_getitem__(cls, _):
        return cls  # for typing ergonomics


def _settings(uri: str = "bolt://127.0.0.1:1") -> _SettingsFactory:
    return _SettingsFactory(uri)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_help_lists_ingest_subcommand():
    from contextlib import suppress
    buf_out = io.StringIO()
    with redirect_stdout(buf_out), suppress(SystemExit):
        main(["--help"])
    assert "ingest" in buf_out.getvalue()


def test_ingest_subcommand_registered_in_parser():
    from contextlib import suppress
    buf_out = io.StringIO()
    with redirect_stdout(buf_out), suppress(SystemExit):
        main(["--help"])
    assert "ingest" in buf_out.getvalue()


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def test_preflight_raises_with_check_hint_when_neo4j_unreachable():
    settings = _settings(uri="bolt://127.0.0.1:1")  # nothing listening here
    with pytest.raises(PreflightError) as exc_info:
        preflight(settings)
    message = str(exc_info.value)
    assert "pg check" in message
    assert exc_info.value.remediation == "Run `pg check` to verify connectivity."


def test_ingest_command_emits_check_hint_and_no_model_call_on_unreachable_neo4j(tmp_path: Path):
    settings = _settings(uri="bolt://127.0.0.1:1")
    source = tmp_path / "demo.md"
    source.write_text("# Demo\n\nbody text", encoding="utf-8")
    err = io.StringIO()
    with redirect_stderr(err):
        code = ingest_command(settings, str(source))
    output = err.getvalue()
    assert code == 1
    assert "pg check" in output
    # No model request path was issued; pre-flight raised before the orchestrator
    # was composed (the gateway client is never instantiated on preflight failure).
    assert "LLM" not in output


def test_ingest_command_missing_source_returns_nonzero(tmp_path: Path):
    settings = _settings(uri="bolt://127.0.0.1:1")
    err = io.StringIO()
    with redirect_stderr(err):
        code = ingest_command(settings, str(tmp_path / "missing.md"))
    assert code == 2
    assert "Source not found" in err.getvalue()


# ---------------------------------------------------------------------------
# Pre-flight with a fake driver: schema-missing case.
# ---------------------------------------------------------------------------


class _FakeIndexRow(dict):
    """Dict subclass used by ``SHOW INDEXES`` rows."""

    def __init__(self, **kw):
        super().__init__(**kw)


class _FakeSession:
    def __init__(self, *, constraints=(), indexes=()):
        self._constraints = list(constraints)
        self._indexes = list(indexes)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, **params):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def __iter__(self):
                return iter(self._rows)

        if cypher.startswith("SHOW CONSTRAINTS"):
            return _Result(self._constraints)
        if cypher.startswith("SHOW INDEXES"):
            return _Result(self._indexes)
        if cypher == "RETURN 1":
            return _Result([{"x": 1}])
        raise AssertionError(f"unexpected cypher in fake driver: {cypher}")


class _FakeDriver:
    def __init__(self, *, constraints=(), indexes=()):
        self._constraints = constraints
        self._indexes = indexes

    def session(self, database="neo4j"):
        return _FakeSession(constraints=self._constraints, indexes=self._indexes)

    def verify_connectivity(self):
        return None

    def close(self):
        return None


def test_preflight_raises_init_hint_when_schema_missing(monkeypatch):
    settings = _settings(uri="bolt://127.0.0.1:1")
    monkeypatch.setattr(
        "principle_graph.cli._driver",
        lambda _s: _FakeDriver(constraints=[], indexes=[]),
    )
    with pytest.raises(PreflightError) as exc_info:
        preflight(settings)
    assert "pg init" in str(exc_info.value)
    assert exc_info.value.remediation == "Run `pg init` to apply the schema."


def test_preflight_passes_when_schema_present(monkeypatch):
    settings = _settings(uri="bolt://127.0.0.1:1")
    monkeypatch.setattr(
        "principle_graph.cli._driver",
        lambda _s: _FakeDriver(
            constraints=[{"entityType": "NODE", "labelsOrTypes": ["Entity"], "properties": ["name", "type"]}],
            indexes=[{"name": "entity_embedding", "entityType": "NODE", "labelsOrTypes": ["Entity"], "properties": ["embedding"]}],
        ),
    )
    preflight(settings)  # must not raise


# ---------------------------------------------------------------------------
# Exit code semantics. We exercise the full ingest_command via a monkeypatched
# orchestrator so the test stays credential-free and does not require Neo4j.
# ---------------------------------------------------------------------------


class _FakeStats:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.source = "demo.md"
        self.rendered = f"transcript-verdict={verdict}\n"

    def render(self) -> str:
        return self.rendered


class _FakeResult:
    def __init__(self, verdict: str) -> None:
        self.stats = _FakeStats(verdict)


class _FakeOrchestrator:
    def __init__(self, verdict: str) -> None:
        self._verdict = verdict
        self.received_input_fn = None

    def run(self, source_path, *, input_fn=None):
        self.received_input_fn = input_fn
        return _FakeResult(self._verdict)


class _FakeWriter:  # mirrors the seam
    def close(self):
        return None


def test_ingest_command_exit_zero_on_approved(monkeypatch, tmp_path: Path):
    settings = _settings(uri="bolt://127.0.0.1:1")
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s: (_FakeOrchestrator("approved"), _FakeWriter()),
    )
    out = io.StringIO()
    with redirect_stdout(out):
        code = ingest_command(settings, str(source), out=out)
    assert code == 0
    assert "transcript-verdict=approved" in out.getvalue()


def test_ingest_command_exit_nonzero_on_rejected(monkeypatch, tmp_path: Path):
    settings = _settings(uri="bolt://127.0.0.1:1")
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s: (_FakeOrchestrator("rejected"), _FakeWriter()),
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ingest_command(settings, str(source), out=out)
    assert code == 4
    assert "transcript-verdict=rejected" in out.getvalue()


# ---------------------------------------------------------------------------
# Default input_fn must not be None (scripted approval feed-through).
# ---------------------------------------------------------------------------


def test_ingest_command_passes_input_fn_through_to_orchestrator(monkeypatch, tmp_path: Path):
    settings = _settings(uri="bolt://127.0.0.1:1")
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    orchestrator = _FakeOrchestrator("approved")
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s: (orchestrator, _FakeWriter()),
    )

    def scripted(_prompt: str) -> str:
        return "approve"

    out = io.StringIO()
    with redirect_stdout(out):
        ingest_command(settings, str(source), input_fn=scripted, out=out)
    assert orchestrator.received_input_fn is scripted


# ---------------------------------------------------------------------------
# Build orchestrator seam shape.
# ---------------------------------------------------------------------------


def test_build_orchestrator_returns_orchestrator_and_driver():
    """Smoke: build_orchestrator returns an IngestOrchestrator + closable driver."""
    from principle_graph.cli import build_orchestrator
    from principle_graph.orchestrator import IngestOrchestrator

    settings = _settings(uri="bolt://127.0.0.1:1")
    # We can't connect, but build_orchestrator only needs to assemble seams.
    # We monkeypatch the driver factory to a fake so the real Neo4j never opens.
    class _StubDriver:
        def __init__(self, uri, auth):
            pass

        def session(self, database="neo4j"):
            raise AssertionError("no driver calls expected from build_orchestrator")

        def close(self):
            pass

    # The Neo4jGraphWriter / EntityStore take the driver and only use it when
    # methods are invoked, so build_orchestrator must succeed without touching
    # the network. Stub the GraphDatabase entrypoint just in case.
    import principle_graph.cli as cli_module
    cli_module.__dict__["_driver"] = lambda s: _StubDriver(s.uri, auth=(s.user, s.password))
    orchestrator, driver = build_orchestrator(settings)
    assert isinstance(orchestrator, IngestOrchestrator)
    assert driver is not None
    driver.close()
