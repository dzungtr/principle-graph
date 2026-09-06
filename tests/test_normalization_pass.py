"""Relation normalization pass (issue #77, ADR-0003).

External behavior only: the pure plan over ledger rows, the writer pass
executed through a property-graph fake, the write-boundary canonicalization on
ingest, and the CLI command. Idempotency is asserted on full state equality
(rows, arrows, timestamps). Database-free.
"""
from __future__ import annotations

import copy
import io
from contextlib import redirect_stdout

import pytest

from principle_graph.cli import main, normalize_relations_command
from principle_graph.label_registry import load_label_registry
from principle_graph.ledger import LedgerRow
from principle_graph.neo4j import Neo4jGraphWriter
from principle_graph.normalization import plan_normalization
from principle_graph.reduction import GraphEdge


@pytest.fixture(scope="module")
def registry():
    return load_label_registry("src/principle_graph/data/relation-registry.yaml")


def _row(subject, relation, object_, source_ref, confidence=0.9, **kwargs):
    return LedgerRow(subject, relation, object_, source_ref, confidence,
                     "evidence for the claim", **kwargs)


# --- the pure plan -----------------------------------------------------------


def test_alias_rows_are_rewritten_to_the_canonical_identity(registry):
    plan = plan_normalization([_row("a", "REPLACES", "b", "doc:1")], registry)
    assert len(plan.rows_to_create) == 1
    created = plan.rows_to_create[0]
    assert created.identity == ("a", "SUPERSEDES", "b", "doc:1")
    assert created.raw_relation == "REPLACES"  # pre-registry verb preserved
    assert plan.rows_to_delete == (("a", "REPLACES", "b", "doc:1"),)
    assert plan.arrows_to_delete == (("a", "REPLACES", "b"),)


def test_inverse_spelling_moves_the_row_to_the_flipped_triple(registry):
    plan = plan_normalization([_row("a", "SUPERSEDED_BY", "b", "doc:1")], registry)
    (created,) = plan.rows_to_create
    assert created.identity == ("b", "SUPERSEDES", "a", "doc:1")
    assert created.raw_relation == "SUPERSEDED_BY"
    assert plan.arrows_to_delete == (("a", "SUPERSEDED_BY", "b"),)


def test_emptied_arrows_are_deleted_and_targets_recompute_their_aggregate(registry):
    plan = plan_normalization([
        _row("a", "SUPERSEDED_BY", "b", "doc:1", 0.6),
        _row("b", "SUPERSEDES", "a", "doc:2", 0.5),
    ], registry)
    (update,) = plan.arrow_updates
    assert (update.subject, update.relation, update.object) == ("b", "SUPERSEDES", "a")
    # complement aggregate of {0.5, 0.6}: 1 - 0.5*0.4 = 0.8
    assert update.aggregate_confidence == pytest.approx(0.8)


def test_same_source_verb_variants_collapse_to_one_row(registry):
    # Canonical spelling first: the variant is deleted, the kept row survives.
    plan = plan_normalization([
        _row("a", "SUPERSEDES", "b", "doc:1"),
        _row("a", "REPLACES", "b", "doc:1"),
    ], registry)
    assert len(plan.rows_to_create) == 0
    assert len(plan.rows_to_delete) == 1
    # Variant only: the canonical row is created, the variant deleted.
    plan = plan_normalization([_row("a", "REPLACES", "b", "doc:1")], registry)
    assert len(plan.rows_to_create) == 1
    assert len(plan.rows_to_delete) == 1


def test_an_untouched_canonical_row_wins_over_a_rewritten_variant(registry):
    plan = plan_normalization([
        _row("a", "SUPERSEDES", "b", "doc:1"),
        _row("a", "REPLACES", "b", "doc:1"),
    ], registry)
    # The kept row already holds the canonical identity; the variant is
    # deleted without replacement — never two rows for one identity.
    assert plan.rows_to_create == ()
    assert plan.rows_to_delete == (("a", "REPLACES", "b", "doc:1"),)


