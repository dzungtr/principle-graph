"""Mis-shape dispatch: deterministic guards classify, Jev decides, pipeline executes.

Placement: after extract, before the novelty gate (PRD #95 — shaping before
admission). Deterministic guards classify mis-shaped triple proposals into a
fixed set of classes — classification only, never disposal. One Jev decision
call per guard-flagged candidate sends the bare claim + evidence + the original
chunk (grounding preserved: repairs generate from source text, not model
memory) plus the per-category handling menu; Jev returns a typed step + payload
and the executor materializes it:

- repaired triples re-enter the candidate stream (shared evidence, ADR-0008),
- repaired states flow through the #98 state path,
- ``pass_flagged`` / ``to_source`` surface in Mode-2 review visibility,
- ``drop_noise`` and invalid payloads land in the rejected log with a verdict,
- well-formed triples bypass dispatch entirely (zero extra calls),
- a dispatcher outage raises :class:`DispatchError` — hard abort of ingest
  (ADR-0006 stance).

Network-free and database-free: the client runs over an injectable transport
(prior art: ``novelty.JevDecisionsClient``).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .extraction_contract import Chunk, ContractError, validate_state, validate_triple


class DispatchError(RuntimeError):
    """The dispatcher could not produce an executable step; the ingest must abort."""


# --- guards: deterministic classification only ------------------------------

GUARD_CLASSES: tuple[str, ...] = (
    "numeric_endpoint", "qualitative_endpoint", "deictic", "self_loop",
    "description_in_name", "clause", "list", "compound_actor",
)


@dataclass(frozen=True)
class GuardThresholds:
    """Tunable classification parameters (PRD #95: thresholds are spec parameters)."""

    # An endpoint with at least this many words is a clause-node, not a name.
    clause_min_words: int = 7
    # An endpoint enumerating at least this many items is a list-node.
    list_min_items: int = 3
    # Adjective-like values tracked as qualitative measurements, not entities.
    qualitative_terms: frozenset[str] = frozenset(
        {"elevated", "high", "low", "stable", "weak", "strong",
         "rising", "falling", "improved", "deteriorated", "ample", "tight"})


_NUMERIC_RE = re.compile(r"^\s*[$€£]?\s*[-+]?\d[\d.,]*\s*(%|percent|bn|billion|million|trillion|m|k)?\b", re.I)
_DEICTIC_RE = re.compile(
    r"^(the |this |that |these |those )?(previous|next|following|above|current|prior|foregoing|aforementioned)\b"
    r"|^(it|they|them|he|she|this|that|speaker|the speaker|the video|the author)$", re.I)
_PAREN_QUALIFIER_RE = re.compile(r"\([^)]+\)")
# Possessive descriptions ("Germany's chancellor", "France's president") —
# a description, not a bare canonical name.
_POSSESSIVE_DESC_RE = re.compile(r"\b\w+'s\b")


def _split_items(endpoint: str) -> list[str]:
    """Split an endpoint on commas and 'and' to count enumerated items."""
    parts = re.split(r",|\band\b", endpoint)
    return [part for part in (p.strip() for p in parts) if part]


def _endpoint_classes(endpoint: str, thresholds: GuardThresholds) -> list[str]:
    classes: list[str] = []
    if _NUMERIC_RE.match(endpoint):
        classes.append("numeric_endpoint")
    if endpoint.strip().casefold() in thresholds.qualitative_terms:
        classes.append("qualitative_endpoint")
    if _DEICTIC_RE.search(endpoint):
        classes.append("deictic")
    if _PAREN_QUALIFIER_RE.search(endpoint) or _POSSESSIVE_DESC_RE.search(endpoint):
        classes.append("description_in_name")
    words = len(endpoint.split())
    items = _split_items(endpoint)
    if words >= thresholds.clause_min_words:
        classes.append("clause")
    elif len(items) >= thresholds.list_min_items:
        classes.append("list")
    elif " and " in endpoint.casefold():
        classes.append("compound_actor")
    return classes


def classify_candidate(
    candidate: Mapping[str, Any],
    thresholds: GuardThresholds | None = None,
) -> tuple[str, ...]:
    """Deterministically classify a triple candidate; empty tuple = well-formed.

    Classification only — guards never repair and never dispose (PRD #95).
    The returned order is the check order above; the first class is the
    primary class passed to :func:`dispatch`.
    """
    thresholds = thresholds or GuardThresholds()
    classes: list[str] = []
    for endpoint in (candidate.get("subject", ""), candidate.get("object", "")):
        if not isinstance(endpoint, str):
            continue
        for guard_class in _endpoint_classes(endpoint, thresholds):
            if guard_class not in classes:
                classes.append(guard_class)
    subject = str(candidate.get("subject", "")).strip().casefold()
    object_ = str(candidate.get("object", "")).strip().casefold()
    if subject and subject == object_:
        classes.append("self_loop")
    return tuple(classes)


def flagged_endpoint(candidate: Mapping[str, Any], guard_class: str,
                     thresholds: GuardThresholds | None = None) -> str:
    """Which endpoint triggered ``guard_class``: ``"subject"`` or ``"object"``.

    Subject wins ties (both endpoints flagged, or the class is endpoint-order
    independent). ``self_loop`` defaults to the object slot, which the
    ``missing_endpoint`` executor replaces.
    """
    if guard_class == "self_loop":
        return "object"
    thresholds = thresholds or GuardThresholds()
    for slot in ("subject", "object"):
        endpoint = candidate.get(slot, "")
        if isinstance(endpoint, str) and guard_class in _endpoint_classes(endpoint, thresholds):
            return slot
    return "subject"


# --- menus and payload contracts (issue #101 authoritative table) -----------

CATEGORY_MENUS: dict[str, tuple[str, ...]] = {
    "numeric_endpoint": ("to_state", "pass_flagged", "drop_noise"),
    "qualitative_endpoint": ("to_state", "to_scope", "drop_noise"),
    "clause": ("decompose", "to_state", "drop_noise"),
    "list": ("decompose_per_member", "joint_with_scope", "drop_noise"),
    "compound_actor": ("pairwise_joint_edge", "named_group", "decompose", "drop_noise"),
    "deictic": ("to_source", "pass_flagged", "drop_noise"),
    "self_loop": ("missing_endpoint", "reflexive_state", "drop_noise"),
    "description_in_name": ("normalize_name", "pass_flagged"),
}

# Payload keys the executor needs; a well-formed answer missing one of these is
# an invalid payload (rejected log + flag), never a bad write.
REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    "to_state": ("entity", "state_key", "value", "as_of"),
    "reflexive_state": ("entity", "state_key", "value", "as_of"),
    "to_scope": ("scope",),
    "decompose": ("triples",),
    "decompose_per_member": ("triples",),
    "pairwise_joint_edge": ("edges",),
    "joint_with_scope": ("edges", "scope_conditions"),
    "named_group": ("group_name",),
    "normalize_name": ("name",),
    "missing_endpoint": ("endpoint",),
    "to_source": ("note",),
}

