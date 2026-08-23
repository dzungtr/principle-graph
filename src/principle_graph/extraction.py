"""Sequential, tool-call based extraction from source chunks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .extraction_contract import Chunk, ContractError, validate_triple

PROPOSE_TRIPLE_TOOL: dict[str, Any] = {
    "name": "propose_triple",
    "description": "Propose one relationship explicitly asserted or reasonably implied by the supplied chunk.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "subject_type": {"type": "string"},
            "relation": {"type": "string"},
            "object": {"type": "string"},
            "object_type": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string"},
            "scope_conditions": {"type": "string"},
            "source_ref": {"type": "string"},
        },
        "required": [
            "subject", "subject_type", "relation", "object", "object_type",
            "confidence", "evidence", "scope_conditions", "source_ref",
        ],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You extract relationship candidates from exactly one supplied source chunk.
Extract only relationships explicitly asserted or reasonably implied by that chunk.
Do not use general or outside knowledge. If a field cannot be grounded in the chunk,
omit the candidate. Preserve qualifiers in scope_conditions. Return zero or more
propose_triple tool calls and no prose claims outside tool calls. Use the chunk's
source_ref verbatim. Confidence is for this extraction event only; ambiguity lowers it."""


class MessagesClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


@dataclass
class ExtractionRun:
    """Disposable scratch state produced by one sequential extraction run."""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    completed_chunks: list[str] = field(default_factory=list)


class SequentialExtractor:
    """Process chunks in order, retaining only structurally valid tool calls."""

    def __init__(self, client: MessagesClient, *, model: str = "claude-sonnet-4-20250514", system: str = SYSTEM_PROMPT) -> None:
        self.client = client
        self.model = model
        self.system = system

    def run(self, chunks: Sequence[Chunk]) -> ExtractionRun:
        scratch = ExtractionRun()
        for chunk in chunks:
            response = self.client.create(
                model=self.model,
                system=self.system,
                max_tokens=4096,
                tools=[PROPOSE_TRIPLE_TOOL],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": self._chunk_prompt(chunk)}],
            )
            for block in self._tool_blocks(response):
                if getattr(block, "name", None) != "propose_triple":
                    continue
                try:
                    candidate = self._input(block)
                    validate_triple(candidate)
                    if candidate["source_ref"] != chunk.source_ref:
                        raise ContractError("source_ref must match the supplied chunk")
                except (ContractError, TypeError, ValueError) as error:
                    scratch.rejected.append({"chunk_id": chunk.id, "candidate": locals().get("candidate", {}), "reason": str(error)})
                else:
                    scratch.candidates.append(candidate)
            scratch.completed_chunks.append(chunk.id)
        return scratch

    @staticmethod
    def _chunk_prompt(chunk: Chunk) -> str:
        metadata = f"source_ref: {chunk.source_ref}\nchunk_id: {chunk.id}"
        if chunk.section_path:
            metadata += f"\nsection_path: {' / '.join(chunk.section_path)}"
        if chunk.pages:
            metadata += f"\npages: {', '.join(map(str, chunk.pages))}"
        return f"Extract from this chunk.\n\n{metadata}\n\nchunk text:\n{chunk.text}"

    @staticmethod
    def _tool_blocks(response: Any) -> list[Any]:
        return [block for block in getattr(response, "content", ()) if getattr(block, "type", None) == "tool_use"]

    @staticmethod
    def _input(block: Any) -> dict[str, Any]:
        value = getattr(block, "input", None)
        if not isinstance(value, dict):
            raise ContractError("tool input must be an object")
        return value


def extract_chunks(chunks: Sequence[Chunk], client: MessagesClient, **kwargs: Any) -> ExtractionRun:
    """Convenience wrapper for a single sequential extraction session."""
    return SequentialExtractor(client, **kwargs).run(chunks)