def test_unknown_verbs_pass_through_flagged_and_untouched(registry):
    plan = plan_normalization([
        _row("a", "MAY_DESCRIBE", "b", "doc:1"),
        _row("c", "MAY_DESCRIBE", "d", "doc:2"),
    ], registry)
    assert plan.is_empty
    assert plan.unknown_flagged == ("MAY_DESCRIBE",)


def test_second_plan_over_the_normalized_rows_is_empty(registry):
    rows = [
        _row("a", "REPLACES", "b", "doc:1"),
        _row("a", "SUPERSEDED_BY", "b", "doc:2"),
    ]
    plan = plan_normalization(rows, registry)
    final_rows = [row for row in rows if row.identity not in plan.rows_to_delete]
    final_rows += plan.rows_to_create
    assert plan_normalization(final_rows, registry).is_empty


def test_the_canonical_verbs_themselves_are_untouched(registry):
    plan = plan_normalization([_row("a", "SUPPORTS", "b", "doc:1")], registry)
    assert plan.is_empty


# --- the writer pass over a property-graph fake ------------------------------


class _Record(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Executes the exact queries the normalization pass issues, in memory."""

    def __init__(self, graph):
        self.graph = graph

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, query, **params):
        g = self.graph
        if "e.raw_relation AS raw_relation" in query:  # normalize rows load
            return _Result([
                _Record(subject=s, relation=rel, object=o, source_ref=src,
                        confidence=row["confidence"], evidence=row["evidence"],
                        scope_conditions=row["scope_conditions"],
                        domain=row["domain"], raw_relation=row["raw_relation"])
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
            ])
        if "ExtractionEvent {relation: $relation}" in query:  # triple row load
            return _Result([
                _Record(source_ref=src, confidence=row["confidence"],
                        evidence=row["evidence"], scope_conditions=row["scope_conditions"])
                for (s, rel, o, src), row in sorted(g.rows.items(), key=lambda kv: kv[1]["created_at"])
                if (s, rel, o) == (params["subject"], params["relation"], params["object"])
            ])
        if "DETACH DELETE e" in query:  # rewritten row removal
            identity = (params["subject"], params["relation"],
                        params["object"], params["source_ref"])
            del g.rows[identity]
            return _Result([])
        if "MERGE (s)-[:REPORTED]->" in query:  # canonical row merge
            identity = (params["subject"], params["relation"],
                        params["object"], params["source_ref"])
            if identity not in g.rows:
                g.rows[identity] = {
                    "confidence": params["confidence"],
                    "evidence": params["evidence"],
                    "scope_conditions": params["scope_conditions"],
                    "domain": params["domain"],
                    "raw_relation": params["raw_relation"],
                    "created_at": g.tick(),
                    "updated_at": g.tick(),
                }
            return _Result([])
        if "DELETE r" in query:  # emptied arrow removal
            key = (params["subject"], self._rel(query), params["object"])
            g.arrows.pop(key, None)
            return _Result([])
        if "RETURN r.confidence AS confidence" in query:  # arrow read
            arrow = g.arrows.get((params["subject"], self._rel(query), params["object"]))
            return _Result([_Record(confidence=arrow["confidence"])] if arrow else [])
        if "MERGE (s)-[r:" in query:  # arrow create/update
            key = (params["subject"], self._rel(query), params["object"])
            arrow = g.arrows.setdefault(key, {"scope_conditions": ""})
            arrow["confidence"] = params["aggregate_confidence"]
            if params["scope_conditions"]:
                arrow["scope_conditions"] = params["scope_conditions"]
            arrow["created_at"] = arrow.get("created_at") or g.tick()
            arrow["updated_at"] = g.tick()
            return _Result([])
        raise AssertionError(f"fake does not handle query: {query}")

    @staticmethod
    def _rel(query: str) -> str:
        start = query.index("-[r:") + 4
        end = query.index("]", start)
        return query[start:end].split("{")[0]


class _FakeDriver:
    """In-memory property graph with a write-tick clock for idempotency checks."""

    def __init__(self):
        self.rows: dict[tuple, dict] = {}
        self.arrows: dict[tuple, dict] = {}
        self._clock = 0

    def tick(self) -> str:
        self._clock += 1
        return f"t{self._clock}"

    def session(self, database=None):
        return _FakeSession(self)

    def close(self):
        return None

    def add_row(self, subject, relation, object_, source_ref, confidence,
                raw_relation="", scope_conditions=""):
        self.rows[(subject, relation.upper(), object_, source_ref)] = {
            "confidence": confidence, "evidence": "seed evidence",
            "scope_conditions": scope_conditions, "domain": "",
            "raw_relation": raw_relation,
            "created_at": self.tick(), "updated_at": self.tick(),
        }
        self.arrows[(subject, relation.upper(), object_)] = {
            "confidence": confidence, "scope_conditions": scope_conditions,
            "created_at": self.tick(), "updated_at": self.tick(),
        }

    def snapshot(self):
        return copy.deepcopy({"rows": self.rows, "arrows": self.arrows})


def _writer(driver) -> Neo4jGraphWriter:
    return Neo4jGraphWriter(driver, database="neo4j")


def _mixed_driver() -> _FakeDriver:
    driver = _FakeDriver()
    driver.add_row("a", "REPLACES", "b", "doc:chunk-1", 0.9)
    driver.add_row("a", "SUPERSEDES", "b", "doc:chunk-2", 0.8)
    driver.add_row("a", "SUPERSEDED_BY", "c", "doc:chunk-3", 0.7)
    driver.add_row("d", "MAY_DESCRIBE", "e", "doc:chunk-4", 0.5)
    return driver


def test_pass_canonicalizes_rows_flips_arrows_and_flags_unknown():
    driver = _mixed_driver()
    report = _writer(driver).normalize_relations()
    assert report["rows_seen"] == 4
    assert report["rows_created"] == 2  # chunk-1 canonical + flipped chunk-3
    assert report["rows_deleted"] == 2
    assert report["arrows_deleted"] == 2
    assert report["unknown_flagged"] == 1
    # The alias spelling landed on its canonical identity, verb preserved;
    # the other source's canonical row was kept untouched.
    alias_row = driver.rows[("a", "SUPERSEDES", "b", "doc:chunk-1")]
    assert alias_row["raw_relation"] == "REPLACES"
    assert driver.rows[("a", "SUPERSEDES", "b", "doc:chunk-2")]["raw_relation"] == ""
    # The triple now aggregates both rows: 1 - 0.1 * 0.2.
    assert driver.arrows[("a", "SUPERSEDES", "b")]["confidence"] == pytest.approx(0.98)
    # The inverse spelling moved to the flipped triple.
    assert ("a", "SUPERSEDED_BY", "c", "doc:chunk-3") not in driver.rows
    flipped = driver.rows[("c", "SUPERSEDES", "a", "doc:chunk-3")]
    assert flipped["raw_relation"] == "SUPERSEDED_BY"
    assert ("a", "SUPERSEDED_BY", "c") not in driver.arrows
    assert driver.arrows[("c", "SUPERSEDES", "a")]["confidence"] == pytest.approx(0.7)
    # Unknown verb: row and arrow untouched.
    assert ("d", "MAY_DESCRIBE", "e", "doc:chunk-4") in driver.rows


def test_second_pass_is_a_full_state_noop():
    driver = _mixed_driver()
    writer = _writer(driver)
    writer.normalize_relations()
    first = driver.snapshot()
    report = writer.normalize_relations()
    assert report["rows_created"] == 0
    assert report["rows_deleted"] == 0
    assert report["arrows_deleted"] == 0
    assert report["arrows_recomputed"] == 0
    assert driver.snapshot() == first  # timestamps included: no write fired


def test_distinct_type_count_drops_after_normalization():
    driver = _mixed_driver()
    writer = _writer(driver)
    before = {rel for (_s, rel, _o, _src) in driver.rows}
    writer.normalize_relations()
    after = {rel for (_s, rel, _o, _src) in driver.rows}
    assert len(after) < len(before)
    assert "REPLACES" not in after and "SUPERSEDED_BY" not in after


def test_normalization_of_an_already_canonical_graph_changes_nothing():
    driver = _FakeDriver()
    driver.add_row("a", "SUPERSEDES", "b", "doc:chunk-1", 0.9)
    first = driver.snapshot()
    report = _writer(driver).normalize_relations()
    assert report["rows_created"] == 0 and report["rows_deleted"] == 0
    assert driver.snapshot() == first


# --- write-boundary canonicalization on ingest -------------------------------


def test_ingest_lands_alias_verbs_on_the_canonical_identity():
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(
        GraphEdge("a", "replaces", "b", 0.9, "doc:chunk-1", ("ev",))
    )
    writer.upsert_extraction(
        GraphEdge("a", "supersedes", "b", 0.8, "doc:chunk-1", ("ev",))
    )
    # Same source, alias + canonical spellings: one row, canonical identity.
    assert list(driver.rows) == [("a", "SUPERSEDES", "b", "doc:chunk-1")]
    assert driver.rows[("a", "SUPERSEDES", "b", "doc:chunk-1")]["raw_relation"] == "replaces"


def test_ingest_of_an_inverse_spelling_flips_the_triple():
    driver = _FakeDriver()
    _writer(driver).upsert_extraction(
        GraphEdge("a", "superseded_by", "b", 0.9, "doc:chunk-1", ("ev",))
    )
    assert list(driver.rows) == [("b", "SUPERSEDES", "a", "doc:chunk-1")]
    assert ("b", "SUPERSEDES", "a") in driver.arrows
    assert driver.arrows[("b", "SUPERSEDES", "a")]["confidence"] == pytest.approx(0.9)


def test_ingest_of_an_unknown_verb_passes_through_and_is_counted(caplog):
    driver = _FakeDriver()
    writer = _writer(driver)
    with caplog.at_level("WARNING"):
        writer.upsert_extraction(
            GraphEdge("a", "may_describe", "b", 0.9, "doc:chunk-1", ("ev",))
        )
    assert ("a", "MAY_DESCRIBE", "b", "doc:chunk-1") in driver.rows
    assert writer.unknown_relation_counts == {"may_describe": 1}
    assert any("may_describe" in message and "registry" in message
               for message in caplog.messages)


def test_reingest_stays_structurally_idempotent_under_normalization():
    driver = _FakeDriver()
    writer = _writer(driver)
    writer.upsert_extraction(
        GraphEdge("a", "supersedes", "b", 0.9, "doc:chunk-1", ("ev",))
    )
    writer.upsert_extraction(
        GraphEdge("a", "replaces", "b", 0.7, "doc:chunk-1", ("ev",))
    )
    # The alias spelling resolves to the same identity: keep-first skip.
    assert len(driver.rows) == 1
    assert driver.rows[("a", "SUPERSEDES", "b", "doc:chunk-1")]["confidence"] == 0.9


# --- the CLI command ---------------------------------------------------------


def test_cli_help_documents_the_pass_and_idempotency(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["normalize-relations", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "registry" in out
    assert "idempotent" in out


def test_cli_normalize_relations_reports_and_exits_zero():
    driver = _mixed_driver()
    import principle_graph.cli as cli
    original = cli._driver
    cli._driver = lambda _settings: driver
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = normalize_relations_command(cli.Settings.from_env(), out=out)
    finally:
        cli._driver = original
    assert code == 0
    text = out.getvalue()
    assert "rows_created=2" in text and "unknown_flagged=1" in text


def test_cli_normalize_relations_reports_failure_as_exit_one(capsys):
    import principle_graph.cli as cli

    class _Broken:
        def session(self, database=None):
            raise RuntimeError("connection refused")

        def close(self):
            return None

    original = cli._driver
    cli._driver = lambda _settings: _Broken()
    try:
        code = normalize_relations_command(cli.Settings.from_env())
    finally:
        cli._driver = original
    assert code == 1
    assert "connection refused" in capsys.readouterr().err
