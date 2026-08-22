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
    assert "commit result: 5 edges committed" in result.transcript
    fanout = result.transcript.split("Fan-out directions for: interest rates are rising", 1)[1]
    assert "demand path: interest rates -> borrowing -> spending -> reduced demand" in fanout
    assert "qualification: supply disruptions -> prices" in fanout
    assert fanout.index("demand path") < fanout.index("qualification")
    assert "scope: when demand is weak" in fanout
    assert result.elapsed_seconds < 600


def test_demo_rejects_delta_without_committing():
    source = Path(__file__).parents[1] / "docs/demo/demo-source.md"
    result = run_demo(source, input_fn=lambda _: "reject")
    assert not result.graph.edges
    assert len(result.graph.rejected) == 5
    assert "Mode-2 review: rejected" in result.transcript
    assert "commit result: 0 edges committed" in result.transcript
    assert "rejected items: 5" in result.transcript
    assert "Fan-out directions: none" in result.transcript
