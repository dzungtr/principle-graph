# Novelty filter contract

Status: accepted for implementation (2026-09-23 design session) · ADR: [0006](adr/0006-ingest-novelty-gate.md)

## Problem

The last real ingest run showed two pollution modes in the proposed-extraction stream:

- **noise** — filler, structural text, and non-claims riding along as proposals;
- **common-sense facts** — textbook knowledge and truisms that an educated reader already holds.

Both enter the graph and commit as high-confidence arrows. A graph of truisms gives every
fan-out query an obvious-direction highway, which starves the graph's actual mission:
direction generation from what was *learned*, not what everyone already knows.

## Solution

A **novelty gate** inside `pg ingest`, between extraction and entity resolution. Each unique
proposed relationship is rendered as a plain knowledge claim and challenged by a decision
model — TypeSafe's Jev (`typesafe/jev-1.13`) via the OpenRouter Decisions API — which returns
one of three typed choices:

| Choice | Meaning | Action |
|---|---|---|
| `noise` | not a substantive claim about the world | drop |
| `common_sense` | an educated reader already holds this belief | drop |
| `novel` | something learned from this source | save |

Only novel proposals flow to resolve → assemble → review → commit.

## Decisions

Each decision was grilled and confirmed by the owner (2026-09-23). An implementer should treat
these as fixed; deviations need a new design conversation.

1. **Novelty reference frame — model knowledge, not graph state.** Novel means "would an
   educated reader already hold this belief without reading the source", judged by the
   decision model's own knowledge. It is *not* "is this fact already in the graph" — that is
   a dedup problem the ledger already solves: repeats are no-ops under `(triple, source_ref)`
   identity and cross-source duplicates are desirable (they feed confidence aggregation).

2. **Placement — after extract, before resolve.** Dedup candidates by casefolded raw
   `(subject, relation, object)` *before* classification, so the same proposal extracted from
   multiple chunks costs one decision call. Skipped proposals never cost embedding requests,
   resolution calls, ambiguity-queue churn, or review-screen space. Residual asymmetry is
   safe: two spellings of the same fact may both be classified, but a single `novel` verdict
   saves the fact — errors fail toward saving, never toward losing.

3. **Skipped facts — pure drop, no artifact.** No filter log, no per-item receipt
   (owner decision). The ingest transcript stats line still reports aggregates:
   `filtered items: N (noise=X, common_sense=Y)` plus run-level mean probabilities.
   Consequence accepted: criteria tuning has no confusion-matrix feedback; the fix path is
   re-running a source with edited criteria and observing.

4. **Failure policy — hard abort.** Filter enabled + Jev unreachable (network error, timeout,
   missing key, no credits) ⇒ the ingest run refuses to proceed; nothing commits. Scratch
   state is disposable by design ("a crash means rerun"). No partial state, no silent
   unfiltered ingestion. Matches the fail-fast registry precedent (`RegistryError`).

5. **Verdict semantics — pure argmax.** The top choice is the verdict; no confidence
   thresholds in v1 (they would be untunable knobs with no feedback data, per decision 3).
   Probabilities surface only as run-level stats averages.

6. **Posture — always-on, escape hatch.** The filter is the default path;
   `pg ingest --no-novelty-filter` opts out (bulk re-ingests, backfills). Consequently
   `OPENROUTER_API_KEY` is a required environment variable for `pg ingest` unless opted out.

7. **Jev contract — plain knowledge claim, zero product signature.** State carries only
   `claim` + `evidence` (see below). No entity types, no registry vocabulary, no scope
   conditions, no domain tags, no extractor confidence.

