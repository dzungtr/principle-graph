# Neo4j prototype data model

This document defines the persisted graph contract for the Principle Graph prototype. The
runnable DDL is [`neo4j-schema.cypher`](neo4j-schema.cypher).

## Nodes

### `Entity`

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `name` | string | yes | Canonical display name. |
| `type` | string | yes | Domain/entity type (validated by extraction, not a Neo4j enum). |
| `embedding` | list<float> | no | Local Ollama `bge-m3` embedding, 1024 dimensions (ADR-0001). |
| `created_at` | datetime | yes | First persistence time. |
| `updated_at` | datetime | yes | Last mutation time. |

`(name, type)` is the canonical identity. Names may repeat across types, while a name/type
pair may occur only once. Aliases are not persisted in this slice; resolution can map aliases
to this identity later.

## Nodes

### `ExtractionEvent`

Ledger row (ADR-0002): one append-only extraction event per accepted extraction, wired
`(:Entity)-[:REPORTED]->(:ExtractionEvent)-[:ABOUT]->(:Entity)`. Row identity is
`(subject, relation, object, source_ref)`, enforced by the write layer's pattern MERGE —
Neo4j Community 5.x cannot express composite uniqueness across relationship endpoints.
Re-ingesting the same claim from the same source reference is therefore a structural
no-op under the default keep-first mode.

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `relation` | string | yes | Domain relation this row reports, in canonical registry form (ADR-0003). Part of row identity. |
| `raw_relation` | string | no | The verb exactly as extracted, before registry normalization (ADR-0003). Empty on rows written before ADR-0003. Excluded from identity. |
| `source_ref` | string | yes | Source/chunk or page reference; part of row identity. |
| `confidence` | float | yes | This extraction event's own confidence in `[0.0, 1.0]`. |
| `evidence` | string | yes | Single supporting snippet — lists exist nowhere anymore. |
| `scope_conditions` | string | no | Qualifiers claimed by this extraction. |
| `domain` | string | no | Optional domain tag, canonicalized against `domain-registry.yaml` at the write boundary (aliases collapse, unknowns pass through flagged); untagged when absent (slice #78). |
| `created_at` | datetime | yes | First persistence time. |
| `updated_at` | datetime | yes | Last touch time (keep-first never rewrites values). |

### `Source`

Per-source provenance node (ADR-0004): one per source id — the `source_ref`
prefix before the first colon (for example `book-1` in
`book-1:chapter-2/page-14`). Created automatically on ingestion and by the
idempotent `pg backfill-sources` pass for pre-existing rows.

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `id` | string | yes | Source id: the `source_ref` prefix before the first colon. |
| `first_seen` | datetime | yes | `created_at` of the earliest row linked to this source. |

### `Verdict`

Append-only fact-check receipt (ADR-0005): one node per verdict produced by
`pg fact-check`. Never merged — each run appends a fresh receipt, and rows or
arrows are never mutated by a verdict (verdicts inform; humans decide).
Linked to the rows it checks via `(:Verdict)-[:CHECKS]->(:ExtractionEvent)`;
`pg fact-check --domain <d>` and the per-source walk read these receipts.

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `id` | string | yes | UUID of the receipt. |
| `verdict` | string | yes | One of `support` / `refute` / `unclear`. |
| `confidence` | float | yes | Verdict LLM self-reported confidence, 0.0–1.0. |
| `evidence_urls` | list[string] | yes | Web-search evidence URLs backing the verdict. |
| `model` | string | yes | Verdict LLM provenance (model id). |
| `search_provider` | string | yes | Web-search provenance (e.g. `ddg`). |
| `reasoning` | string | yes | Verdict LLM's stated rationale. |
| `created_at` | string | yes | ISO timestamp from the receipt's injected `now()` clock. |

## Relationships (Provenance)

`(:ExtractionEvent)-[:FROM_SOURCE]->(:Source)` links every ledger row to the
source it came from (ADR-0004). The link is additive: rows keep their
denormalized `source_ref` string — ledger identity depends on it (ADR-0002).
`pg provenance <source-id>` walks everything one source claimed, including rows
later contradicted.

## Relationships (Arrows)

Every typed, directed relationship uses its domain relation as the Neo4j relationship type
(for example, `:INCREASES`). Relationship types must be normalized to uppercase identifiers
before Cypher is generated; values are not interpolated directly into queries. The relation
vocabulary itself is no longer open: incoming relations are canonicalized at the write
boundary against the versioned relation registry (`src/principle_graph/data/relation-registry.yaml`)
— alias spellings collapse onto their canonical verb and inverse-pair spellings flip to the
canonical direction (ADR-0003, which supersedes this document's earlier open-vocabulary
wording). Unknown verbs pass through flagged and are consolidated by a registry edit plus a
re-run of `pg normalize-relations`. The extracted verb is preserved on ledger rows as
`raw_relation`.

An Arrow carries current state only; history lives in the ledger above.

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `confidence` | float | yes | Derived aggregate `1 − Π(1 − ci)` over the triple's ledger rows, clamped to `[0.0, 1.0]`. Never set independently. |
| `scope_conditions` | string | no | Denormalized latest-merged qualifiers limiting the relationship. |
| `created_at` | datetime | yes | First persistence time. |
| `updated_at` | datetime | yes | Last recompute time. |

A relationship is identified for prototype purposes by `(start Entity, relationship type,
end Entity)`. Confidence aggregation and repeat-extraction behavior follow
[ADR-0002](../adr/0002-ledger-two-layer-schema.md) over `:ExtractionEvent` rows; provenance
(evidence strings, source references) is read through one ledger hop. Arrows written before
the ledger migration may still carry legacy `evidence`/`source_ref` properties until the
backfill migration removes them.

## Constraints and indexes

`docs/schema/neo4j-schema.cypher` applies:

- a uniqueness constraint on `Entity(name, type)`;
- a 1024-dimensional cosine vector index on `Entity.embedding`, matching the local `bge-m3` embedding model (ADR-0001);
- a range index on `ExtractionEvent(source_ref)` for provenance lookups (ADR-0002);
- a range index on `Source(id)` for provenance walks (ADR-0004);
- a range index on `Verdict(id)` for append-only fact-check receipts (ADR-0005).

Neo4j property types are enforced by the application write layer (including confidence bounds,
non-null required fields, and timestamp assignment). Neo4j does not support a property schema
constraint for all of these values in Community 5.x, so the schema DDL stays portable and
idempotent.
