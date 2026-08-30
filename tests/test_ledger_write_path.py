"""Ledger write-path behavior through a plan-driven stateful fake (issue #58 AC 4-5, 7).

The fake executes the real policy core (plan_ledger_writes + complement_aggregate)
in memory, so corroboration, keep-first idempotency, and reject isolation are
asserted against observable graph state without a database. The Cypher adapter
shape is pinned separately in tests/test_neo4j_backends.py.
"""
from principle_graph.ledger import LedgerRow, complement_aggregate, plan_ledger_writes
from principle_graph.reduction import assemble_delta, commit_delta
from principle_graph.review import GraphEdge, GraphEntity, review_and_commit


class FakeLedgerGraph:
    """GraphWriter + ledger writer applying LedgerWritePlan semantics in memory."""

    def __init__(self):
        self.entities = []
        self.rows: list[LedgerRow] = []
        self.arrows: dict[tuple[str, str, str], dict] = {}
        self.rejected: list[dict] = []
        self.legacy_upserts = 0

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
        plan = plan_ledger_writes(existing, [candidate])
        self.rows.extend(plan.rows_to_create)
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
