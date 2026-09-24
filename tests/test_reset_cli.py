"""Tests for the ``pg reset`` CLI command (issue #97, PRD #95 story 25).

External behaviour only, over driver fakes — no database, no network: the
command refuses to run without confirmation, deletes all nodes/rels behind
the flag, and reports Neo4j failures without a traceback.
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout, suppress

from principle_graph.cli import main, reset_command


class _FakeCounters:
    def __init__(self, nodes: int, relationships: int) -> None:
        self.nodes_deleted = nodes
        self.relationships_deleted = relationships


class _FakeSummary:
    def __init__(self, counters: _FakeCounters) -> None:
        self.counters = counters


class _FakeResult:
    def __init__(self, summary: _FakeSummary) -> None:
        self._summary = summary

    def consume(self) -> _FakeSummary:
        return self._summary


class _FakeSession:
    def __init__(self, log: list[str], counters: _FakeCounters) -> None:
        self._log = log
        self._counters = counters

    def run(self, cypher: str, **_params) -> _FakeResult:
        self._log.append(cypher)
        return _FakeResult(_FakeSummary(self._counters))

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *_exc) -> None:
        return None


class _FakeDriver:
    def __init__(self, counters: _FakeCounters | None = None) -> None:
        self.counters = counters or _FakeCounters(0, 0)
        self.executed: list[str] = []

    def session(self, database: str = "neo4j") -> _FakeSession:
        return _FakeSession(self.executed, self.counters)

    def close(self) -> None:
        return None


class _Settings:
    def __init__(self) -> None:
        self.uri = "bolt://127.0.0.1:1"
        self.database = "neo4j"


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_help_lists_reset_subcommand():
    from contextlib import suppress

    buf = io.StringIO()
    with redirect_stdout(buf), suppress(SystemExit):
        main(["--help"])
    assert "reset" in buf.getvalue()


def test_reset_without_confirmation_flag_refuses_and_runs_nothing(monkeypatch):
    driver = _FakeDriver()
    monkeypatch.setattr("principle_graph.cli._driver", lambda _s: driver)
    code, out, err = _run(["reset"])
    assert code == 1
    assert driver.executed == []
    assert "--yes" in err


def test_reset_with_yes_flag_deletes_all_nodes_and_rels(monkeypatch):
    driver = _FakeDriver(counters=_FakeCounters(252, 394))
    monkeypatch.setattr("principle_graph.cli._driver", lambda _s: driver)
    out = io.StringIO()
    code = reset_command(_Settings(), yes=True, out=out)
    assert code == 0
    assert len(driver.executed) == 1
    assert "DETACH DELETE" in driver.executed[0]
    assert "nodes_deleted=252" in out.getvalue()
    assert "relationships_deleted=394" in out.getvalue()


def test_reset_invoked_via_main_with_yes_exits_zero(monkeypatch):
    driver = _FakeDriver()
    monkeypatch.setattr("principle_graph.cli._driver", lambda _s: driver)
    code, _out, err = _run(["reset", "--yes"])
    assert code == 0
    assert err == ""
    assert "DETACH DELETE" in driver.executed[0]


def test_reset_reports_connection_failure_without_traceback(monkeypatch):
    def _boom(_settings):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("principle_graph.cli._driver", _boom)
    code, out, err = _run(["reset", "--yes"])
    assert code == 1
    assert "reset failed" in err
