# Neo4j prototype data model

This document defines the persisted graph contract for the Principle Graph prototype. The
runnable DDL is [`neo4j-schema.cypher`](neo4j-schema.cypher).

## Nodes

### `Entity`

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `name` | string | yes | Canonical display name. |
| `type` | string | yes | Domain/entity type (validated by extraction, not a Neo4j enum). |
| `embedding` | list<float> | no | Voyage `voyage-3` embedding, 1024 dimensions. |
| `created_at` | datetime | yes | First persistence time. |
| `updated_at` | datetime | yes | Last mutation time. |

`(name, type)` is the canonical identity. Names may repeat across types, while a name/type
pair may occur only once. Aliases are not persisted in this slice; resolution can map aliases
to this identity later.

## Relationships

Every typed, directed relationship uses its domain relation as the Neo4j relationship type
(for example, `:INCREASES`). Relationship types must be normalized to uppercase identifiers
before Cypher is generated; values are not interpolated directly into queries.

| Property | Type | Required | Meaning |
|---|---|---:|---|
| `confidence` | float | yes | Current aggregate confidence in `[0.0, 1.0]`. |
| `evidence` | list<string> | yes | Paraphrased supporting snippets, retaining all accepted evidence. |
| `scope_conditions` | string | no | Qualifiers limiting the relationship. |
| `source_ref` | string | yes | Source/chunk or page reference for the extraction. |
| `created_at` | datetime | yes | First persistence time. |
| `updated_at` | datetime | yes | Last mutation time. |

A relationship is identified for prototype purposes by `(start Entity, relationship type,
end Entity)`. Confidence aggregation and repeat-extraction behavior are specified by issue
#4; this schema deliberately does not impose that policy.

## Constraints and indexes

`docs/schema/neo4j-schema.cypher` applies:

- a uniqueness constraint on `Entity(name, type)`;
- a 1024-dimensional cosine vector index on `Entity.embedding`, matching Voyage `voyage-3`.

Neo4j property types are enforced by the application write layer (including confidence bounds,
non-null required fields, and timestamp assignment). Neo4j does not support a property schema
constraint for all of these values in Community 5.x, so the schema DDL stays portable and
idempotent.