8. **Config — pinned model, configurable transport.** See [Config surface](#config-surface).

9. **Docs.** This contract + ADR-0006 + the `Novelty filter` glossary entry in CONTEXT.md.

## The Jev contract (frozen)

**Request** — `POST {jev_base_url}` (default `https://openrouter.ai/api/alpha/decisions`):

```json
{
  "model": "typesafe/jev-1.13",
  "state": {
    "claim": "Postel's Law may describe TCP",
    "evidence": "be conservative in what you send, liberal in what you accept"
  },
  "questions": {
    "novelty": {
      "type": "choice",
      "instructions": "Classify this claim against what an educated general reader already knows without reading the source document.",
      "criteria": {
        "noise": "Not a substantive claim about the world: filler, transitions, structural text, a question to the reader, an incomplete fragment, or generic scaffolding with no factual content.",
        "common_sense": "An educated general reader already holds this belief without needing the source: textbook knowledge, definitions, widely known facts, truisms. Nothing is learned by reading it.",
        "novel": "A substantive, specific claim a knowledgeable reader would plausibly not already hold and would learn from this source."
      }
    }
  }
}
```

**Claim rendering rule** (code, invisible to Jev):
`claim = f"{subject} {raw_relation_as_words} {object}"` — the extractor's own raw verb,
lowercased, underscores → spaces (`MAY_DESCRIBE` → `may describe`). Scope conditions are
deliberately excluded (bare-claim classification; accepted in grilling).

**Response** (shape to parse):

```json
{
  "id": "gen-dec-...",
  "model": "typesafe/jev-1.13-20260917",
  "provider": "TypeSafe",
  "answers": {
    "novelty": {
      "type": "choice",
      "choice": "novel",
      "confidence": 0.71,
      "probabilities": {"novel": 0.71, "common_sense": 0.24, "noise": 0.05}
    }
  },
  "usage": {"input_tokens": 96, "output_tokens": 20, "cost": 0.000004}
}
```

Gate rule: `answers.novelty.choice == "novel"` ⇒ save; anything else ⇒ drop. Non-`choice`
answer type or unexpected choice key ⇒ treat as transport failure (decision 4: abort).

## Jev integration facts (verified 2026-09-23)

- Endpoint: `POST https://openrouter.ai/api/alpha/decisions` — **not** chat completions.
- Auth: `Authorization: Bearer $OPENROUTER_API_KEY`; OpenRouter prepaid credits required
  (no free tier for Jev).
- Price: $0.042 per 1M input tokens, $0 per 1M output tokens. Measured per-decision cost
  ~$1.3–2.7e-05; measured latency 239–430 ms (P50 ~0.26 s). Context window 32,000 tokens.
- The response `model` field names the dated snapshot (e.g. `typesafe/jev-1.13-20260917`);
  sending `typesafe/jev-1.13` resolves to the current release — expected, not an error.
- Errors arrive as `{"error": {"code": ..., "message": ...}}` (OpenRouter wrapping).
- `UNVERIFIED:` the `~typesafe/jev-latest` alias is *reported* broken by one measured
  writeup while OpenRouter's own model page lists it — pinning `typesafe/jev-1.13`
  sidesteps the conflict entirely.
- `UNVERIFIED:` newer docs also mention `POST https://openrouter.ai/api/v1/systemone` as a
  sibling endpoint; the configurable `jev_base_url` covers a future cutover either way.

## Config surface

```python
# Settings additions (env overrides in parentheses)
jev_base_url: str = "https://openrouter.ai/api/alpha/decisions"   # PG_JEV_BASE_URL
jev_model:    str = "typesafe/jev-1.13"                           # PG_JEV_MODEL
jev_timeout:  float = 10.0                                        # PG_JEV_TIMEOUT
```

- Key: `OPENROUTER_API_KEY` (standard name, not `PG_`-wrapped — OpenRouter infra, not graph
  policy). Missing key + filter enabled ⇒ hard abort with a clear message.
- CLI: `pg ingest --no-novelty-filter` — the only flag this feature adds.

## Implementation plan (for the dispatch session)

- New module `src/principle_graph/novelty.py`:
  - `render_claim(candidate) -> str` — the rendering rule above.
  - `NoveltyFilter` protocol: `classify(claims) -> list[NoveltyVerdict]`.
  - `JevDecisionsClient(base_url, model, api_key, timeout, transport=None)` — transport
    injectable exactly like `LLMGateway` in `llm_gateway.py` (fakes in tests; no network).
  - `apply_novelty_filter(candidates, filter) -> (kept, stats)` — dedup by casefolded raw
    triple, one call per unique, argmax gate, aggregate stats (counts per choice, mean
    probabilities, call count).
- Orchestrator: optional `novelty_filter` seam applied to `run.candidates` between
  `_extract` and `_resolve`; `None`/opt-out ⇒ current behavior, zero Jev calls.
- CLI: wire `--no-novelty-filter` to skip the seam; default path constructs
  `JevDecisionsClient` from `Settings` and aborts early with a clear message when
  `OPENROUTER_API_KEY` is unset.
- `IngestStats`: `filtered_noise`, `filtered_common_sense`, `novelty_calls`, mean
  probabilities; rendered in `render()`.

## Testing decisions

Network-free, database-free, fake-transport — mirroring `tests/test_factcheck.py`:

1. Claim rendering: raw verb → plain words; state contains exactly `claim` + `evidence`.
2. Dedup: same casefolded raw triple from two chunks ⇒ one Jev call.
3. Gate semantics: `noise` / `common_sense` ⇒ dropped before resolve; `novel` ⇒ flows
   through to resolve/assemble.
4. Hard abort: transport raises ⇒ ingest dies before commit; no partial state.
5. Missing `OPENROUTER_API_KEY` with filter on ⇒ clear abort message.
6. `--no-novelty-filter` ⇒ zero Jev calls, current behavior unchanged.
7. Stats: filtered counts + mean probabilities render in the transcript.

Measured results for ADR-0006 stay **pending** until a live run against the demo graph.