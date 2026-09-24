# 0007 — Quantities and qualitative measurements as state on entities: the StateEvent two-layer pattern

Date: 2026-09-24 · Status: accepted · Spec: #95

The demo-notes ingest materialized **19 quantity-like entity nodes** (`14%`, `18.8%`, `€130 billion`, `4-4.25%` — including *triplicate* variants of one $1 trillion borrowing fact). The extraction contract offered exactly one shape — `(entity) -[verb]-> (entity)` — so every measurement was forced to become a node. Quantity nodes are dead ends (nothing ever fans out from `14%`), duplicate freely across surface variants, and are semantically wrong: a number is a property of a thing, not a thing.

## Decision

Add a second extraction primitive, `propose_state` — `{entity, entity_type, state_key, value, unit, as_of}` plus the standard provenance fields — for numeric **and qualitative** measurements ("yields remain elevated" is a state, not an entity). Storage follows the ADR-0002 two-layer precedent exactly:

- **History:** append-only `:StateEvent` ledger rows, identity `(entity, state_key, source_ref)`, keep-first (same-source re-ingest a structural no-op), wired `(:Entity)-[:HAS_STATE_EVENT]->(:StateEvent)`.
- **Current state:** a denormalized state map on the Entity node, recomputed from rows — latest `as_of` wins, then confidence; disagreement rows remain queryable, mirroring ADR-0002's derived-Arrow design.
- `state_key` canonicalizes against a registry at the write boundary; unknown keys pass flagged (the ADR-0003 never-reject stance).
- State assertions pass the ADR-0006 novelty gate rendered as plain claims ("Entity's state_key is value"), deduped by `(entity, state_key, value)`: garbage numbers never annotate canonical entities.

## Considered Options

- **Quantity nodes with a `quantity` type** (status quo plus a type): rejected — keeps dead-end nodes and surface-variant duplicates; the shape problem is the problem.
- **Quantity values on the Arrow** (edge property): rejected — the measurement belongs to an entity, not a relationship, and per-source history would reintroduce edge mutation, the exact problem ADR-0002 removed.
- **Separate `:State` nodes** (`(:Entity)-[:HAS_STATE]->(:State)`): rejected — an extra hop for what is a denormalized read of StateEvent rows; the Arrow precedent keeps derived current state on the node itself.

## Consequences

- Zero quantity nodes; numbers enrich canonical entities. Three sources mentioning Merz's approval converge on one node — corroboration concentrates instead of fragmenting, which is the fragmentation fix (spec #99) working in the same direction.
- A whole family of state-verbs (`HAS_APPROVAL_RATING`, `RECEIVED_VOTE_SHARE`, `PREDICTED_TO_HIKE_TO`, `HAS_10_YEAR_BORROWING_RATE_ABOVE`) disappears from the relation vocabulary — they were state assertions in edge costume.
- Joint-claim magnitudes ("Canada and Australia produce roughly a third") have no single entity to attach to — they stay in `scope_conditions` (routing per ADR-0008's dispatch).
- Fan-out output carries seed states: numbers join direction generation.
