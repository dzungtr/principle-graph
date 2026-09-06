"""Decide-mode fact-checking: pure orchestrator over ledger rows (issue #80).

The pipeline is pure: rows go in, receipts come out, and the web searcher and
verdict LLM are injected seams (fakes in tests, no network). Verdicts are
receipt-shaped and append-only — they never mutate ledger rows or arrow
confidences; a human decides (PRD #76, ADR-0005). The local verdict LLM rides
the extraction transport (ADR-0001) via the same OpenAI-compatible client.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .ledger import LedgerRow

VERDICTS = ("support", "refute", "unclear")


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str


class WebSearch(Protocol):
    def search(self, query: str) -> Sequence[SearchResult]: ...


@dataclass(frozen=True)
class VerdictOutput:
    verdict: str
    confidence: float
    reasoning: str


class VerdictLLM(Protocol):
    def check(self, claim: str, evidence: str) -> VerdictOutput: ...


@dataclass(frozen=True)
class VerdictReceipt:
    """One append-only verdict, written to the graph as a :Verdict node."""

    subject: str
    relation: str
    object: str
    source_ref: str
    verdict: str
    confidence: float
    evidence_urls: tuple[str, ...]
    model: str
    search_provider: str
    reasoning: str
    created_at: str


def _claim_text(row: LedgerRow) -> str:
    parts = [f"{row.subject} {row.relation} {row.object}"]
    if row.scope_conditions:
        parts.append(f"(scope: {row.scope_conditions})")
    return " ".join(parts)


def fact_check_rows(
    rows: Sequence[LedgerRow],
    *,
    searcher: WebSearch,
    llm: VerdictLLM,
    model: str,
    search_provider: str,
    now: Callable[[], str],
) -> list[VerdictReceipt]:
    """Run the search-grounded verdict pipeline over each fetched row.

    Pure: one search per row, one verdict LLM call per row, receipts out.
    Invalid model output (unknown verdict enum or out-of-range confidence)
    raises ``ValueError`` — malformed verdicts are never written.
    """
    receipts: list[VerdictReceipt] = []
    for row in rows:
        claim = _claim_text(row)
        results = list(searcher.search(claim))
        evidence = "\n".join(
            f"- [{r.title}] {r.url}: {r.snippet}" for r in results
        ) or "(no search results)"
        output = llm.check(claim, evidence)
        if output.verdict not in VERDICTS:
            raise ValueError(
                f"verdict LLM returned invalid verdict {output.verdict!r}: "
                f"expected one of {', '.join(VERDICTS)}")
        confidence = float(output.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"verdict confidence {confidence} outside [0.0, 1.0]")
        receipts.append(VerdictReceipt(
            subject=row.subject, relation=row.relation, object=row.object,
            source_ref=row.source_ref, verdict=output.verdict,
            confidence=confidence,
            evidence_urls=tuple(r.url for r in results if r.url),
            model=model, search_provider=search_provider,
            reasoning=output.reasoning, created_at=now(),
        ))
    return receipts


def fact_check_candidates(
    written_rows: Sequence[LedgerRow],
    existing_rows: Sequence[LedgerRow],
) -> list[LedgerRow]:
    """Rows that a repeat-source ingestion into an occupied domain surfaces.

    A domain triggers when newly written rows and pre-existing rows in that
    domain come from different sources. The returned candidates are the
    overlapping domain's rows — existing first, then written — deduplicated by
    ledger identity. Untagged rows never trigger (empty domain).
    """
    written_domains = {row.domain for row in written_rows if row.domain}
    overlapping: set[str] = set()
    for domain in written_domains:
        existing_sources = {
            row.source_ref.split(":", 1)[0]
            for row in existing_rows if row.domain == domain
        }
        written_sources = {
            row.source_ref.split(":", 1)[0] for row in written_rows
            if row.domain == domain
        }
        if existing_sources - written_sources:
            overlapping.add(domain)
    if not overlapping:
        return []
    candidates: list[LedgerRow] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in [*existing_rows, *written_rows]:
        if row.domain not in overlapping:
            continue
        if row.identity in seen:
            continue
        seen.add(row.identity)
        candidates.append(row)
    return candidates


def candidates_for_domains(
    domains: Sequence[str],
    fetch_rows: Callable[[str], Sequence[LedgerRow]],
) -> list[LedgerRow]:
    """Fact-check candidates for committed domains (the ingest-run trigger).

    A domain fires when its rows come from more than one distinct source (an
    ingestion wrote into a domain already held by a different source). All rows
    in firing domains are returned, deduplicated by ledger identity; untagged
    ("") domains never trigger.
    """
    candidates: list[LedgerRow] = []
    seen: set[tuple[str, str, str, str]] = set()
    for domain in sorted({d for d in domains if d}):
        rows = list(fetch_rows(domain))
        if len({row.source_ref.split(":", 1)[0] for row in rows}) < 2:
            continue
        for row in rows:
            if row.identity in seen:
                continue
            seen.add(row.identity)
            candidates.append(row)
    return candidates


def fact_check_notice(
    domains: Sequence[str],
    fetch_rows: Callable[[str], Sequence[LedgerRow]] | None,
) -> list[str]:
    """End-of-ingest lines surfacing fact-check candidates, if any.

    Never raises and never fetches when the writer has no domain reader:
    surfacing is advisory — the check itself runs via ``pg fact-check``.
    """
    if fetch_rows is None:
        return []
    try:
        candidates = candidates_for_domains(domains, fetch_rows)
    except Exception:
        return []
    if not candidates:
        return []
    fired = sorted({row.domain for row in candidates})
    return [
        f"Fact-check candidates: {len(candidates)} row(s) across "
        f"{len(fired)} domain(s) with rows from multiple sources "
        f"({', '.join(fired)}) — run: pg fact-check --domain <domain>",
    ]


VERDICT_TOOL = {
    "name": "propose_verdict",
    "description": (
        "Record a fact-check verdict for the claim, grounded in the supplied "
        "web-search evidence."),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(VERDICTS),
                "description": "support / refute / unclear",
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in the verdict",
            },
            "reasoning": {
                "type": "string",
                "description": "short justification citing the evidence",
            },
        },
        "required": ["verdict", "confidence", "reasoning"],
    },
}

VERDICT_SYSTEM_PROMPT = (
    "You are a careful fact-checker. Given a claim from a knowledge graph and "
    "web-search evidence, decide whether the evidence supports or refutes the "
    "claim, or is unclear. Use propose_verdict exactly once."
)


class LocalVerdictLLM:
    """Verdict LLM over the extraction seam's OpenAI-compatible transport.

    Rides the local-model gateway (ADR-0001) with a forced ``propose_verdict``
    tool call, mirroring the extraction contract's tool-use pattern.
    """

    def __init__(self, client, *, model: str) -> None:
        self._client = client
        self._model = model

    def check(self, claim: str, evidence: str) -> VerdictOutput:
        response = self._client.create(
            model=self._model,
            system=VERDICT_SYSTEM_PROMPT,
            max_tokens=512,
            tools=[VERDICT_TOOL],
            tool_choice={"type": "function", "name": "propose_verdict"},
            messages=[{
                "role": "user",
                "content": f"Claim: {claim}\n\nWeb-search evidence:\n{evidence}",
            }],
        )
        blocks = [b for b in response.content if b.name == "propose_verdict"]
        if not blocks:
            raise ValueError("verdict LLM returned no propose_verdict tool call")
        payload = blocks[0].input
        return VerdictOutput(
            verdict=str(payload.get("verdict", "")),
            confidence=float(payload.get("confidence", -1.0)),
            reasoning=str(payload.get("reasoning", "")),
        )


class HtmlWebSearcher:
    """Minimal real WebSearch seam: DuckDuckGo's HTML endpoint via urllib.

    Used only by the real CLI path; tests inject fakes. Best-effort parsing of
    result links and snippets — network failures raise to the caller.
    """

    _URL = "https://html.duckduckgo.com/html/?q={query}"
    _LINK = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    _SNIPPET = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
    _TAG = re.compile(r"<[^>]+>")

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    @staticmethod
    def _clean(fragment: str) -> str:
        return html.unescape(HtmlWebSearcher._TAG.sub("", fragment)).strip()

    def search(self, query: str) -> list[SearchResult]:
        request = urllib.request.Request(
            self._URL.format(query=urllib.parse.quote_plus(query)),
            headers={"User-Agent": "principle-graph-factcheck/1.0"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
        links = [
            (self._clean(title), urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0]))
            for title, href in self._LINK.findall(page)
        ]
        snippets = [self._clean(s) for s in self._SNIPPET.findall(page)]
        return [
            SearchResult(url=url, title=title,
                         snippet=snippets[i] if i < len(snippets) else "")
            for i, (title, url) in enumerate(links[:5])
        ]
