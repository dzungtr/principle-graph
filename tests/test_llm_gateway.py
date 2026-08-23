import json
import pytest
from principle_graph.llm_gateway import GatewayError, OpenAICompatibleMessagesClient


def test_translates_request_and_tool_calls():
    seen = {}
    def transport(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return json.dumps({"choices": [{"message": {"content": "ignored", "tool_calls": [{"function": {"name": "propose_triple", "arguments": '{"subject":"rates"}'}}]}}]}).encode()
    client = OpenAICompatibleMessagesClient("http://gateway", model="m", transport=transport)
    response = client.create(model="m", system="rules", max_tokens=12, tool_choice={"type": "auto"}, tools=[{"name": "propose_triple", "description": "d", "input_schema": {"type": "object"}}], messages=[{"role": "user", "content": "text"}])
    assert seen["url"] == "http://gateway/v1/chat/completions"
    assert seen["body"] == {"model": "m", "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "text"}], "max_tokens": 12, "tool_choice": "auto", "tools": [{"type": "function", "function": {"name": "propose_triple", "description": "d", "parameters": {"type": "object"}}}]}
    assert response.content[0].input == {"subject": "rates"}


def test_empty_tool_calls_and_bad_gateway_errors():
    client = OpenAICompatibleMessagesClient("http://gateway", transport=lambda *args: b'{"choices":[{"message":{"content":"ok"}}]}')
    assert client.create().content == []
    broken = OpenAICompatibleMessagesClient("http://gateway", transport=lambda *args: b"not-json")
    with pytest.raises(GatewayError, match="malformed JSON"):
        broken.create()
