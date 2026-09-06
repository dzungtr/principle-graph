# 0005 — In-graph, append-only :Verdict store for decide-mode fact-checking

Date: 2026-08-30 · Status: accepted · PRD: #76

ADR-0002 explicitly deferred a re-decision: "when decide-mode fact-checking (#62) lands,
verdict storage will need either in-graph statuses or a verdict sidecar." Decide mode is the
unimplemented third of the add/decide/skip taxonomy that seeded PRD #57. Rejects stay in the
JSONL sidecar; fact-check verdicts had no home.

## Decision

Decision: verdicts are **in-graph, append-only, receipt-shaped nodes** — not row statuses,
not a sidecar.

- `(:Verdict)-[:CHECKS]->(:ExtractionEvent)`; fields: verdict (`support` / `refute` /
  `unclear`), confidence, evidence URLs, model + search provenance, timestamp.
- Rows and arrow confidences are **never mutated** by verdicts. The ledger stays
  accepted-only truth; verdicts inform, humans decide.
- Trigger: an ingestion run writing rows into a domain already holding rows from a different
  source surfaces candidate rows; a CLI run produces verdicts over them.
- The fact-check orchestrator is a pure pipeline over fetched rows with injected web
  searcher and verdict LLM (fakes in tests; the verdict LLM rides the local-model
  transport, ADR-0001). Re-runs append fresh verdicts; they never rewrite.
- Per-source verdict walking rides the `:Source` layer (ADR-0004).

This resolves ADR-0002's recorded re-decision and closes #62.

## Considered Options

- **Verdict sidecar (JSONL, like raw rejects):** rejected — verdicts invisible to every
  graph query; #64's provenance walking would have a blind spot exactly where trust matters.
- **Unified row status field (verdict as row state):** rejected — the same reason ADR-0002
  rejected rejects-as-statuses: the ledger stops being clean accepted truth, and verdict
  history is lost to last-write-wins.

## Consequences

- The graph gains a third node kind; verdict volume grows with fact-check runs (append-only,
  cheap) and old verdicts remain auditable.
- "Everything this source claimed, with current verdicts" becomes one traversal — the
  original #62/#64 motivation.
- Deciding what to do with a refuted row stays human work; no automatic re-approval or
  status change exists in this decision.
- The searcher and LLM interfaces become part of the tested surface — the suite stays
  database- and network-free.

## Measured results

To be filled at initiative close (PRD #76): verdict volume + accuracy spot-check, trigger
reachability on the demo corpus, suite growth.
