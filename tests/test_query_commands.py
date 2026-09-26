"""Tests for the split ``pg query``/``pg entity`` CLI commands.

External behaviour only, over driver fakes — no database, no network: the
old fan-out `pg query <text>` path is gone, and the new read-only commands
(entity/event vector search, entity show in three modes) route queries and
render markdown/JSON per the sharpened spec. All lookups and outputs use
Neo4j elementId().
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout, suppress

import pytest

from principle_graph.cli import main


class _FakeSession:
    """Dispatches canned rows per Cypher keyword, records the queries run."""

    def __init__(self, cypher_rows: dict[str, list[dict]]):
        self._cypher_rows = cypher_rows
        self.run_cyphers: list[str] = []

    def run(self, cypher: str, **params):
        self.run_cyphers.append((cypher, params))
        index = params.get("index")
        if index is not None and index in self._cypher_rows:
            return iter(self._cypher_rows[index])
        for keyword, rows in self._cypher_rows.items():
            if keyword in cypher:
                return iter(rows)
        return iter([])

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class _FakeDriver:
    def __init__(self, cypher_rows: dict[str, list[dict]] | None = None):
        self._session = _FakeSession(cypher_rows or {})

    def session(self, database: str = "neo4j"):
        return self._session

    def close(self):
        return None


class _FakeEmbedder:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


ENTITY_ROWS = [
    {"element_id": "4:abc:1", "label": "Entity", "name": "United States",
     "type": "country", "relation": None, "evidence": None, "source_ref": None,
     "confidence": None, "score": 0.91},
    {"element_id": "4:abc:2", "label": "Entity", "name": "United States",
     "type": "person", "relation": None, "evidence": None, "source_ref": None,
     "confidence": None, "score": 0.75},
]

EVENT_ROWS = [
    {"element_id": "4:abc:10", "label": "ExtractionEvent",
     "relation": "INCREASES", "evidence": "rates rose", "source_ref": "book:1",
     "confidence": 0.8, "score": 0.88,
     "subject_id": "4:abc:1", "subject_name": "Fed", "subject_type": "org",
     "subject_edge": "REPORTED",
     "object_id": "4:abc:2", "object_name": "Rates", "object_type": "topic",
     "object_edge": "ABOUT"},
    {"element_id": "4:abc:11", "label": "StateEvent",
     "relation": "", "evidence": "gdp 2%", "source_ref": "book:2",
     "confidence": 0.7, "score": 0.65,
     "subject_id": "4:abc:1", "subject_name": "US", "subject_type": "country",
     "subject_edge": "HAS_STATE_EVENT",
     "object_id": None, "object_name": None, "object_type": None,
     "object_edge": None},
]

LINK_ROWS = [
    {"relation": "MAY_DESCRIBE", "direction": "outgoing",
     "element_id": "4:abc:9", "name": "Target", "type": "topic"},
]

STATE_ROWS = [
    {"element_id": "4:abc:20", "state_key": "inflation", "value": "2.4",
     "unit": "%", "as_of": "2026-01", "confidence": 0.9, "source_ref": "book:3"},
]

EVENT_LINK_ROWS = [
    {"element_id": "4:abc:10", "label": "ExtractionEvent", "relation": "REPORTED",
     "raw_relation": "increases", "evidence": "rates rose", "source_ref": "book:1",
     "confidence": 0.8},
]


def _run(argv, driver, embedder=_FakeEmbedder(), monkeypatch=None):
    monkeypatch.setattr("principle_graph.cli._driver", lambda _s: driver)
    monkeypatch.setattr("principle_graph.cli._build_embedder", lambda _s: embedder)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def test_old_query_fanout_path_is_removed(monkeypatch):
    # argparse rejects the stray positional on the `query` parent.
    with pytest.raises(SystemExit) as excinfo:
        _run(["query", "interest rates are rising"], _FakeDriver(),
             monkeypatch=monkeypatch)
    assert excinfo.value.code == 2


def test_bare_query_parent_reports_missing_subcommand(monkeypatch):
    code, _, _ = _run(["query"], _FakeDriver(), monkeypatch=monkeypatch)
    assert code == 2


def test_query_entity_markdown_lists_element_id_name_type_score(monkeypatch):
    driver = _FakeDriver({"vector.queryNodes": ENTITY_ROWS})
    code, out, _ = _run(["query", "entity", "United States"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    assert "4:abc:1" in out and "United States" in out
    assert "country" in out and "person" in out
    # Both same-name duplicates survive; elementId binds the next command.
    assert out.count("- 4:abc:") == 2


def test_query_entity_applies_threshold_and_top_k(monkeypatch):
    driver = _FakeDriver({"vector.queryNodes": ENTITY_ROWS})
    code, out, _ = _run(["query", "entity", "United States", "--top-k", "1"],
                        driver, monkeypatch=monkeypatch)
    assert code == 0
    assert "4:abc:1" in out and "4:abc:2" not in out


def test_query_entity_json_shape(monkeypatch):
    driver = _FakeDriver({"vector.queryNodes": ENTITY_ROWS})
    code, out, _ = _run(["query", "entity", "United States", "--format", "json"],
                        driver, monkeypatch=monkeypatch)
    assert code == 0
    payload = json.loads(out)
    assert payload["query"] == "United States"
    assert [r["element_id"] for r in payload["results"]] == ["4:abc:1", "4:abc:2"]
    assert payload["results"][0]["type"] == "country"


def test_query_entity_low_score_filtered_by_threshold(monkeypatch):
    rows = [dict(ENTITY_ROWS[0], score=0.30)]
    driver = _FakeDriver({"vector.queryNodes": rows})
    code, out, _ = _run(["query", "entity", "United States"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    assert "No matches above the similarity threshold." in out


def test_query_event_includes_label_and_entity_pair(monkeypatch):
    driver = _FakeDriver({"extraction_evidence_embedding": [EVENT_ROWS[0]]})
    code, out, _ = _run(["query", "event", "rates rising"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    assert "ExtractionEvent" in out
    assert "StateEvent" not in out
    assert "via REPORTED" in out and "via ABOUT" in out
    assert "4:abc:10" in out


def test_query_event_json_carries_subject_and_object(monkeypatch):
    driver = _FakeDriver({"extraction_evidence_embedding": [EVENT_ROWS[0]]})
    code, out, _ = _run(["query", "event", "rates rising", "--format", "json"],
                        driver, monkeypatch=monkeypatch)
    assert code == 0
    results = json.loads(out)["results"]
    assert results[0]["subject"]["element_id"] == "4:abc:1"
    assert results[0]["object"]["element_id"] == "4:abc:2"


def test_query_event_only_uses_extraction_evidence_index(monkeypatch):
    driver = _FakeDriver({"extraction_evidence_embedding": [EVENT_ROWS[0]],
                 "state_evidence_embedding": [EVENT_ROWS[1]]})
    _run(["query", "event", "rates", "--format", "json"], driver,
         monkeypatch=monkeypatch)
    indexes = {params.get("index") for _, params in driver._session.run_cyphers}
    assert indexes == {"extraction_evidence_embedding"}


def test_query_event_sorted_by_score(monkeypatch):
    driver = _FakeDriver({"extraction_evidence_embedding":
                 [EVENT_ROWS[0], dict(EVENT_ROWS[0], score=0.60)]})
    code, out, _ = _run(["query", "event", "rates", "--format", "json"], driver,
                        monkeypatch=monkeypatch)
    scores = [r["score"] for r in json.loads(out)["results"]]
    assert scores == sorted(scores, reverse=True)


def test_query_entity_requires_embedder(monkeypatch):
    code, _, err = _run(["query", "entity", "x"], _FakeDriver(), embedder=None,
                        monkeypatch=monkeypatch)
    assert code == 1
    assert "Embedder unavailable" in err


def test_query_rejects_non_positive_top_k(monkeypatch):
    code, _, err = _run(["query", "entity", "x", "--top-k", "0"], _FakeDriver(),
                        monkeypatch=monkeypatch)
    assert code == 2
    assert "positive" in err


def test_entity_show_linked_entities_with_direction(monkeypatch):
    driver = _FakeDriver({"startNode(r) = e": LINK_ROWS,
                          "RETURN elementId(e) AS element_id":
                              [{"element_id": "4:abc:1"}]})
    code, out, _ = _run(["entity", "show", "4:abc:1"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    assert "outgoing MAY_DESCRIBE" in out
    assert "4:abc:9" in out and "Target" in out


def test_entity_show_state_mode_uses_has_state_event(monkeypatch):
    driver = _FakeDriver({"HAS_STATE_EVENT": STATE_ROWS,
                          "RETURN elementId(e) AS element_id":
                              [{"element_id": "4:abc:1"}]})
    code, out, _ = _run(["entity", "show", "4:abc:1", "--state"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    assert "inflation=2.4 %" in out and "as_of=2026-01" in out


def test_entity_show_event_mode_lists_ledger_rows(monkeypatch):
    driver = _FakeDriver({"ExtractionEvent OR ev:StateEvent": EVENT_LINK_ROWS,
                          "RETURN elementId(e) AS element_id":
                              [{"element_id": "4:abc:1"}]})
    code, out, _ = _run(["entity", "show", "4:abc:1", "--event"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    assert "REPORTED" in out and "raw: increases" in out
    assert "4:abc:10" in out


def test_entity_show_state_and_event_are_mutually_exclusive(monkeypatch):
    with suppress(SystemExit):
        code, _, _ = _run(["entity", "show", "4:abc:1", "--state", "--event"],
                          _FakeDriver(), monkeypatch=monkeypatch)
        assert code == 2


def test_entity_show_unknown_element_id_fails_cleanly(monkeypatch):
    code, _, err = _run(["entity", "show", "4:missing:0"], _FakeDriver(),
                        monkeypatch=monkeypatch)
    assert code == 1
    assert "Entity not found: 4:missing:0" in err


def test_entity_show_json_mode(monkeypatch):
    driver = _FakeDriver({"startNode(r) = e": LINK_ROWS,
                          "RETURN elementId(e) AS element_id":
                              [{"element_id": "4:abc:1"}]})
    code, out, _ = _run(["entity", "show", "4:abc:1", "--format", "json"], driver,
                        monkeypatch=monkeypatch)
    assert code == 0
    payload = json.loads(out)
    assert payload["element_id"] == "4:abc:1"
    assert payload["mode"] == "entities"
    assert payload["results"][0]["relation"] == "MAY_DESCRIBE"