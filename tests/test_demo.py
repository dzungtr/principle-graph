from pathlib import Path

from principle_graph.demo import run_demo


def test_demo_runs_source_to_review_commit_and_query():
    source = Path(__file__).parents[1] / "docs/demo/demo-source.md"
    result = run_demo(source)
    assert result.extraction_requests == 3
    assert result.embedding_requests == 0
    assert result.graph.edges
    assert all(edge.source_ref.startswith("demo-source.md:chunk-") for edge in result.graph.edges.values())
    assert "Mode-2 review: approved" in result.transcript
    assert "commit result:" in result.transcript
    assert "interest rates are rising" in result.transcript
    assert "supply disruptions" in result.transcript
    assert result.elapsed_seconds < 600


def test_demo_rejects_delta_without_committing():
    source = Path(__file__).parents[1] / "docs/demo/demo-source.md"
    result = run_demo(source, input_fn=lambda _: "reject")
    assert not result.graph.edges
    assert "commit result: 0 edges committed" in result.transcript
