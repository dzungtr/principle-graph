from dataclasses import dataclass

from principle_graph.extraction import PROPOSE_STATE_TOOL, PROPOSE_TRIPLE_TOOL, SequentialExtractor
from principle_graph.extraction_contract import chunk_markdown


@dataclass
class ToolUse:
    type: str
    name: str
    input: dict


@dataclass
class Response:
    content: list


class FakeMessages:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def triple(source_ref, subject="rates"):
    return {
        "subject": subject, "subject_type": "policy_action", "relation": "reduces",
        "object": "borrowing", "object_type": "economic_behavior", "confidence": .8,
        "evidence": "The text links rates and borrowing.", "scope_conditions": "short run",
        "source_ref": source_ref,
    }


def test_extracts_chunks_sequentially_and_stages_valid_candidates():
    chunks = chunk_markdown("# One\nrates reduce borrowing.\n# Two\ninflation follows.", "book")
    client = FakeMessages([
        Response([ToolUse("tool_use", "propose_triple", triple(chunks[0].source_ref))]),
        Response([]),
    ])
    run = SequentialExtractor(client).run(chunks)
    assert [c["subject"] for c in run.candidates] == ["rates"]
    assert run.completed_chunks == ["chunk-1", "chunk-2"]
    assert [call["messages"][0]["content"] for call in client.calls] == [
        "Extract from this chunk.\n\nsource_ref: book:chunk-1\nchunk_id: chunk-1\nsection_path: One\n\nchunk text:\n# One\nrates reduce borrowing.",
        "Extract from this chunk.\n\nsource_ref: book:chunk-2\nchunk_id: chunk-2\nsection_path: Two\n\nchunk text:\n# Two\ninflation follows.",
    ]
    assert client.calls[0]["tools"] == [PROPOSE_TRIPLE_TOOL, PROPOSE_STATE_TOOL]


def test_rejects_malformed_and_wrong_source_tool_calls():
    chunks = chunk_markdown("# One\nrates reduce borrowing.", "book")
    invalid = triple("wrong:chunk-1")
    invalid["confidence"] = 2
    client = FakeMessages([Response([ToolUse("tool_use", "propose_triple", invalid)])])
    run = SequentialExtractor(client).run(chunks)
    assert not run.candidates
    assert len(run.rejected) == 1
    assert "confidence" in run.rejected[0]["reason"]


def test_ignores_non_tool_response_content():
    chunks = chunk_markdown("# One\ntext", "book")
    client = FakeMessages([Response([ToolUse("text", "", {})])])
    run = SequentialExtractor(client).run(chunks)
    assert run.candidates == []
    assert run.rejected == []


# --- Extraction contract v2 shaping rules (issue #100) ---

def test_system_prompt_encodes_atomicity_rule():
    from principle_graph.extraction import SYSTEM_PROMPT
    assert "exactly one thing" in SYSTEM_PROMPT
    assert "scope_conditions" in SYSTEM_PROMPT


def test_system_prompt_encodes_canonical_naming_rule():
    from principle_graph.extraction import SYSTEM_PROMPT
    assert "canonical" in SYSTEM_PROMPT
    assert "full name" in SYSTEM_PROMPT


def test_system_prompt_encodes_decomposition_rule():
    from principle_graph.extraction import SYSTEM_PROMPT
    assert "separate propose_triple" in SYSTEM_PROMPT
    assert "same evidence" in SYSTEM_PROMPT


def test_system_prompt_encodes_event_node_rule():
    from principle_graph.extraction import SYSTEM_PROMPT
    assert "edge" in SYSTEM_PROMPT
    assert "event" in SYSTEM_PROMPT


def test_system_prompt_encodes_description_in_name_stripping():
    from principle_graph.extraction import SYSTEM_PROMPT
    assert "parenthetical" in SYSTEM_PROMPT


def test_system_prompt_has_no_per_chunk_proposal_cap():
    from principle_graph.extraction import SYSTEM_PROMPT
    for banned in ("at most", "maximum of", "no more than", "limit the number"):
        assert banned not in SYSTEM_PROMPT.lower()


def test_token_ceiling_unchanged_while_decomposing_multi_actor_claim():
    chunks = chunk_markdown("# One\nFrance and Canada signed the agreement.", "book")
    evidence = "France and Canada signed the agreement."
    client = FakeMessages([Response([
        ToolUse("tool_use", "propose_triple", triple(
            chunks[0].source_ref, subject="France") | {
                "subject_type": "country", "object": "the agreement",
                "object_type": "agreement", "evidence": evidence}),
        ToolUse("tool_use", "propose_triple", triple(
            chunks[0].source_ref, subject="Canada") | {
                "subject_type": "country", "object": "the agreement",
                "object_type": "agreement", "evidence": evidence}),
    ])])
    run = SequentialExtractor(client).run(chunks)
    assert [c["subject"] for c in run.candidates] == ["France", "Canada"]
    assert {c["evidence"] for c in run.candidates} == {evidence}
    assert client.calls[0]["max_tokens"] == 16384


# --- Domain tag in the tool contract (issue #78) ---

def test_propose_triple_schema_has_optional_domain():
    properties = PROPOSE_TRIPLE_TOOL["input_schema"]["properties"]
    assert properties["domain"] == {"type": "string"}
    assert "domain" not in PROPOSE_TRIPLE_TOOL["input_schema"]["required"]


def test_system_prompt_tags_domain_only_when_chunk_grounded():
    from principle_graph.extraction import SYSTEM_PROMPT
    assert "domain" in SYSTEM_PROMPT
    assert "never guess" in SYSTEM_PROMPT


def test_candidate_with_domain_is_staged():
    chunks = chunk_markdown("# One\nrates reduce borrowing.", "book")
    client = FakeMessages([
        Response([ToolUse("tool_use", "propose_triple",
                          triple(chunks[0].source_ref) | {"domain": "economics"})]),
    ])
    run = SequentialExtractor(client).run(chunks)
    assert run.candidates[0]["domain"] == "economics"