STEP_INSTRUCTIONS: dict[str, str] = {
    "to_state": "Route the measurement onto an entity as state (entity, state_key, value, unit, as_of).",
    "to_scope": "Keep the relation but move the qualitative value into scope text (scope).",
    "decompose": "Split the proposition into separate atomic triples (triples[]).",
    "decompose_per_member": "One triple per enumerated member (triples[]).",
    "pairwise_joint_edge": "Joint claim as pairwise edges (edges[]).",
    "joint_with_scope": "Joint claim as edges with magnitude in scope (edges[], scope_conditions).",
    "named_group": "Keep one edge, name the compound actor as a group (group_name, group_type).",
    "to_source": "Deictic/meta content routes to the source layer (note).",
    "normalize_name": "Bare canonical name plus qualifier (name, qualifier).",
    "missing_endpoint": "Supply the missing self-loop endpoint (endpoint, endpoint_type).",
    "reflexive_state": "The subject holds a state about itself (entity, state_key, value, unit, as_of).",
    "pass_flagged": "Pass the candidate through flagged for Mode-2 review.",
    "drop_noise": "Not knowledge; drop to the rejected log.",
}


# --- Jev decision client (recorded-transport seam) --------------------------

Transport = Callable[[str, Mapping[str, str], bytes, float], bytes]


