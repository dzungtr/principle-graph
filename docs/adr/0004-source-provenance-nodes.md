# 0004 — :Source provenance nodes: per-source walking over the ledger

Date: 2026-08-30 · Status: accepted · PRD: #76 · Issues: #64, #79

Ledger rows carry a denormalized `source_ref` string (`book-1:chapter-2/section-3/page-14`), which answers "which chunk produced this row?" but not "what did this document claim?" — that question needs a full-graph scan with string prefix matching, and once a row's claim is contradicted later it still deserves to surface as something that source asserted.

## Decision

Decision: add one **`:Source` node per source id** — the `source_ref` prefix before the first colon — carrying the id plus `first_seen` metadata, and link every row to it: `(:ExtractionEvent)-[:FROM_SOURCE]->(:Source)`. The link is **additive**: rows keep the denormalized `source_ref` string unchanged, because ledger identity `(subject, relation, object, source_ref)` depends on it and Neo4j Community 5.x cannot enforce composite uniqueness across relationship endpoints (ADR-0002). New ingestion writes the node and edge in the same row merge; a one-off idempotent pass (`pg backfill-sources`) creates nodes and edges for existing rows; `pg provenance <source-id>` walks everything one source claimed, rows later contradicted included. The arrow layer is untouched. This amends ADR-0002's two-layer schema with a third, derived node kind; the amendment is additive by the same rule the ledger itself follows — append, never rewrite.

## Considered Options

- **Replace `source_ref` with the node link:** rejected — ledger identity MERGEs on the string; rewriting it re-opens ADR-0002's idempotency guarantees for zero gain. The string stays; the edge is derived from its prefix.
- **Source id denormalized onto each row:** rejected — duplicates the id per row, still requires prefix scans, and gives first-seen metadata nowhere to live. A node makes "everything this source claimed" a one-hop, index-backed walk.
- **Fold the backfill into `migrate-ledger`:** rejected — #60's pass is closed, pinned idempotent, and single-purpose; a separate `backfill-sources` pass keeps each migration independently re-runnable and reviewable.
- **Auto-create `:Source` nodes for prefix-less refs:** rejected — a row whose `source_ref` has no valid id prefix has no walkable source. New ingestion rejects it before any write and the backfill raises it as a data error, so `:Source` coverage stays total by construction.

## Consequences

- Per-source provenance becomes `MATCH (:Source {id})<-[:FROM_SOURCE]-(:ExtractionEvent)` — one hop over a range-indexed id.
- The fact-checker (#62, ADR-0005) can walk a source's rows without mutating them; a contradicted row remains visible as that source's claim.
- Source ids inherit the `source_ref` prefix grammar; renaming a source's id strands its old node — ids are stable identities, not display labels.
- The backfill is a data pass, not a schema change: `pg init` gains only a range index on `Source.id`.

## Measured results

(filled at initiative close, PRD #76 Results)

- :Source node inventory (ids, row coverage): **pending demo-graph measurement** — the mechanism is covered by `tests/test_source_provenance.py` (prefix extraction, node/edge creation, no-op re-run) and ingest writes `FROM_SOURCE` on both write paths (PR #85); the demo source was removed from the repo (#50), so the rebuilt demo graph's inventory must be measured at the next demo run.
- Backfill scale / second-run no-op evidence: no-op idempotency proven in `tests/test_source_provenance.py` (`pg backfill-sources` twice changes no state); scale pending demo-graph measurement.
- Suite: 182 → 210 at this slice's merge (PR #85, e21d1bb).
