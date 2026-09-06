# 0003 — Write-boundary relation normalization through a versioned registry

Date: 2026-08-30 · Status: accepted · PRD: #76 (slice #77)

Re-ingesting the demo doc (2026-08-30) exposed a free-form relation taxonomy: 145 domain edges spread over 35 distinct relationship types, 27 of them singletons (#74, supersedes #63). The same claim extracted with different verbs (REPLACED in one chunk, SUPERSEDES in another) lands on two arrows and two ledger identities, so cross-document queries miss half the evidence; direction-sensitive writes let one fact land twice as A→B and B→A. The extraction contract (ADR-0002 era) pinned only the label *shape* — lowercase `snake_case` — leaving the vocabulary itself open.

## Decision

Decision: normalize relation labels deterministically at the **write boundary** — inside `Neo4jGraphWriter`, before any Cypher is generated — against a versioned data file, `src/principle_graph/data/relation-registry.yaml` (canonical verb, aliases, inverse pair, description). Three consumers share it: the writer (lookup), the query-time LLM prompt (vocabulary embedding), and humans consolidating overflow types.

- **Alias collapse:** an incoming `replaces` resolves to canonical `SUPERSEDES` before planning or Cypher generation.
- **Inverse-pair direction collapse:** the pair member that declares `inverse:` in the registry is the pair's canonical direction; an edge spelled with the other member flips to `(object, canonical, subject)` — one fact, one arrow.
- **Ledger identity uses the canonical verb.** The LLM's original verb is preserved on the row as `raw_relation` (excluded from identity). Amends #74's acceptance wording ("rows keep original verb"): the verb is kept as a property, not the identity key, so same-source verb variants collapse to one row and re-ingest stays structurally idempotent (ADR-0002).
- **Unknown verbs pass through, flagged** — warned once per distinct verb and counted per occurrence in the run summary; never rejected, so an unfamiliar book still ingests (extraction contract preserved: extraction still emits normalized snake_case freely). Consolidation = a registry edit plus a re-run of `pg normalize-relations`.
- **A one-off normalization pass** (`pg normalize-relations`) re-canonicalizes the existing graph: a pure plan module decides the rewrites, the writer executes them. Rows already canonical or carrying unknown verbs are untouched, so the second run is a full state no-op. This supersedes the open-vocabulary wording of the relation-label notes in `docs/schema/neo4j-schema.md`.

The same registry loader pattern (generic label registry) is reused by the domain registry (slice #78).

## Considered Options

- **LLM-based normalization in the write path:** rejected — nondeterminism in the write path breaks structural idempotency and adds latency to every ingest; the registry is a deterministic lookup.
- **Reject unknown verbs:** rejected — a strict enum would reject valid concepts in an unfamiliar book (the extraction contract's own reasoning).
- **Rows keep the original verb as the identity key (as #74 first worded it):** rejected — identity including the raw verb means REPLACED and SUPERSEDES from one source are two rows and two arrows; canonical identity is what makes the taxonomy queryable.
- **Normalize at read time:** rejected — every consumer (fan-out, corroboration, queries) would pay the resolution cost forever; normalizing once at the boundary keeps the graph canonical at rest.

## Consequences

- Registry edits are a data change plus a pass re-run — no code change — but unknown verbs accumulate as flagged passthroughs until someone edits the registry (the consolidation loop is manual by design).
- `raw_relation` adds a property to every row write; pre-registry rows carry it empty and their rewritten copies record the pre-normalization verb as the best available provenance.
- Inverse flips move rows across triples, so the pass deletes emptied arrows and recomputes target aggregates; a flipped rewrite is the newest statement for its target triple's scope.
- Query-time prompts must embed the canonical vocabulary (registry `vocabulary()`) so the LLM emits tokens the graph actually contains.

## Measured results

(filled at initiative close, PRD #76 Results)

- Suite growth: **181 → 234 passing** at this slice's merge (PR #86: +25 registry/normalization tests, database-free).
- Registry consolidation loop: exercised end-to-end by `tests/test_normalization_pass.py` (unknowns flagged, aliases added, re-run idempotent, raw_relation preserved, same-source verb variants collapse to one row).
- Migration scale / distinct-type reduction: **pending demo-graph measurement** — the demo source was removed from the repo (#50) and the extraction gateway is unavailable in this environment; re-run `pg migrate-ledger` + `pg normalize-relations` against a rebuilt demo graph to pin the distinct-type count.
