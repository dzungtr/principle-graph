"""Sequential, tool-call based extraction from source chunks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .extraction_contract import Chunk, ContractError, validate_state, validate_triple

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
            "domain": {"type": "string"},
            "source_ref": {"type": "string"},
        },
        "required": [
            "subject", "subject_type", "relation", "object", "object_type",
            "confidence", "evidence", "scope_conditions", "source_ref",
        ],
        "additionalProperties": False,
    },
}

PROPOSE_STATE_TOOL: dict[str, Any] = {
    "name": "propose_state",
    "description": "Propose one numeric or qualitative measurement of an entity that is explicitly asserted or reasonably implied by the supplied chunk.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "entity_type": {"type": "string"},
            "state_key": {"type": "string"},
            "value": {"type": ["string", "number"]},
            "unit": {"type": "string"},
            "as_of": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string"},
            "scope_conditions": {"type": "string"},
            "source_ref": {"type": "string"},
        },
        "required": [
            "entity", "entity_type", "state_key", "value", "unit", "as_of",
            "confidence", "evidence", "scope_conditions", "source_ref",
        ],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You extract relationship candidates and entity measurements from exactly one supplied source chunk.
Extract only relationships explicitly asserted or reasonably implied by that chunk.
Do not use general or outside knowledge. If a field cannot be grounded in the chunk,
omit the candidate. Preserve qualifiers in scope_conditions. Return zero or more
propose_triple or propose_state tool calls and no prose claims outside tool calls.
Shape every entity as follows:
- Atomicity: subject and object each name exactly one thing (a person, organization,
country, place, event, policy, agreement, product, technology, market, or concept).
Never use a clause, sentence, list, or possessive description as an entity; move such
content into the relation, scope_conditions, or evidence instead.
- Canonical naming: use the canonical form — name people by their full name ("Friedrich Merz", never "Merz"
or "Germany's chancellor") and organizations by one standard short name ("Bank of
Japan", never "BOJ" and "Bank of Japan" in the same run).
- Decomposition: a proposition about several actors becomes one separate propose_triple per
actor, all sharing the same evidence sentence from the chunk. Never collapse a multi-actor
claim into a compound entity such as "France and Canada".
- Event-nodes are second-class: if a subject-verb-object sentence carries the whole
claim, emit it as the edge itself. Create an event entity only for events discussed
as things ("EU-Canada summit", "November 10 truce expiry").
- Names stay bare: move parenthetical qualifiers ("the ECB (European Central Bank)",
"rates (2024)") into scope_conditions or evidence and keep the name itself clean.
Use propose_state for numeric or qualitative measurements of an entity (approval
ratings, rates, volumes, levels such as "elevated"): quantities and measurements
route to propose_state, the entity carries the state, and the measurement never
becomes its own node or endpoint. Use the chunk's source_ref verbatim.
There is no cap on how many candidates you may propose for a chunk; propose every
grounded candidate. Confidence is for this extraction event only; ambiguity lowers it.
Include domain only when the chunk itself grounds the claim in a topic area —
never guess; omit the field to leave the row untagged."""


class MessagesClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


@dataclass
class ExtractionRun:
    """Disposable scratch state produced by one sequential extraction run."""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    # Validated ``propose_state`` candidates (issue #98): same provenance
    # discipline as triples; stored separately so the triple pipeline is untouched.
    state_candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    completed_chunks: list[str] = field(default_factory=list)


class SequentialExtractor:
    """Process chunks in order, retaining only structurally valid tool calls."""

    def __init__(self, client: MessagesClient, *, model: str = "claude-sonnet-4-20250514", system: str = SYSTEM_PROMPT) -> None:
        self.client = client
        self.model = model
        self.system = system

    def run(self, chunks: Sequence[Chunk], *, verb_menu: Sequence[str] = (),
            entity_roster: Sequence[str] = ()) -> ExtractionRun:
        # Issue #102: the two-pass scan's verb menu + entity roster ride every
        # chunk prompt; empty menus render the pre-scan prompt unchanged.
        scratch = ExtractionRun()
        for chunk in chunks:
            response = self.client.create(
                model=self.model,
                system=self.system,
                max_tokens=16384,
                tools=[PROPOSE_TRIPLE_TOOL, PROPOSE_STATE_TOOL],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": self._chunk_prompt(
                    chunk, verb_menu=verb_menu, entity_roster=entity_roster)}],
            )
            for block in self._tool_blocks(response):
                name = getattr(block, "name", None)
                if name == "propose_state":
                    try:
                        candidate = self._input(block)
                        validate_state(candidate)
                        if candidate["source_ref"] != chunk.source_ref:
                            raise ContractError("source_ref must match the supplied chunk")
                    except (ContractError, TypeError, ValueError) as error:
                        scratch.rejected.append({"chunk_id": chunk.id, "candidate": locals().get("candidate", {}), "reason": str(error), "tool": "propose_state"})
                    else:
                        scratch.state_candidates.append(candidate)
                    continue
                if name != "propose_triple":
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
    def _chunk_prompt(chunk: Chunk, *, verb_menu: Sequence[str] = (),
                      entity_roster: Sequence[str] = ()) -> str:
        metadata = f"source_ref: {chunk.source_ref}\nchunk_id: {chunk.id}"
        if chunk.section_path:
            metadata += f"\nsection_path: {' / '.join(chunk.section_path)}"
        if chunk.pages:
            metadata += f"\npages: {', '.join(map(str, chunk.pages))}"
        # Issue #102 injection point (PRD #95 Handoffs): after the metadata
        # block, before `chunk text:`.
        sections = ""
        if verb_menu:
            sections += f"\nverb menu: {', '.join(verb_menu)}"
        if entity_roster:
            sections += f"\nentity roster: {'; '.join(entity_roster)}"
        return f"Extract from this chunk.\n\n{metadata}{sections}\n\nchunk text:\n{chunk.text}"

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
    menu = kwargs.pop("verb_menu", ())
    roster = kwargs.pop("entity_roster", ())
    return SequentialExtractor(client, **kwargs).run(chunks, verb_menu=menu, entity_roster=roster)
