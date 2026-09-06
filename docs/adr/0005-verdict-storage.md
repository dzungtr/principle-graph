# 0005 — In-graph verdict storage: append-only fact-check receipts

Date: 2026-09-06 · Status: accepted · PRD: #76 · Issues: #62, #80

Decide-mode fact-checking (the third mode of the add/decide/skip taxonomy that seeded PRD #57) needs somewhere to put what the fact-checker concluded. ADR-0002 explicitly deferred this re-decision. The ledger is accepted-only truth: rows and arrow confidences move only through accepted extractions. A fact-check verdict is an opinion about a row — it must not be able to rewrite the row it judges.

## Decision

Decision: store verdicts **in the graph as append-only, receipt-shaped `:Verdict` nodes**, wired to the rows they check: `(:Verdict)-[:CHECKS]->(:ExtractionEvent)`. Each verdict carries the verdict enum (`support` / `refute` / `unclear`), a confidence, evidence URLs, model + search provenance, reasoning, and a write timestamp. The store only `CREATE`s — never `MERGE`s — so every fact-check run appends fresh receipts and a re-run can never rewrite history. Rows and arrows are never mutated by verdicts; the sidecar JSONL keeps its existing role for raw rejects, and the ledger's status semantics are untouched. Verdicts inform; a human decides.

The fact-check orchestrator is a pure pipeline over fetched ledger rows with the web searcher and verdict LLM injected as seams (fakes in tests — no network, no database). The verdict LLM rides the existing local-model transport (ADR-0001) via the same OpenAI-compatible client, forcing a `propose_verdict` tool call exactly as the extraction contract does; the web searcher is a minimal HTML-endpoint client behind the same `WebSearch` protocol. The trigger is pure detection: an ingestion that writes rows into a domain whose rows come from a different source surfaces candidates in the run report; the check itself runs on demand via `pg fact-check --domain <domain> | --source <source-id>`, walking rows through slice 3's `:Source` layer when checking per source.

## Considered Options

- **JSON sidecar file:** rejected — verdicts would be invisible to graph queries ("what refutes this claim?"), duplicating the exact gap `source_ref` strings had before ADR-0004.
- **Properties on the row (`row.verdict = ...`):** rejected — mutates ledger rows and collapses a row's verdict history to the latest opinion; the ledger must stay accepted-only truth.
- **MERGE one verdict per row (upsert):** rejected — a re-run would silently overwrite the earlier verdict. Verdicts are receipts of runs; append-only keeps the full history auditable.
- **Auto-apply verdicts to arrow confidence:** rejected — violates the ledger's complement-aggregate policy (ADR-0002) and the PRD's explicit "verdicts inform; humans decide" boundary.

## Consequences

- "What does the graph believe, and what did the fact-checker find?" is one query: `MATCH (v:Verdict)-[:CHECKS]->(e:ExtractionEvent)`.
- Verdict volume grows linearly with fact-check runs; consolidation (e.g. latest-wins views) is a read-time concern, deliberately deferred.
- The trigger is advisory by design: ingestion surfaces candidates, the human (or an agent) chooses to run the check — no model calls happen implicitly during ingest.
- `pg init` gains only a range index on `Verdict.id`; no migration is needed for existing graphs.

## Measured results

(filled at initiative close, PRD #76 Results)

- Fact-check verdict volume + accuracy spot-check: **pending demo-graph measurement** — the pipeline is fully covered by fake-searcher/fake-verdict-LLM tests (`tests/test_factcheck.py`, database- and network-free; PR #89), but no live fact-check run has occurred: it needs a populated multi-source graph plus the web-search and verdict-LLM services.
- Suite: 182 → 304 at this slice's merge (PR #89, d22c6f6; 182 → 210 slice 3 #85, → 265 slice 1 #86, → 281 slice 2 #87).
