"""Backfill migration behavior (issue #60): one ledger row per existing edge.

External behavior only: observable graph state through a property-graph fake
that executes the real writer logic, plus the CLI surface. Run-twice
idempotency is asserted on full state equality (rows, arrows, timestamps).
Live-graph evidence is recorded in the PR (issue #60 brief: no CI exists).
"""
from __future__ import annotations

import copy
import io
from contextlib import redirect_stdout

import pytest

from principle_graph.cli import main, migrate_ledger_command
from principle_graph.neo4j import Neo4jGraphWriter, assemble_provenance
from principle_graph.reduction import GraphEdge


class _Record(dict):
    """Neo4j-record stand-in: mapping access plus ``get``."""

    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return _Consume()

    def __iter__(self):
        return iter(self._rows)


class _Consume:
    counters = None


class _FakeSession:
    """Executes the exact queries the ledger writer issues, in memory."""

    def __init__(self, graph):
        self.graph = graph

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, query, **params):
        g = self.graph
        if "legacy_source_ref" in query:  # get_edge (checked first: it embeds
            # the same RETURN prefix as the backfill edge load)
            rel = params["relation"]
            arrow = g.arrows.get((params["subject"], rel, params["object"]))
            if arrow is None:
                return _Result([])
            rows_for = [
                (identity, row) for identity, row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
                if identity[:3] == (params["subject"], rel, params["object"])
            ]
            return _Result([_Record(
                subject=params["subject"], relation=rel, object=params["object"],
                confidence=arrow.get("confidence"),
                scope_conditions=arrow.get("scope_conditions"),
                legacy_source_ref=arrow.get("source_ref"),
                legacy_evidence=arrow.get("evidence"),
                row_refs=[src for (_s, _r, _o, src), _row in rows_for],
                row_evidence=[row["evidence"] for _identity, row in rows_for],
            )])
        if "RETURN count(r) AS count" in query:  # strip-count read
            n = sum(1 for key, arrow in g.arrows.items()
                    if self._strip_due(key, arrow, g, self._strip_per_relation(query)))
            return _Result([_Record(count=n)])
        if "RETURN s.name AS subject, type(r) AS relation" in query:
            per_relation = "{relation: type(r)}" in query  # issue #70 guard pin
            def _has_rows(key):
                if per_relation:  # EXISTS matches the arrow's own relation only
                    return any(identity[:3] == key for identity in g.rows)
                return any(  # un-pinned EXISTS matches the (subject, object) pair
                    identity[0] == key[0] and identity[2] == key[2]
                    for identity in g.rows)
            if "NOT EXISTS" in query:  # candidate load: un-migrated arrows only
                items = [(k, a) for k, a in g.arrows.items() if not _has_rows(k)]
            else:
                items = list(g.arrows.items())
            return _Result([
                _Record(subject=s, relation=rel, object=o,
                        confidence=arrow.get("confidence"),
                        evidence=arrow.get("evidence"),
                        scope_conditions=arrow.get("scope_conditions"),
                        source_ref=arrow.get("source_ref"))
                for (s, rel, o), arrow in items
            ])
        if "MERGE (s)-[:REPORTED]->" in query:  # row merge: no ON MATCH side effects
            identity = (params["subject"], params["relation"], params["object"],
                        params["source_ref"])
            if identity not in g.rows:
                g.rows[identity] = {
                    "confidence": params["confidence"],
                    "evidence": params["evidence"],
                    "scope_conditions": params["scope_conditions"],
                    "domain": params["domain"],
                    "created_at": g.tick(),
                    "updated_at": g.tick(),
                }
            return _Result([])
        if "RETURN e.source_ref AS source_ref, e.confidence AS confidence" in query:
            rows = [  # existing ledger rows for the triple, created_at order
                _Record(source_ref=src, confidence=row["confidence"],
                        evidence=row["evidence"], scope_conditions=row["scope_conditions"])
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
                if (s, rel, o) == (params["subject"], params["relation"], params["object"])
            ]
            return _Result(rows)
        if "RETURN s.name AS subject, e.relation AS relation" in query:  # all-rows load
            return _Result([
                _Record(subject=s, relation=rel, object=o,
                        source_ref=src, confidence=row["confidence"],
                        evidence=row["evidence"], scope_conditions=row["scope_conditions"])
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
            ])
        if "RETURN r.confidence AS confidence" in query:  # arrow confidence read
            arrow = g.arrows[(params["subject"], self._rel(query), params["object"])]
            return _Result([_Record(confidence=arrow.get("confidence"))])
        if "MERGE (s)-[r:" in query:  # arrow confidence write (drift repair only)
            arrow = g.arrows[(params["subject"], self._rel(query), params["object"])]
            arrow["confidence"] = params["aggregate_confidence"]
            arrow["updated_at"] = g.tick()
            return _Result([])
        if "REMOVE r.evidence, r.source_ref" in query:  # legacy provenance strip
            per_relation = self._strip_per_relation(query)
            for key, arrow in g.arrows.items():
                if self._strip_due(key, arrow, g, per_relation):
                    arrow.pop("evidence", None)
                    arrow.pop("source_ref", None)
            return _Result([])
        raise AssertionError(f"fake does not handle query: {query}")

    @staticmethod
    def _rel(query: str) -> str:
        start = query.index("-[r:") + 4
        end = query.index("]", start)
        label = query[start:end]
        return label.split("{")[0]

    @staticmethod
    def _strip_per_relation(query: str) -> bool:
        """Issue #70: the strip EXISTS is pinned to the arrow's relation."""
        return "{relation: type(r)}" in query

    @staticmethod
    def _strip_due(key, arrow, g, per_relation) -> bool:
        if arrow.get("evidence") is None and arrow.get("source_ref") is None:
            return False
        if per_relation:  # a ledger row for this arrow's own relation exists
            return any(identity[:3] == key for identity in g.rows)
        return any(  # un-pinned: any row on the (subject, object) pair
            identity[0] == key[0] and identity[2] == key[2]
            for identity in g.rows)


