"""OpenAI-compatible chat-completions transport for the extraction seam."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class GatewayError(RuntimeError):
    """An actionable error returned by, or encountered while calling, the gateway."""


@dataclass(frozen=True)
class ToolUseBlock:
    type: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class MessagesResponse:
    content: list[ToolUseBlock]


Transport = Callable[[str, Mapping[str, str], bytes, float], bytes]


class OpenAICompatibleMessagesClient:
    """Adapt OpenAI chat completions to the Anthropic-shaped MessagesClient protocol."""

    def __init__(self, base_url: str, *, model: str = "z-ai/glm-5.3-flash", api_key: str = "local-placeholder", timeout: float = 60, transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or "local-placeholder"
        self.timeout = timeout
        self._transport = transport or self._request

    def create(self, **kwargs: Any) -> MessagesResponse:
        payload: dict[str, Any] = {
            "model": kwargs.get("model", self.model),
            "messages": self._messages(kwargs.get("system"), kwargs.get("messages", [])),
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        if "tools" in kwargs:
            payload["tools"] = [self._tool(tool) for tool in kwargs["tools"]]
        if "tool_choice" in kwargs:
            payload["tool_choice"] = self._tool_choice(kwargs["tool_choice"])
        try:
            raw = self._transport(self._endpoint(), {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, json.dumps(payload).encode(), self.timeout)
            response = json.loads(raw)
        except GatewayError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
            raise GatewayError(f"LLM gateway request failed: {getattr(error, 'reason', error)}") from None
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise GatewayError(f"LLM gateway returned malformed JSON: {error}") from None
        try:
            calls = response["choices"][0]["message"].get("tool_calls", [])
            if calls is None:
                calls = []
            if not isinstance(calls, list):
                raise TypeError("tool_calls is not an array")
            return MessagesResponse([self._tool_call(call) for call in calls])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GatewayError(f"LLM gateway returned malformed response: {error}") from None

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    @staticmethod
    def _messages(system: str | None, messages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return ([{"role": "system", "content": system}] if system is not None else []) + [dict(message) for message in messages]

    @staticmethod
    def _tool(tool: Mapping[str, Any]) -> dict[str, Any]:
        return {"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("input_schema", {})}}

    @staticmethod
    def _tool_choice(choice: Any) -> Any:
        if isinstance(choice, Mapping):
            if choice.get("type") == "auto":
                return "auto"
            if choice.get("type") == "any":
                return "required"
            # Anthropic's forced-tool shape maps to the OpenAI function shape;
            # the Anthropic dict passed through verbatim is not valid OpenAI.
            if choice.get("type") == "tool" and isinstance(choice.get("name"), str):
                return {"type": "function", "function": {"name": choice["name"]}}
        return choice

    @staticmethod
    def _tool_call(call: Mapping[str, Any]) -> ToolUseBlock:
        function = call["function"]
        arguments = function["arguments"]
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(parsed, dict):
            raise TypeError("tool-call arguments are not an object")
        return ToolUseBlock(type="tool_use", name=function["name"], input=parsed)

    def _request(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as result:
                return result.read()
        except urllib.error.HTTPError as error:
            raise GatewayError(f"LLM gateway HTTP error {error.code}: {error.reason}") from None


OpenAIMessagesClient = OpenAICompatibleMessagesClient
