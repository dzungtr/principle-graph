"""Ledger write-path behavior through a plan-driven stateful fake (issue #58 AC 4-5, 7).

The fake executes the real policy core (plan_ledger_writes + complement_aggregate)
in memory, so corroboration, keep-first idempotency, and reject isolation are
asserted against observable graph state without a database. The Cypher adapter
shape is pinned separately in tests/test_neo4j_backends.py.
"""
from principle_graph.ledger import LedgerRow, complement_aggregate, plan_ledger_writes
from principle_graph.reduction import assemble_delta, commit_delta
from principle_graph.review import GraphDelta, GraphEdge, GraphEntity, review_and_commit


class FakeLedgerGraph:
    """GraphWriter + ledger writer applying LedgerWritePlan semantics in memory."""

    def __init__(self, repeat_mode: str = "keep-first"):
        self.entities = []
        self.rows: list[LedgerRow] = []
        self.arrows: dict[tuple[str, str, str], dict] = {}
        self.rejected: list[dict] = []
        self.legacy_upserts = 0
        self.repeat_mode = repeat_mode

    # --- GraphWriter seam (legacy methods must never fire on the ledger path) ---
    def upsert_entity(self, entity):
        if entity not in self.entities:
            self.entities.append(entity)

    def upsert_edge(self, edge):
        self.legacy_upserts += 1

    def get_edge(self, subject, relation, object_):
        """Same external shape as Neo4jGraphWriter.get_edge, sourced from rows."""
        relation = relation.upper()
        arrow = self.arrows.get((subject, relation, object_))
        if arrow is None:
            return None
        rows = self.rows_for(subject, relation, object_)
        return GraphEdge(subject, relation, object_, arrow["confidence"],
                         rows[-1].source_ref if rows else "",
                         tuple(r.evidence for r in rows),
                         arrow["scope_conditions"])

    def record_rejected(self, record):
        self.rejected.append(record)

    # --- Ledger writer seam: executes the plan like Neo4jGraphWriter does ---
    def upsert_extraction(self, edge: GraphEdge) -> None:
        relation = edge.relation.upper()
        candidate = LedgerRow(edge.subject, relation, edge.object, edge.source_ref,
                              edge.confidence, edge.evidence[0] if edge.evidence else "",
                              edge.scope_conditions)
        existing = [r for r in self.rows if r.identity[:3] == candidate.identity[:3]]
        plan = plan_ledger_writes(existing, [candidate], mode=self.repeat_mode)
        self.rows.extend(plan.rows_to_create)
        for updated in plan.rows_to_update:
            # Refresh replaces the matched row's values in place (issue #59).
            self.rows = [updated if row.identity == updated.identity else row
                         for row in self.rows]
        for update in plan.arrow_updates:
            key = (update.subject, update.relation, update.object)
            arrow = self.arrows.setdefault(key, {"confidence": 0.0, "scope_conditions": ""})
            arrow["confidence"] = update.aggregate_confidence
            if update.scope_conditions:
                arrow["scope_conditions"] = update.scope_conditions

    def edge(self, subject, relation, object_):
        return self.arrows[(subject, relation.upper(), object_)]

    def rows_for(self, subject, relation, object_):
        return [r for r in self.rows
                if r.identity[:3] == (subject, relation.upper(), object_)]


def _commit(graph, confidence, source_ref, evidence, scope=""):
    delta = assemble_delta(
        [GraphEdge("a", "supports", "b", confidence, source_ref, (evidence,), scope)],
        existing=[GraphEdge("a", "SUPPORTS", "b", r.confidence, r.source_ref, (r.evidence,),
                            r.scope_conditions) for r in graph.rows_for("a", "supports", "b")],
        entities=[GraphEntity("a", "thing"), GraphEntity("b", "thing")],
    )
    commit_delta(delta, graph)


def test_two_sources_become_visible_corroboration():
    graph = FakeLedgerGraph()
    _commit(graph, 0.6, "doc:chunk-1", "first evidence")
    _commit(graph, 0.5, "doc:chunk-2", "second evidence", scope="scope x")
    rows = graph.rows_for("a", "supports", "b")
    assert sorted((r.source_ref, r.evidence) for r in rows) == [
        ("doc:chunk-1", "first evidence"), ("doc:chunk-2", "second evidence")]
    assert graph.edge("a", "supports", "b")["confidence"] == 0.8
    assert graph.edge("a", "supports", "b")["scope_conditions"] == "scope x"
    assert graph.legacy_upserts == 0


def test_same_document_reingest_is_a_deterministic_noop():
    graph = FakeLedgerGraph()
    _commit(graph, 0.6, "doc:chunk-1", "first evidence")
    _commit(graph, 0.5, "doc:chunk-2", "second evidence")
    before = (len(graph.rows), graph.edge("a", "supports", "b")["confidence"])
    _commit(graph, 0.6, "doc:chunk-1", "first evidence")
    after = (len(graph.rows), graph.edge("a", "supports", "b")["confidence"])
    assert before == after == (2, 0.8)


