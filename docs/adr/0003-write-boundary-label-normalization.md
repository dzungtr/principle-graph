# 0003 — Write-boundary label normalization via versioned registries

Date: 2026-08-30 · Status: accepted · PRD: #76

The extractor LLM picks each relationship verb freely (extraction contract: labels are an
extensible controlled vocabulary, not a closed enum). Re-ingesting the demo doc (2026-08-30)
showed the cost: 145 domain edges over **35 distinct relationship types**, 27 of them
singletons, plus direction drift (the same fact as A→B and B→A spellings). Cross-document
queries miss half the evidence; nothing deduplicates LLM vocabulary before Cypher writes.

## Decision

Decision: normalize labels at the **write boundary**, deterministically, from versioned
registry files — never by an LLM in the write path.

- `relation-registry.yaml`: canonical verb, aliases, inverse pair, description. A generic
  label-registry module (pure, loadable, testable without Neo4j) serves relation and domain
  registries alike.
- The writer canonicalizes before Cypher generation: alias collapse plus inverse-pair
  direction collapse (A→B spelling of a fact whose canonical direction is B→A lands as the
  canonical arrow).
- **Ledger identity uses the canonical verb.** Row `relation` holds the canonical form; the
  LLM's original verb is preserved as `raw_relation`. Same-source verb variants collapse to
  one row, keeping re-ingest structurally idempotent under normalization.
- Unknown labels pass through as-is, **flagged** (warning log + run-summary count).
  Consolidation is a registry edit plus a re-run of the one-off normalization pass — a data
  change, not a code change.
- The registry is embedded in query-time LLM prompts so query agents never guess tokens the
  graph does not contain.
- Domains mirror this pattern (`domain-registry.yaml`, optional `domain` on `propose_triple`,
  default untagged, no row backfill).

This supersedes the prototype-era free-form relation-type note in `docs/schema/neo4j-schema.md`
and closes #74 (which superseded #63).

## Considered Options

- **Closed vocabulary (fixed enum, retry/reject on miss):** rejected — a strict enum rejects
  valid concepts in an unfamiliar book, exactly what the extraction contract warns against;
  also forces guessing the enum per book in advance.
- **LLM canonicalization in the write path:** rejected — nondeterminism and latency in every
  write; a deterministic table covers the consolidation loop once humans curate aliases.
- **Raw verb as ledger identity (normalization only at query time):** rejected — same-source
  verb variants become duplicate rows; dedup degrades to a query-time join and idempotent
  re-ingest weakens.

## Consequences

- Every write carries one table lookup; the registry file gains three consumers (writer,
  query prompts, human consolidation) and must stay versioned with the code.
- `raw_relation` preserves extraction provenance; normalization never destroys what the
  model actually said.
- Inverse pairs must be curated honestly — a wrong pair silently flips direction at write
  time. Registry review joins the docs-PR loop.
- The existing graph converges via the one-off pass; second run is a full no-op.

## Measured results

To be filled at initiative close (PRD #76): distinct-type count before/after on the demo
graph, pass idempotency evidence, registry consolidation-loop turns, suite growth.
