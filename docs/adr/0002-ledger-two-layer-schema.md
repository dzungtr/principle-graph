# 0002 — Two-layer graph schema: derived Arrows over an append-only ExtractionEvent ledger

Date: 2026-08-29 · Status: accepted · PRD: #57

The prototype stored each relationship as a single typed edge carrying one aggregate confidence plus an evidence list, updated in place. `confidence-policy.md` (2026-08-03) accepted complement aggregation and append-only evidence, but that policy is unimplementable on this shape: there is nowhere to put per-extraction facts. The actual write path was last-write-wins — a second source extracting the same claim destroyed the first's confidence and evidence (verified in the 2026-08-29 smoke test). Corroboration was invisible and disagreement unqueryable.

Decision: split current state from history into two layers. The typed relationship survives as the **Arrow** — current state only: one derived aggregate confidence (recomputed as `1 − Π(1 − ci)` over accepted rows, clamped, never set independently) plus a denormalized latest-merged scope condition. History moves to **`:ExtractionEvent` ledger rows** — one append-only node per accepted extraction, identified by `(subject, relation, object, source_ref)`, wired via `(:Entity)-[:REPORTED]->(:ExtractionEvent)-[:ABOUT]->(:Entity)`, each carrying its own event confidence, single-string evidence, scope conditions, source ref, optional domain tag, relation, and timestamps. Ledger identity makes same-source re-extraction a structural no-op (keep-first, the default); a `refresh` mode may update a matched row deliberately, then recompute. Mode-2 rejects stay in the JSONL sidecar — the ledger holds accepted events only. The existing graph migrates in place: each edge becomes exactly one row; single-row aggregates equal prior confidences, so no arrow moves.

## Considered Options

- **Derived-only graph (no direct arrows; every query walks Entity→row→Entity):** rejected — the fan-out hot path and structural corroboration walk direct edges today; dropping arrows rewrites every read path for purity's sake. The denormalized Arrow is cheap and derived.
- **Per-run row identity (uuid per extraction run):** rejected — re-ingesting a document would append duplicates and force read-time dedup; `(triple, source_ref)` identity makes idempotent re-ingest structural.
- **Arrow keeps an evidence list (status quo plus aggregation in code):** rejected — the list-on-edge was the unimplementable halfway house; per-row storage is what makes the accepted policy computable as written.
- **Rejects unified into the ledger with a status field:** rejected for this initiative — graph stays clean truth; re-approval remains manual. The deferred fact-checker initiative (#62) explicitly re-opens this question.
- **Refresh as the default repeat mode:** rejected — LLM nondeterminism would let identical re-ingests wobble aggregates; keep-first preserves idempotency, refresh is the deliberate opt-in.

## Consequences

- Every accepted extraction is two writes (row merge + aggregate recompute); the aggregate is always derivable from rows, so drift is detectable and repairable by recomputation.
- Neo4j Community 5.x cannot express composite uniqueness across relationship endpoints — ledger identity is enforced by pattern MERGE in the write layer, joining today's app-enforced invariants (confidence bounds, timestamps).
- Fan-out output contract is preserved by design; provenance (refs, evidence) now joins via one ledger hop instead of edge properties.
- Per-domain belief (rows tagged `domain`) and disagreement hunts (min vs max per claim) become plain queries — no future schema change needed for either.
- The rejects question is deliberately left on the JSONL sidecar; when decide-mode fact-checking (#62) lands, verdict storage will need either in-graph statuses or a verdict sidecar — an explicit re-decision, recorded here so it is not made silently.

## Measured results

_To be promoted from PRD #57 Results at initiative close (migration scale, aggregate shifts, equality-test evidence)._
