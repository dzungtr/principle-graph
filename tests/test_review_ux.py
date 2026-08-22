from principle_graph.reduction import InMemoryGraph
from principle_graph.review import (
    GraphDelta, GraphEdge, GraphEntity, review_and_commit, review_delta, render_delta,
)


def delta():
    return GraphDelta(
        new_entities=[GraphEntity("Alice", "Person")],
        new_edges=[GraphEdge("Alice", "USES", "Python", 0.8, "chapter 1")],
        merges=[("Alicia", "Alice")],
        confidence_changes=[("Alice", "USES", "Python", 0.8, 0.9)],
    )


def test_render_delta_includes_all_change_types():
    output = render_delta(delta())
    assert "New entities" in output
    assert "Alice [Person]" in output
    assert "Alice -[USES, confidence 0.80]-> Python" in output
    assert "Alicia -> Alice" in output
    assert "0.80 -> 0.90" in output


def test_review_approve_returns_approved_and_empty_rejected():
    result = review_delta(delta(), input_fn=lambda _: "a")
    assert result.approved == delta()
    assert result.rejected == []


def test_review_reject_records_audit_item():
    result = review_delta(delta(), input_fn=lambda _: "r")
    assert result.approved == GraphDelta()
    assert len(result.rejected) == 1
    assert result.rejected[0]["decision"] == "rejected"
    assert result.rejected[0]["reason"] == "rejected by reviewer"
    assert result.rejected[0]["delta"] == delta()


def test_review_edit_changes_confidence_before_approval():
    answers = iter(["e", "0.95", "a"])
    result = review_delta(delta(), input_fn=lambda _: next(answers))
    assert result.approved.new_edges[0].confidence == 0.95


def test_review_and_commit_hands_approved_verdict_to_writer():
    graph = InMemoryGraph()
    proposed = GraphDelta(
        new_entities=[GraphEntity("Alice", "Person")],
        new_edges=[GraphEdge("Alice", "USES", "Python", 0.8, "chapter 1")],
    )
    result = review_and_commit(proposed, graph, input_fn=lambda _: "a")
    assert result.rejected == []
    assert graph.edges[("Alice", "USES", "Python")].confidence == 0.8
    assert graph.entities == [GraphEntity("Alice", "Person")]


def test_rejected_verdict_is_audited_without_commit():
    graph = InMemoryGraph()
    proposed = GraphDelta(new_edges=[GraphEdge("Alice", "USES", "Python", 0.8, "chapter 1")])
    result = review_and_commit(proposed, graph, input_fn=lambda _: "r")
    assert result.approved == GraphDelta()
    assert graph.edges == {}
    assert graph.entities == []
    assert graph.rejected[0]["source_ref"] == "chapter 1"
