from principle_graph.reduction import InMemoryGraph, aggregate_confidence, assemble_delta, commit_delta, reduce_edges
from principle_graph.review import GraphEdge, GraphEntity


def test_repeated_edges_aggregate_and_keep_unique_evidence():
    edges = [GraphEdge("a", "supports", "b", .6, "s1", ("first",)), GraphEdge("a", "supports", "b", .5, "s2", ("second",))]
    result = reduce_edges(edges)
    assert len(result) == 1
    assert result[0].relation == "SUPPORTS"
    assert result[0].confidence == .8
    assert result[0].evidence == ("first", "second")


def test_same_source_does_not_raise_confidence():
    result = reduce_edges([GraphEdge("a", "r", "b", .6, "same"), GraphEdge("a", "r", "b", .5, "same")])
    assert result[0].confidence == .6


def test_assemble_delta_separates_new_and_confidence_update():
    delta = assemble_delta([GraphEdge("a", "r", "b", .5, "s2", ("new",))], [GraphEdge("a", "R", "b", .5, "s1", ("old",))], [GraphEntity("a", "thing")])
    assert delta.new_edges == []
    assert delta.confidence_changes == [("a", "R", "b", .5, .75)]
    assert delta.updated_edges == [GraphEdge("a", "R", "b", .75, "s2", ("old", "new"), "")]
    assert delta.new_entities


def test_commit_only_writes_approved_delta():
    graph = InMemoryGraph()
    commit_delta(assemble_delta([GraphEdge("a", "r", "b", .7, "s")], entities=[GraphEntity("a", "thing")]), graph)
    assert graph.edges[("a", "R", "b")].confidence == .7
    assert graph.rejected == []


def test_two_independent_sources_commit_full_merged_provenance():
    graph = InMemoryGraph()
    first = GraphEdge("a", "r", "b", .5, "book:page-1", ("first evidence",), "first scope")
    commit_delta(assemble_delta([first]), graph)

    second = GraphEdge("a", "r", "b", .5, "book:page-2", ("second evidence",), "second scope")
    commit_delta(assemble_delta([second], existing=list(graph.edges.values())), graph)

    edge = graph.edges[("a", "R", "b")]
    assert edge.confidence == .75
    assert edge.source_ref == "book:page-2"
    assert edge.evidence == ("first evidence", "second evidence")
    assert edge.scope_conditions == "second scope"


def test_same_source_commit_keeps_existing_provenance():
    graph = InMemoryGraph()
    first = GraphEdge("a", "r", "b", .5, "book:page-1", ("evidence",), "scope")
    commit_delta(assemble_delta([first]), graph)
    repeat = GraphEdge("a", "r", "b", .5, "book:page-1", ("evidence",), "")
    commit_delta(assemble_delta([repeat], existing=list(graph.edges.values())), graph)
    assert graph.edges[("a", "R", "b")].confidence == .5
    assert graph.edges[("a", "R", "b")].evidence == ("evidence",)


def test_aggregation_is_clamped():
    assert aggregate_confidence(2, 2) == 1