@dataclass(frozen=True)
class DispatchStep:
    """Jev's typed answer: one menu step plus its (possibly empty) payload."""

    choice: str
    payload: Mapping[str, Any]


class JevDispatchClient:
    """Call the OpenRouter Decisions API with an injectable transport.

    State carries the bare claim, the evidence, and the original chunk text —
    grounding preserved (ADR-0008). Any transport or malformed-answer failure
    raises :class:`DispatchError` (hard abort); a *well-formed* answer whose
    payload the executor cannot run is an invalid payload, handled by the
    caller as rejected log + flag.
    """

    def __init__(self, base_url: str = "https://openrouter.ai/api/alpha/decisions",
                 *, model: str = "typesafe/jev-1.13", api_key: str = "",
                 timeout: float = 10.0, transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport or self._request

    def decide(self, candidate: Mapping[str, Any], guard_class: str,
               chunk: Chunk) -> DispatchStep:
        menu = CATEGORY_MENUS.get(guard_class)
        if not menu:
            raise DispatchError(f"no handling menu for guard class {guard_class!r}")
        if not self.api_key:
            raise DispatchError(
                "mis-shape dispatch is enabled but OPENROUTER_API_KEY is unset")
        relation_as_words = str(candidate.get("relation", "")).strip().casefold().replace("_", " ")
        payload = {
            "model": self.model,
            "state": {
                "claim": f"{candidate['subject']} {relation_as_words} {candidate['object']}",
                "evidence": str(candidate.get("evidence", "")),
                "chunk": chunk.text,
            },
            "questions": {"repair": {
                "type": "choice",
                "instructions": "Pick the one repair step that best shapes this mis-shaped proposal.",
                "criteria": {step: STEP_INSTRUCTIONS[step] for step in menu},
            }},
        }
        try:
            raw = self._transport(
                self.base_url,
                {"Content-Type": "application/json",
                 "Authorization": f"Bearer {self.api_key}"},
                json.dumps(payload).encode(),
                self.timeout,
            )
            response = json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise DispatchError(
                f"dispatch request failed: {getattr(error, 'reason', error)}") from None
        try:
            answer = response["answers"]["repair"]
            if answer.get("type") != "choice":
                raise TypeError("repair answer is not a choice")
            choice = answer["choice"]
            if choice not in menu:
                raise ValueError(f"choice {choice!r} not in menu {menu}")
            answer_payload = answer.get("payload") or {}
            if not isinstance(answer_payload, dict):
                raise TypeError("payload must be an object")
        except (KeyError, TypeError, ValueError) as error:
            raise DispatchError(
                f"Jev dispatch returned a malformed answer: {error}") from None
        return DispatchStep(choice, answer_payload)

    @staticmethod
    def _request(url: str, headers: Mapping[str, str], body: bytes,
                 timeout: float) -> bytes:
        request = urllib.request.Request(url, data=body, headers=dict(headers),
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as result:
                return result.read()
        except urllib.error.HTTPError as error:
            raise DispatchError(
                f"Jev dispatch HTTP error {error.code}: {error.reason}") from None


class Dispatcher(Protocol):
    def decide(self, candidate: Mapping[str, Any], guard_class: str,
               chunk: Chunk) -> DispatchStep: ...


# --- outcomes ---------------------------------------------------------------

@dataclass(frozen=True)
class RepairedCandidate:
    """The executed repair: what re-enters the pipeline and what is flagged.

    ``triples`` re-enter the candidate stream before the novelty gate;
    ``states`` flow through the state path (issue #98); ``flagged`` records
    surface in Mode-2 review visibility (never written as triples).
    """

    step: str
    triples: tuple[dict[str, Any], ...] = ()
    states: tuple[dict[str, Any], ...] = ()
    flagged: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Dropped:
    """A candidate removed from the stream with an auditable verdict."""

    verdict: str  # "drop_noise" or "invalid_payload"
    reason: str
    flagged: bool = True


def _flagged_record(candidate: Mapping[str, Any], guard_class: str, step: str,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate": dict(candidate),
        "guard_class": guard_class,
        "step": step,
        "payload": dict(payload),
        "source_ref": candidate.get("source_ref", ""),
        "reason": f"{guard_class} repaired via {step}; flagged for Mode-2 review",
    }


def _invalid_record(candidate: Mapping[str, Any], guard_class: str, step: str,
                    reason: str) -> dict[str, Any]:
    return {
        "candidate": dict(candidate),
        "guard_class": guard_class,
        "step": step,
        "source_ref": candidate.get("source_ref", ""),
        "decision": "rejected",
        "verdict": "invalid_payload",
        "reason": reason,
    }


def _ensure_scope(repaired: dict[str, Any]) -> None:
    """The triple contract requires non-empty scope text; unconditional says so."""
    if not str(repaired.get("scope_conditions", "")).strip():
        repaired["scope_conditions"] = "unconditional"


def _state_candidate(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a ``propose_state``-shaped candidate from a ``to_state`` payload.

    Provenance (evidence, scope, source_ref, confidence) comes from the original
    candidate; the payload contributes the measurement itself.
    """
    return {
        "entity": payload["entity"],
        "entity_type": payload.get("entity_type", candidate["subject_type"]),
        "state_key": payload["state_key"],
        "value": payload["value"],
        "unit": payload.get("unit", ""),
        "as_of": payload["as_of"],
        "confidence": candidate["confidence"],
        "evidence": candidate["evidence"],
        "scope_conditions": candidate.get("scope_conditions", ""),
        "source_ref": candidate["source_ref"],
    }


def _member_triples(candidate: Mapping[str, Any], members: Any,
                    scope_conditions: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate payload triples[], forcing shared provenance from the original."""
    if not isinstance(members, list) or not members:
        raise ContractError("payload triples/edges must be a non-empty list")
    repaired: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            errors.append(f"member {index} is not an object")
            continue
        merged = {
            "subject": member.get("subject", ""),
            "subject_type": member.get("subject_type", ""),
            "relation": member.get("relation", ""),
            "object": member.get("object", ""),
            "object_type": member.get("object_type", ""),
            "confidence": candidate["confidence"],
            "evidence": candidate["evidence"],  # shared evidence, ADR-0008
            # Inherit the original scope; members may override. The contract
            # requires non-empty scope text, so unconditional repairs say so.
            "scope_conditions": (
                scope_conditions if scope_conditions is not None
                else str(member.get("scope_conditions")
                         or candidate.get("scope_conditions")
                         or "unconditional")),
            "source_ref": candidate["source_ref"],
        }
        if "domain" in candidate:
            merged["domain"] = candidate["domain"]
        try:
            validate_triple(merged)
        except (ContractError, TypeError, ValueError) as error:
            errors.append(f"member {index}: {error}")
            continue
        repaired.append(merged)
    return repaired, errors


# --- dispatch: one Jev call per guard-flagged candidate ----------------------

def dispatch(candidate: Mapping[str, Any], guard_class: str, chunk: Chunk,
             client: Dispatcher) -> RepairedCandidate | Dropped:
    """Classified candidate in, executable repair (or auditable drop) out."""
    try:
        step = client.decide(candidate, guard_class, chunk)
    except DispatchError:
        raise  # unreachable dispatcher: hard abort of ingest
    choice = step.choice
    payload = step.payload
    missing = [key for key in REQUIRED_PAYLOAD.get(choice, ())
               if not str(payload.get(key, "")).strip()
               and not isinstance(payload.get(key), (list, dict))]
    if missing:
        return Dropped("invalid_payload",
                       f"step {choice!r} payload missing fields: {', '.join(missing)}")
    thresholds = GuardThresholds()

    if choice in ("to_state", "reflexive_state"):
        try:
            state = _state_candidate(candidate, payload)
            validate_state(state)
        except (ContractError, KeyError, TypeError, ValueError) as error:
            return Dropped("invalid_payload", f"state payload invalid: {error}")
        return RepairedCandidate(choice, states=[state])

    if choice == "to_scope":
        scope = str(payload["scope"])
        repaired = dict(candidate)
        repaired["scope_conditions"] = (
            f"{candidate.get('scope_conditions', '')} {scope}".strip()).strip()
        _ensure_scope(repaired)
        try:
            validate_triple(repaired)
        except (ContractError, TypeError, ValueError) as error:
            return Dropped("invalid_payload", f"scope repair invalid: {error}")
        return RepairedCandidate(choice, triples=[repaired])

    if choice in ("decompose", "decompose_per_member"):
        repaired, errors = _member_triples(candidate, payload.get("triples"))
        if errors and not repaired:
            return Dropped("invalid_payload", "; ".join(errors))
        return RepairedCandidate(choice, triples=repaired)

    if choice in ("pairwise_joint_edge", "joint_with_scope"):
        scope = (str(payload["scope_conditions"]) if choice == "joint_with_scope"
                 else None)
        repaired, errors = _member_triples(candidate, payload.get("edges"),
                                           scope_conditions=scope)
        if errors and not repaired:
            return Dropped("invalid_payload", "; ".join(errors))
        return RepairedCandidate(choice, triples=repaired)

    if choice == "named_group":
        slot = flagged_endpoint(candidate, guard_class, thresholds)
        repaired = dict(candidate)
        repaired[slot] = payload["group_name"]
        if payload.get("group_type"):
            repaired[f"{slot}_type"] = str(payload["group_type"])
        _ensure_scope(repaired)
        try:
            validate_triple(repaired)
        except (ContractError, TypeError, ValueError) as error:
            return Dropped("invalid_payload", f"named-group repair invalid: {error}")
        return RepairedCandidate(choice, triples=[repaired])

    if choice == "normalize_name":
        slot = flagged_endpoint(candidate, guard_class, thresholds)
        repaired = dict(candidate)
        repaired[slot] = payload["name"]
        qualifier = str(payload.get("qualifier", "")).strip()
        if qualifier:
            repaired["scope_conditions"] = (
                f"{candidate.get('scope_conditions', '')} {qualifier}".strip()).strip()
        _ensure_scope(repaired)
        try:
            validate_triple(repaired)
        except (ContractError, TypeError, ValueError) as error:
            return Dropped("invalid_payload", f"name repair invalid: {error}")
        return RepairedCandidate(choice, triples=[repaired])

    if choice == "missing_endpoint":
        repaired = dict(candidate)
        repaired["object"] = payload["endpoint"]
        if payload.get("endpoint_type"):
            repaired["object_type"] = str(payload["endpoint_type"])
        _ensure_scope(repaired)
        try:
            validate_triple(repaired)
        except (ContractError, TypeError, ValueError) as error:
            return Dropped("invalid_payload", f"endpoint repair invalid: {error}")
        return RepairedCandidate(choice, triples=[repaired])

    if choice == "to_source":
        record = _flagged_record(candidate, guard_class, choice, payload)
        record["reason"] = f"deictic/meta routed to source layer: {payload['note']}"
        return RepairedCandidate(choice, flagged=[record])

    if choice == "pass_flagged":
        return RepairedCandidate(
            choice, flagged=[_flagged_record(candidate, guard_class, choice, payload)])

    if choice == "drop_noise":
        return Dropped("drop_noise", f"{guard_class} dropped as noise by dispatch")

    return Dropped("invalid_payload", f"unknown repair step {choice!r}")


__all__ = [
    "CATEGORY_MENUS",
    "DispatchError",
    "DispatchStep",
    "Dispatcher",
    "Dropped",
    "GUARD_CLASSES",
    "GuardThresholds",
    "JevDispatchClient",
    "RepairedCandidate",
    "classify_candidate",
    "dispatch",
    "flagged_endpoint",
]
