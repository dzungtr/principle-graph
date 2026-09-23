"""Ingest novelty gate: classify extracted proposals against model knowledge (issue #93, ADR-0006).

Placement: after extract, before resolve. Each unique proposed relationship is
rendered as a plain knowledge claim and challenged by the Jev decision model
(``typesafe/jev-1.13`` via the OpenRouter Decisions API). ``novel`` saves;
``noise`` and ``common_sense`` drop. Skipped items cost no embedding,
resolution, or review work. Failure is a hard abort: with the filter enabled,
an unreachable Jev (network error, timeout, missing key, no credits) refuses
the ingest — no partial state, no silent unfiltered ingestion.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class NoveltyError(RuntimeError):
    """The novelty gate could not produce a verdict; the ingest must abort."""


VALID_CHOICES = ("novel", "noise", "common_sense")

NOVELTY_CRITERIA: dict[str, str] = {
    "noise": "Not a substantive claim about the world: filler, transitions, "
             "structural text, a question to the reader, an incomplete fragment, "
             "or generic scaffolding with no factual content.",
    "common_sense": "An educated general reader already holds this belief without "
                    "needing the source: textbook knowledge, definitions, widely "
                    "known facts, truisms. Nothing is learned by reading it.",
    "novel": "A substantive, specific claim a knowledgeable reader would plausibly "
             "not already hold and would learn from this source.",
}

NOVELTY_INSTRUCTIONS = (
    "Classify this claim against what an educated general reader already knows "
    "without reading the source document."
)


def render_claim(candidate: Mapping[str, Any]) -> str:
    """Render a candidate as a bare knowledge claim: subject, raw verb as words, object.

    Uses the extractor's own raw relation, lowercased with underscores as
    spaces (``MAY_DESCRIBE`` → ``may describe``). Scope conditions are
    deliberately excluded — bare-claim classification per ADR-0006.
    """
    relation_as_words = str(candidate["relation"]).strip().casefold().replace("_", " ")
    return f"{candidate['subject']} {relation_as_words} {candidate['object']}"


@dataclass(frozen=True)
class NoveltyVerdict:
    """One candidate's three-way classification plus the response probabilities."""

    candidate: Mapping[str, Any]
    choice: str
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class NoveltyStats:
    """Run-level aggregates for the transcript; per-item receipts are not kept (decision 3)."""

    novelty_calls: int = 0
    filtered_noise: int = 0
    filtered_common_sense: int = 0
    mean_probabilities: Mapping[str, float] = field(default_factory=dict)


class NoveltyFilter(Protocol):
    def classify(self, claims: Sequence[Mapping[str, Any]]) -> list[NoveltyVerdict]: ...


Transport = Callable[[str, Mapping[str, str], bytes, float], bytes]


class JevDecisionsClient:
    """Call the OpenRouter Decisions API with an injectable transport, like ``LLMGateway``."""

    def __init__(self, base_url: str = "https://openrouter.ai/api/alpha/decisions",
                 *, model: str = "typesafe/jev-1.13", api_key: str = "",
                 timeout: float = 10.0, transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport or self._request

    def classify(self, claims: Sequence[Mapping[str, Any]]) -> list[NoveltyVerdict]:
        """One decision call per claim, in order; any failure raises :class:`NoveltyError`."""
        verdicts: list[NoveltyVerdict] = []
        for claim in claims:
            verdicts.append(self._classify_one(claim))
        return verdicts

    def _classify_one(self, candidate: Mapping[str, Any]) -> NoveltyVerdict:
        if not self.api_key:
            raise NoveltyError(
                "novelty filter is enabled but OPENROUTER_API_KEY is unset; "
                "set it or pass --no-novelty-filter to opt out"
            )
        payload = {
            "model": self.model,
            "state": {"claim": render_claim(candidate),
                      "evidence": str(candidate.get("evidence", ""))},
            "questions": {"novelty": {
                "type": "choice",
                "instructions": NOVELTY_INSTRUCTIONS,
                "criteria": NOVELTY_CRITERIA,
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
        except NoveltyError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise NoveltyError(f"Jev decisions request failed: {getattr(error, 'reason', error)}") from None
        answer = None
        try:
            answer = response["answers"]["novelty"]
            if answer.get("type") != "choice":
                raise TypeError("novelty answer is not a choice")
            choice = answer["choice"]
            if choice not in VALID_CHOICES:
                raise ValueError(f"unexpected choice key: {choice!r}")
            probabilities = dict(answer.get("probabilities") or {})
        except (KeyError, TypeError, ValueError) as error:
            raise NoveltyError(f"Jev decisions returned a malformed answer: {error}") from None
        return NoveltyVerdict(candidate, choice,
                              float(answer.get("confidence", 0.0)), probabilities)

    def _request(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as result:
                return result.read()
        except urllib.error.HTTPError as error:
            raise NoveltyError(f"Jev decisions HTTP error {error.code}: {error.reason}") from None


def apply_novelty_filter(
    candidates: Sequence[Mapping[str, Any]],
    filter: NoveltyFilter,
) -> tuple[list[Mapping[str, Any]], NoveltyStats]:
    """Dedup by casefolded raw triple, classify each unique once, argmax-gate (decision 2 + 5).

    Duplicates of a kept triple share its verdict and flow on; residual
    asymmetry fails toward saving. Failure inside the filter propagates — the
    caller aborts before resolve/assemble/commit (decision 4).
    """
    if not candidates:
        return [], NoveltyStats()
    unique: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for candidate in candidates:
        key = (str(candidate["subject"]).casefold(),
               str(candidate["relation"]).casefold(),
               str(candidate["object"]).casefold())
        if key not in unique:
            unique[key] = candidate
            order.append(key)
    verdicts = filter.classify([unique[key] for key in order])
    verdict_by_key = dict(zip(order, verdicts))
    kept: list[Mapping[str, Any]] = []
    noise = common_sense = 0
    probability_sums: dict[str, float] = {}
    probability_counts = 0
    for candidate in candidates:
        key = (str(candidate["subject"]).casefold(),
               str(candidate["relation"]).casefold(),
               str(candidate["object"]).casefold())
        verdict = verdict_by_key[key]
        if verdict.choice == "novel":
            kept.append(candidate)
        elif verdict.choice == "noise":
            noise += 1
        else:
            common_sense += 1
        if key in unique:  # count each decision's probabilities exactly once
            for label, probability in verdict.probabilities.items():
                probability_sums[label] = probability_sums.get(label, 0.0) + float(probability)
            probability_counts += 1
            unique.pop(key)  # mark decision consumed
    means = {label: total / probability_counts
             for label, total in probability_sums.items()} if probability_counts else {}
    return kept, NoveltyStats(
        novelty_calls=len(order),
        filtered_noise=noise,
        filtered_common_sense=common_sense,
        mean_probabilities=means,
    )


__all__ = [
    "JevDecisionsClient",
    "NoveltyError",
    "NoveltyFilter",
    "NoveltyStats",
    "NoveltyVerdict",
    "apply_novelty_filter",
    "render_claim",
]
