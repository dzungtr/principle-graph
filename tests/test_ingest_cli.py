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

from principle_graph.review import GraphDelta, ReviewResult

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
        self.repeat_mode = "keep-first"
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


@pytest.fixture(autouse=True)
def _novelty_gate_off(monkeypatch):
    """Issue #93 tests here exercise non-novelty surfaces; opt the gate out and
    keep the key check deterministic regardless of the ambient environment."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("principle_graph.cli._novelty_filter_for",
                        lambda settings, no_novelty_filter: None)


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
        # Issue #80 trigger surfacing reads the review-approved delta; empty here.
        self.delta = GraphDelta()
        self.review = ReviewResult(approved=GraphDelta(), rejected=[])
        self.graph = None  # no writer; the trigger notice is skipped


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
        lambda _s, repeat_mode=None, novelty_filter=None: (_FakeOrchestrator("approved"), _FakeWriter()),
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
        lambda _s, repeat_mode=None, novelty_filter=None: (_FakeOrchestrator("rejected"), _FakeWriter()),
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
        lambda _s, repeat_mode=None, novelty_filter=None: (orchestrator, _FakeWriter()),
    )

    def scripted(_prompt: str) -> str:
        return "approve"

    out = io.StringIO()
    with redirect_stdout(out):
        ingest_command(settings, str(source), input_fn=scripted, out=out)
    assert orchestrator.received_input_fn is scripted


# ---------------------------------------------------------------------------
# Mode-2 review interaction: interactive default, --yes opt-in.
# ---------------------------------------------------------------------------


def test_ingest_command_defaults_to_interactive_review(monkeypatch, tmp_path: Path):
    """With no --yes and no explicit input_fn, the terminal review loop is wired."""
    import builtins

    settings = _settings(uri="bolt://127.0.0.1:1")
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    orchestrator = _FakeOrchestrator("approved")
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s, repeat_mode=None, novelty_filter=None: (orchestrator, _FakeWriter()),
    )
    out = io.StringIO()
    with redirect_stdout(out):
        ingest_command(settings, str(source), out=out)
    assert orchestrator.received_input_fn is not None
    # Behavioural check: the wired input reads the terminal, not a scripted feed.
    monkeypatch.setattr(builtins, "input", lambda _prompt: "a")
    assert orchestrator.received_input_fn("prompt") == "a"


def test_ingest_command_yes_opts_into_scripted_approval(monkeypatch, tmp_path: Path):
    settings = _settings(uri="bolt://127.0.0.1:1")
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    orchestrator = _FakeOrchestrator("approved")
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s, repeat_mode=None, novelty_filter=None: (orchestrator, _FakeWriter()),
    )
    out = io.StringIO()
    with redirect_stdout(out):
        ingest_command(settings, str(source), yes=True, out=out)
    assert orchestrator.received_input_fn is not None
    assert orchestrator.received_input_fn("prompt") == "approve"


def test_interactive_input_eof_suggests_yes(monkeypatch):
    """Piped stdin without --yes must fail with the opt-in hint, never auto-approve."""
    import builtins

    from principle_graph.cli import _interactive_input

    def _eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)
    with pytest.raises(RuntimeError) as exc_info:
        _interactive_input("prompt")
    assert "--yes" in str(exc_info.value)


def test_main_ingest_wires_yes_flag(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def _fake_ingest(settings, path, *, yes=False, input_fn=None, out=None,
                     repeat_mode=None, no_novelty_filter=False):
        captured["yes"] = yes
        captured["path"] = path
        captured["repeat_mode"] = repeat_mode
        captured["no_novelty_filter"] = no_novelty_filter
        return 0

    monkeypatch.setattr("principle_graph.cli.ingest_command", _fake_ingest)
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    assert main(["ingest", str(source)]) == 0
    assert captured["yes"] is False and captured["repeat_mode"] is None
    assert main(["ingest", str(source), "--yes"]) == 0
    assert captured["yes"] is True
    assert main(["ingest", str(source), "--no-novelty-filter"]) == 0
    assert captured["no_novelty_filter"] is True


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


# ---------------------------------------------------------------------------
# Repeat-mode surfaces (issue #59): flag > PG_REPEAT_MODE > keep-first default.
# ---------------------------------------------------------------------------


def test_ingest_command_invalid_flag_mode_fails_fast_before_any_write(monkeypatch, tmp_path: Path):
    settings = _settings()
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr("principle_graph.cli.preflight",
                        lambda _s: calls.append("preflight"))
    monkeypatch.setattr("principle_graph.cli.build_orchestrator",
                        lambda _s, repeat_mode=None: calls.append("build") or (_FakeOrchestrator("approved"), _FakeWriter()))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ingest_command(settings, str(source), repeat_mode="overwrite", out=out)
    assert code == 2
    assert "overwrite" in err.getvalue()
    assert "keep-first" in err.getvalue() and "refresh" in err.getvalue()
    assert calls == []  # no pre-flight, no orchestrator, no write


def test_ingest_command_invalid_env_mode_fails_fast_before_any_write(monkeypatch, tmp_path: Path):
    from principle_graph.config import Settings
    monkeypatch.setenv("PG_REPEAT_MODE", "overwrite")
    settings = Settings.from_env()
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight",
                        lambda _s: (_ for _ in ()).throw(AssertionError("preflight reached")))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ingest_command(settings, str(source), out=out)
    assert code == 2
    assert "overwrite" in err.getvalue()


def test_ingest_command_flag_overrides_env(monkeypatch, tmp_path: Path):
    from principle_graph.config import Settings
    monkeypatch.setenv("PG_REPEAT_MODE", "refresh")
    settings = Settings.from_env()
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    received: list[str | None] = []
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s, repeat_mode=None, novelty_filter=None: received.append(repeat_mode)
        or (_FakeOrchestrator("approved"), _FakeWriter()),
    )
    with redirect_stdout(io.StringIO()):
        code = ingest_command(settings, str(source), repeat_mode="keep-first")
    assert code == 0
    assert received == ["keep-first"]


def test_ingest_command_env_mode_flows_to_writer_when_flag_absent(monkeypatch, tmp_path: Path):
    from principle_graph.config import Settings
    monkeypatch.setenv("PG_REPEAT_MODE", "refresh")
    settings = Settings.from_env()
    source = tmp_path / "demo.md"
    source.write_text("# Demo", encoding="utf-8")
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    received: list[str | None] = []
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s, repeat_mode=None, novelty_filter=None: received.append(repeat_mode)
        or (_FakeOrchestrator("approved"), _FakeWriter()),
    )
    with redirect_stdout(io.StringIO()):
        ingest_command(settings, str(source))
    assert received == ["refresh"]


def test_build_orchestrator_passes_mode_to_writer(monkeypatch):
    from principle_graph import cli
    from principle_graph.neo4j import Neo4jGraphWriter

    class _Driver:
        def session(self, database=None):
            raise AssertionError("no session expected while composing")

    monkeypatch.setattr(cli, "_driver", lambda _s: _Driver())
    monkeypatch.setattr(cli, "_build_embedder", lambda _s: None)
    settings = _settings()
    refresh_orchestrator, _ = cli.build_orchestrator(settings, repeat_mode="refresh")
    assert refresh_orchestrator.writer.repeat_mode == "refresh"
    default_orchestrator, _ = cli.build_orchestrator(settings)
    assert default_orchestrator.writer.repeat_mode == "keep-first"
    assert isinstance(refresh_orchestrator.writer, Neo4jGraphWriter)


def test_ingest_help_documents_repeat_mode_and_precedence():
    from contextlib import redirect_stdout, suppress
    buf = io.StringIO()
    with redirect_stdout(buf), suppress(SystemExit):
        main(["ingest", "--help"])
    text = buf.getvalue()
    assert "--repeat-mode" in text
    assert "keep-first" in text and "refresh" in text
    assert "PG_REPEAT_MODE" in text  # precedence documented in the help itself


# ---------------------------------------------------------------------------
# Decide-mode notice derives from review.approved (PR #89 review P2)
# ---------------------------------------------------------------------------


def _ingest_result(delta, approved, verdict="approved"):
    from principle_graph.orchestrator import IngestResult, IngestStats
    from principle_graph.review import GraphDelta, ReviewResult

    stats = IngestStats(
        source="demo.md", chunks_sequential=[], extraction_requests=0,
        embedding_requests=0, ambiguity_queued=0, ambiguity_notes=(),
        verdict=verdict, committed_entities=0, committed_edges=0,
        rejected_count=0, rejected_log_path="", elapsed_seconds=0.0,
    )
    return IngestResult(stats=stats, delta=delta,
                        review=ReviewResult(approved=approved, rejected=[]),
                        graph=None)


def _edge(domain):
    from principle_graph.review import GraphEdge
    return GraphEdge(subject="S", relation="CAUSES", object="O",
                     confidence=0.7, source_ref="book-1:ch-1", domain=domain)


def test_ingest_notice_uses_approved_edges_not_pre_review_delta(monkeypatch, tmp_path):
    """A rejected ingest must not advertise fact-check candidates that were
    never committed: domains derive from review.approved, not result.delta."""
    from principle_graph import cli

    rejected_delta = GraphDelta(new_edges=[_edge("economics")])
    approved_empty = GraphDelta()
    result = _ingest_result(rejected_delta, approved_empty, verdict="rejected")

    class _FakeOrchestrator:
        def run(self, _path, input_fn=None):
            return result

    monkeypatch.setattr(cli, "preflight", lambda _s: None)
    monkeypatch.setattr(
        cli, "build_orchestrator",
        lambda _s, repeat_mode=None, novelty_filter=None: (_FakeOrchestrator(), _FakeWriter()))
    settings = _settings()
    source = tmp_path / "demo.md"
    source.write_text("# Demo\n\nbody", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.ingest_command(settings, str(source), yes=True, out=out)
    assert code == 4  # rejected
    assert "Fact-check candidates" not in out.getvalue()


def test_ingest_notice_fires_for_approved_domain_rows(monkeypatch, tmp_path):
    """Approved rows in a domain with pre-existing rows from another source
    surface the fact-check advisory exactly once per ingest."""
    from principle_graph import cli
    from principle_graph.factcheck import LedgerRow

    approved = GraphDelta(new_edges=[_edge("economics")])
    result = _ingest_result(approved, approved)

    class _Graph:
        def rows_for_domain(self, _domain):
            return [LedgerRow("T", "CAUSES", "U", "book-2:ch-1", 0.6,
                              "", "", "economics"),
                    LedgerRow("V", "CAUSES", "W", "book-3:ch-1", 0.6,
                              "", "", "economics")]

    # Rebuild the result against the writer seam the notice reads.
    result = type(result)(stats=result.stats, delta=result.delta,
                          review=result.review, graph=_Graph())

    class _Orch:
        def run(self, _path, input_fn=None):
            return result

    monkeypatch.setattr(cli, "preflight", lambda _s: None)
    monkeypatch.setattr(
        cli, "build_orchestrator", lambda _s, repeat_mode=None, novelty_filter=None: (_Orch(), _FakeWriter()))
    settings = _settings()
    source = tmp_path / "demo.md"
    source.write_text("# Demo\n\nbody", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.ingest_command(settings, str(source), yes=True, out=out)
    assert code == 0
    assert "Fact-check candidates" in out.getvalue()


# ---------------------------------------------------------------------------
# Folder ingest: directory of markdown files.
# ---------------------------------------------------------------------------


class _RecordingOrchestrator:
    """Records the source paths and input_fn it is run with, per call."""

    def __init__(self, verdicts: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._verdicts = verdicts or {}

    def run(self, source_path, *, input_fn=None):
        self.calls.append((str(source_path), input_fn))
        return _FakeResult(self._verdicts.get(source_path.name, "approved"))


def _folder_orchestrator(monkeypatch, orchestrator):
    monkeypatch.setattr("principle_graph.cli.preflight", lambda _s: None)
    monkeypatch.setattr(
        "principle_graph.cli.build_orchestrator",
        lambda _s, repeat_mode=None, novelty_filter=None: (orchestrator, _FakeWriter()),
    )


def test_ingest_directory_runs_each_markdown_file(tmp_path: Path, monkeypatch):
    """Directory ingest runs the orchestrator once per .md/.markdown file."""
    orchestrator = _RecordingOrchestrator()
    _folder_orchestrator(monkeypatch, orchestrator)
    folder = tmp_path / "notes"
    folder.mkdir()
    for name in ("b.md", "a.md", "c.markdown"):
        (folder / name).write_text("# Demo", encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        code = ingest_command(_settings(), str(folder), yes=True, out=out)
    assert code == 0
    # Sorted by filename for deterministic order.
    assert [Path(p).name for p, _ in orchestrator.calls] == ["a.md", "b.md", "c.markdown"]
    # Per-file stats blocks, then the aggregate summary line.
    assert out.getvalue().count("transcript-verdict=approved") == 3
    assert "Ingested 3 files: OK 3, skipped 0" in out.getvalue()


def test_ingest_empty_directory_fails_fast_exit_2(tmp_path: Path, monkeypatch):
    """Empty directory exits 2 before any pre-flight or orchestrator build."""
    built = []

    def _spy(_s, repeat_mode=None, novelty_filter=None):
        built.append(True)
        return (_RecordingOrchestrator(), _FakeWriter())

    monkeypatch.setattr("principle_graph.cli.build_orchestrator", _spy)
    folder = tmp_path / "empty"
    folder.mkdir()
    err = io.StringIO()
    with redirect_stderr(err):
        code = ingest_command(_settings(), str(folder))
    assert code == 2
    assert "No .md/.markdown files found in directory" in err.getvalue()
    assert not built  # pre-flight/model spend never reached


def test_ingest_directory_skips_unsupported_file_with_notice(tmp_path: Path, monkeypatch):
    """An unsupported file inside the folder is skipped with a stderr notice."""
    orchestrator = _RecordingOrchestrator()
    _folder_orchestrator(monkeypatch, orchestrator)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "good.md").write_text("# Demo", encoding="utf-8")
    (folder / "image.pdf").write_bytes(b"%PDF-1.4")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ingest_command(_settings(), str(folder), yes=True, out=out)
    assert code == 0
    assert [Path(p).name for p, _ in orchestrator.calls] == ["good.md"]
    assert "Skipping unsupported file: image.pdf" in err.getvalue()
    assert "Ingested 1 files: OK 1, skipped 1" in out.getvalue()


def test_ingest_directory_is_non_recursive_and_sorted(tmp_path: Path, monkeypatch):
    """Subdirectories are ignored; ordering follows sorted filenames."""
    orchestrator = _RecordingOrchestrator()
    _folder_orchestrator(monkeypatch, orchestrator)
    folder = tmp_path / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "nested.md").write_text("# Demo", encoding="utf-8")
    (folder / "z.md").write_text("# Demo", encoding="utf-8")
    (folder / "a.md").write_text("# Demo", encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        code = ingest_command(_settings(), str(folder), yes=True, out=out)
    assert code == 0
    assert [Path(p).name for p, _ in orchestrator.calls] == ["a.md", "z.md"]
    # Deterministic across two runs of the same folder.
    out2 = io.StringIO()
    with redirect_stdout(out2):
        ingest_command(_settings(), str(folder), yes=True, out=out2)
    assert [Path(p).name for p, _ in orchestrator.calls] == ["a.md", "z.md", "a.md", "z.md"]


def test_ingest_directory_keep_first_repeat_keyed_per_filename(tmp_path: Path, monkeypatch):
    """Each folder file reaches the orchestrator with its own filename path, so
    keep-first repeat mode (keyed by source_id = filename) still applies per file."""
    orchestrator = _RecordingOrchestrator({"a.md": "approved", "b.md": "rejected"})
    _folder_orchestrator(monkeypatch, orchestrator)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("# Demo", encoding="utf-8")
    (folder / "b.md").write_text("# Demo", encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        code = ingest_command(_settings(), str(folder), yes=True, out=out)
    # One rejected file -> nonzero exit, but both files were attempted.
    assert code == 4
    assert [Path(p).name for p, _ in orchestrator.calls] == ["a.md", "b.md"]
    assert "transcript-verdict=approved" in out.getvalue()
    assert "transcript-verdict=rejected" in out.getvalue()
    assert "Ingested 2 files: OK 1, skipped 0, failed 1" in out.getvalue()