class _FakeDriver:
    """In-memory property graph with a write-tick clock for idempotency checks."""

    def __init__(self):
        self.arrows: dict[tuple[str, str, str], dict] = {}
        self.rows: dict[tuple[str, str, str, str], dict] = {}
        self._clock = 0

    def tick(self) -> str:
        self._clock += 1
        return f"t{self._clock}"

    def session(self, database=None):
        return _FakeSession(self)

    def close(self):
        return None

    # --- legacy seeding helpers -------------------------------------------
    def add_arrow(self, subject, relation, object_, confidence, evidence,
                  scope_conditions="", source_ref=""):
        self.arrows[(subject, relation.upper(), object_)] = {
            "confidence": confidence,
            "evidence": list(evidence),
            "scope_conditions": scope_conditions,
            "source_ref": source_ref,
            "created_at": self.tick(),
            "updated_at": self.tick(),
        }

    def snapshot(self):
        return copy.deepcopy({"arrows": self.arrows, "rows": self.rows})


def _writer(driver) -> Neo4jGraphWriter:
    return Neo4jGraphWriter(driver, database="neo4j")


def _legacy_driver() -> _FakeDriver:
    driver = _FakeDriver()
    driver.add_arrow("alpha", "supports", "beta", 0.95, ["alpha backs beta"],
                     scope_conditions="when audited", source_ref="doc:chunk-1")
    driver.add_arrow("beta", "INCLUDES_STAGE", "gamma", 0.85, ["beta stages gamma"],
                     source_ref="doc:chunk-2")
    driver.add_arrow("gamma", "may_describe", "alpha", 0.8, ["primary reading", "loose reading"],
                     scope_conditions="ambiguous", source_ref="doc:chunk-3")
    return driver