def test_rejected_verdict_never_touches_the_ledger():
    graph = FakeLedgerGraph()
    delta = assemble_delta(
        [GraphEdge("a", "supports", "b", 0.9, "doc:chunk-1", ("evidence",), "")],
        entities=[GraphEntity("a", "thing"), GraphEntity("b", "thing")],
    )
    result = review_and_commit(delta, graph, input_fn=lambda _: "r")
    assert result.rejected
    assert graph.rows == []
    assert graph.arrows == {}
    assert graph.rejected[0]["source_ref"] == "doc:chunk-1"


def test_mode2_edit_confidence_lands_in_ledger_rows_and_arrow():
    """Regression (PR #67 review round 1, P1): the Mode-2 [e]dit path must reach
    the ledger — the edited confidence is exactly what lands as the row and the
    recomputed arrow, never the pre-edit candidate value."""
    graph = FakeLedgerGraph()
    proposed = assemble_delta(
        [GraphEdge("a", "supports", "b", 0.6, "doc:chunk-1", ("evidence",), "scope")],
        entities=[GraphEntity("a", "thing"), GraphEntity("b", "thing")],
    )
    answers = iter(["e", "0.95", "a"])
    review_and_commit(proposed, graph, input_fn=lambda _: next(answers))
    (row,) = graph.rows
    assert row.source_ref == "doc:chunk-1"
    assert row.confidence == 0.95
    assert graph.arrows[("a", "SUPPORTS", "b")]["confidence"] == 0.95


def test_mode2_edit_confidence_recomputes_arrow_over_existing_rows():
    """A confidence edit on a pending delta recomputes the arrow from all ledger
    rows: the pre-existing row stays untouched, the edited collapse lands once."""
    graph = FakeLedgerGraph()
    first = assemble_delta(
        [GraphEdge("a", "supports", "b", 0.5, "doc:chunk-0", ("prior",))],
        entities=[GraphEntity("a", "thing"), GraphEntity("b", "thing")],
    )
    review_and_commit(first, graph, input_fn=lambda _: "a")
    second = GraphDelta(
        new_edges=[GraphEdge("a", "SUPPORTS", "b", 0.8, "doc:chunk-1", ("e1", "e2"))],
        raw_candidates=[
            GraphEdge("a", "supports", "b", 0.6, "doc:chunk-1", ("e1",), "scope-1"),
            GraphEdge("a", "supports", "b", 0.5, "doc:chunk-2", ("e2",), "scope-2"),
        ],
    )
    answers = iter(["e", "0.95", "a"])
    review_and_commit(second, graph, input_fn=lambda _: next(answers))
    assert [(row.source_ref, row.confidence) for row in graph.rows] == [
        ("doc:chunk-0", 0.5),
        ("doc:chunk-1", 0.95),
    ]
    # 1 - (1 - 0.5)(1 - 0.95)
    assert abs(graph.arrows[("a", "SUPPORTS", "b")]["confidence"] - 0.975) < 1e-9


def test_refresh_reingest_updates_the_row_and_recomputes_the_aggregate():
    """Issue #59 AC: same source re-ingested under refresh with a new confidence
    keeps exactly one row with updated values and an aggregate recomputed
    accordingly — the deliberate-refinement counterpart of keep-first idempotency."""
    graph = FakeLedgerGraph(repeat_mode="refresh")
    _commit(graph, 0.5, "doc:chunk-1", "first evidence", scope="old scope")
    _commit(graph, 0.9, "doc:chunk-1", "refined evidence", scope="new scope")
    rows = graph.rows_for("a", "supports", "b")
    assert len(rows) == 1
    (only,) = rows
    assert only.confidence == 0.9
    assert only.evidence == "refined evidence"
    assert only.scope_conditions == "new scope"
    assert graph.edge("a", "supports", "b")["confidence"] == 0.9
    assert graph.edge("a", "supports", "b")["scope_conditions"] == "new scope"


def test_refresh_and_keep_first_graphs_diverge_on_the_same_reingest():
    """Same writes, different modes: keep-first keeps the original row, refresh
    replaces it — observable graph state, not plan internals."""
    keep = FakeLedgerGraph()
    refresh = FakeLedgerGraph(repeat_mode="refresh")
    for graph in (keep, refresh):
        _commit(graph, 0.5, "doc:chunk-1", "first evidence")
        _commit(graph, 0.9, "doc:chunk-1", "second evidence")
    assert keep.rows_for("a", "supports", "b")[0].evidence == "first evidence"
    assert keep.edge("a", "supports", "b")["confidence"] == 0.5
    assert refresh.rows_for("a", "supports", "b")[0].evidence == "second evidence"
    assert refresh.edge("a", "supports", "b")["confidence"] == 0.9
