# 0004 — :Source document nodes for per-source provenance walking

Date: 2026-08-30 · Status: accepted · PRD: #76

After ADR-0002, provenance lives on `:ExtractionEvent` rows as a flat `source_ref` string
(`book-1:chapter-2/section-3/page-14`). Walking "everything this document claimed, including
rows later contradicted" requires parsing that string at query time for every row — no
per-source handle exists (#64).

## Decision

Decision: add `:Source` nodes — one per **source id**, the `source_ref` prefix before the
first `:` — wired additively:

- `(:ExtractionEvent)-[:FROM_SOURCE]->(:Source)` on every new ingestion.
- A backfill pass creates nodes and edges for existing rows; idempotent on re-run.
- The denormalized `source_ref` string **stays** on the row: ledger identity is
  `(subject, relation, object, source_ref)` and Neo4j Community cannot enforce composite
  uniqueness across relationship endpoints (ADR-0002), so identity must remain a row
  property.

## Considered Options

- **Node links only (drop the string):** rejected — breaks the identity MERGE; would reopen
  ADR-0002's identity decision.
- **Query-time prefix parsing (no nodes):** rejected — every provenance walk re-derives the
  same split; no place to hang first-seen metadata or future re-approval flows.

## Consequences

- One node + one edge per source; per-source walks and later verdict-per-source queries
  become plain traversals.
- `:Source` identity is only as stable as the prefix discipline of `source_ref`; ingestion
  keeps the extraction contract's verbatim-source-ref rule, which pins it.
- Rows gain no new properties; the ledger layer is untouched.

## Measured results

To be filled at initiative close (PRD #76): backfill scale on the demo graph (sources,
edges, idempotency), row coverage of the provenance walk.