def test_migration_creates_one_row_per_edge_with_ingest_row_shape():
    driver = _legacy_driver()
    report = _writer(driver).migrate_ledger()
    assert report["edges_seen"] == 3
    assert report["rows_created"] == 3
    assert len(driver.rows) == 3
    for (_s, rel, _o, src), row in driver.rows.items():
        assert rel == rel.upper()
        assert isinstance(row["confidence"], float)
        assert isinstance(row["evidence"], str) and row["evidence"]
        assert isinstance(row["scope_conditions"], str)
        assert row["domain"] == ""
        assert row["created_at"] and row["updated_at"]
    row = driver.rows[("alpha", "SUPPORTS", "beta", "doc:chunk-1")]
    assert row["confidence"] == 0.95
    assert row["evidence"] == "alpha backs beta"
    assert row["scope_conditions"] == "when audited"


def test_arrow_confidences_are_unchanged_during_backfill():
    driver = _legacy_driver()
    before = {k: v["confidence"] for k, v in driver.arrows.items()}
    _writer(driver).migrate_ledger()
    after = {k: v["confidence"] for k, v in driver.arrows.items()}
    assert after == before  # single-row aggregate equals prior confidence
    assert driver.arrows[("gamma", "MAY_DESCRIBE", "alpha")]["updated_at"] == "t6"


def test_backfill_joins_multi_item_legacy_evidence_and_round_trips_it():
    """Issue #72: legacy pre-#58 edges may carry multi-item evidence lists;
    the row's single evidence string must preserve every item (joined at
    seed time), not truncate to the first."""
    driver = _FakeDriver()
    driver.add_arrow("alpha", "supports", "beta", 0.9,
                     ["first observation", "second observation"],
                     source_ref="doc:chunk-9")
    writer = _writer(driver)
    report = writer.migrate_ledger()

    assert report["rows_created"] == 1
    row = driver.rows[("alpha", "SUPPORTS", "beta", "doc:chunk-9")]
    assert row["evidence"] == "first observation\nsecond observation"

    # Round-trip through the provenance join shared by every read path.
    source_ref, evidence = assemble_provenance(
        ["doc:chunk-9"], [row["evidence"]], None, None)
    assert source_ref == "doc:chunk-9"
    assert evidence == ("first observation\nsecond observation",)
    assert "first observation" in evidence[0] and "second observation" in evidence[0]
    edge = writer.get_edge("alpha", "supports", "beta")
    assert edge is not None and edge.source_ref == "doc:chunk-9"
    assert edge.evidence == ("first observation\nsecond observation",)


def test_migration_strips_legacy_provenance_but_keeps_scope_and_timestamps():
    driver = _legacy_driver()
    created = {k: dict(v) for k, v in driver.arrows.items()}
    _writer(driver).migrate_ledger()
    for key, arrow in driver.arrows.items():
        assert "evidence" not in arrow and "source_ref" not in arrow
        assert arrow["scope_conditions"] == created[key]["scope_conditions"]
        assert arrow["created_at"] == created[key]["created_at"]
    report = _writer(driver).migrate_ledger()  # second run strips nothing
    assert report["legacy_props_stripped"] == 0


def test_running_migration_twice_yields_identical_graph_state():
    driver = _legacy_driver()
    writer = _writer(driver)
    writer.migrate_ledger()
    first = driver.snapshot()
    report = writer.migrate_ledger()
    assert report["edges_seen"] == 0  # migrated arrows are never re-candidates
    assert report["rows_created"] == 0
    assert report["rows_skipped"] == 0
    assert report["arrows_recomputed"] == 0
    assert driver.snapshot() == first  # timestamps included: no write fired


def test_get_edge_answers_identically_before_and_after_migration():
    driver = _legacy_driver()
    driver.arrows[("gamma", "MAY_DESCRIBE", "alpha")]["evidence"] = ["primary reading"]
    writer = _writer(driver)
    triples = [("alpha", "supports", "beta"), ("beta", "includes_stage", "gamma"),
               ("gamma", "MAY_DESCRIBE", "alpha")]
    before = [writer.get_edge(*triple) for triple in triples]
    assert all(edge is not None for edge in before)
    writer.migrate_ledger()
    for triple, legacy in zip(triples, before):
        assert writer.get_edge(*triple) == legacy


def test_fully_migrated_graph_is_stable_under_reruns_with_drifted_arrow():
    """Migration backfills; it does not silently rewrite a migrated arrow.
    Repair-by-recompute for drifted arrows is deliberate future tooling, not a
    migration side effect (ADR-0002 Consequences)."""
    driver = _legacy_driver()
    writer = _writer(driver)
    writer.migrate_ledger()
    driver.arrows[("beta", "INCLUDES_STAGE", "gamma")]["confidence"] = 0.4
    first = driver.snapshot()
    report = writer.migrate_ledger()
    assert report["edges_seen"] == 0
    assert driver.snapshot() == first


def test_migration_requires_confident_edges():
    driver = _legacy_driver()
    driver.arrows[("alpha", "SUPPORTS", "beta")]["confidence"] = None
    with pytest.raises(ValueError, match="confidence"):
        _writer(driver).migrate_ledger()


def test_guard_and_strip_are_per_relation_not_per_pair():
    """Issue #70: a (subject, object) pair may carry several relation types,
    and a crash between batches can leave one migrated while its sibling is
    not. The guard and strip must match the arrow's own relation, or the
    un-migrated sibling is skipped and silently loses its provenance."""
    driver = _FakeDriver()
    driver.add_arrow("alpha", "supports", "beta", 0.9, ["older reading"],
                     source_ref="doc:old")
    driver.add_arrow("alpha", "contradicts", "beta", 0.7, ["newer reading"],
                     source_ref="doc:new")
    writer = _writer(driver)
    writer.migrate_ledger()
    # Simulate the crash-between-batches state: drop one relation's rows and
    # restore its legacy provenance so the pair is mixed migrated/un-migrated.
    for identity in [k for k in driver.rows if k[1] == "CONTRADICTS"]:
        del driver.rows[identity]
    arrow = driver.arrows[("alpha", "CONTRADICTS", "beta")]
    arrow["evidence"] = ["newer reading"]
    arrow["source_ref"] = "doc:new"

    report = writer.migrate_ledger()

    assert report["edges_seen"] == 1  # only the un-migrated relation is a candidate
    assert report["rows_created"] == 1
    row = driver.rows[("alpha", "CONTRADICTS", "beta", "doc:new")]
    assert row["evidence"] == "newer reading"  # provenance reached the ledger
    assert driver.rows[("alpha", "SUPPORTS", "beta", "doc:old")]["evidence"] == "older reading"
    assert "evidence" not in driver.arrows[("alpha", "SUPPORTS", "beta")]
    assert "evidence" not in arrow  # stripped only after its own row existed
    assert "source_ref" not in arrow


def test_cli_help_documents_behavior_and_idempotency(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["migrate-ledger", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "extractionevent" in out
    assert "idempotent" in out


def test_cli_migrate_ledger_command_reports_and_exits_zero():
    driver = _legacy_driver()
    import principle_graph.cli as cli
    original = cli._driver
    cli._driver = lambda _settings: driver
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = migrate_ledger_command(cli.Settings.from_env(), out=out)
    finally:
        cli._driver = original
    assert code == 0
    text = out.getvalue()
    assert "rows_created=3" in text and "rows_skipped=0" in text


def test_cli_migrate_ledger_reports_failure_as_exit_one(capsys):
    import principle_graph.cli as cli

    class _Broken:
        def session(self, database=None):
            raise RuntimeError("connection refused")

        def close(self):
            return None

    original = cli._driver
    cli._driver = lambda _settings: _Broken()
    try:
        code = migrate_ledger_command(cli.Settings.from_env())
    finally:
        cli._driver = original
    assert code == 1
    assert "connection refused" in capsys.readouterr().err
