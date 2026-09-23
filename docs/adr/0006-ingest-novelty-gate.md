# 0006 — Ingest novelty gate: decision-model filter, always-on with hard abort

Date: 2026-09-23 · Status: accepted · Spec: #93

The last real ingest run polluted the candidate stream with two modes: non-claims riding
along as proposals (noise), and textbook truisms an educated reader already holds
(common sense). Both commit as high-confidence arrows, giving fan-out queries a highway of
obvious directions — dead weight against the graph's mission of direction generation from
what was *learned*. The write boundary (ADR-0003) constrains *how* claims are spelled; nothing
yet constrains *whether a claim earns a place at all*.

## Decision

Decision: gate ingest **before entity resolution** with a novelty classifier — each unique
proposed relationship is rendered as a plain knowledge claim (`claim` + `evidence`, zero
product signature) and challenged by TypeSafe's Jev decision model
(`typesafe/jev-1.13`, OpenRouter Decisions API, `POST /api/alpha/decisions`), which returns a
typed three-way choice: `noise` / `common_sense` / `novel`. Noise and common-sense proposals
are dropped; only novel proposals enter resolve → assemble → review → commit.

The gate's defining stances, each grilled and confirmed by the owner:

- **Novelty is model knowledge, not graph state.** "Would an educated reader already hold this
  belief without reading the source?" Cross-source duplicates are *desirable* — they pass and
  feed confidence aggregation; the ledger already absorbs repeats as no-ops.
- **Placement is pre-resolution.** Dedup by casefolded raw triple first — one decision call per
  unique proposal; skipped items cost no embedding, resolution, or review work.
- **Dropped proposals leave no artifact.** Pure drop; only transcript-level aggregates
  (counts, mean probabilities) survive.
- **Failure is hard abort.** Filter enabled + Jev unreachable ⇒ ingest refuses to run;
  nothing commits. Unfiltered ingestion poisons the graph, so degrade is worse than stop —
  matching the fail-fast registry precedent.
- **Always-on, with escape hatch.** The default path is filtered; `pg ingest
  --no-novelty-filter` opts out. `OPENROUTER_API_KEY` thus becomes a required env for ingest.
- **Verdict is pure argmax.** No confidence thresholds in v1.

The criteria text (frozen in code, single source of truth in the spec) anchors all three
tiers on the reader, not the document; truth is deliberately outside the classification —
that is the fact-checker's job (ADR-0005), and novelty is orthogonal to it.

## Considered Options

- **Fail-open on Jev outage** (pass unfiltered + warn): rejected — converts an infra hiccup
  into silent graph pollution, the exact problem the gate exists to stop.
- **Fail-closed on outage** (skip unclassified): rejected — silent data loss of novel facts.
- **Opt-in flag, default off** (repeat-mode precedent): rejected — inconsistent with the hard
  abort stance; noise enters every ingest, not occasionally.
- **Filter log (`.pg/filtered.jsonl`)** for tuning feedback: rejected by owner — minimal
  machinery; criteria re-tuning happens by re-running a source and observing.
- **Graph-state novelty** ("already in the graph?"): rejected — that is dedup, already solved
  by ledger identity; the filter is epistemic.
- **Post-resolution / review-stage placement:** rejected — wastes embedding and resolution
  work on facts about to be dropped, or makes the human the deduper.
- **Confidence thresholds on the verdict:** rejected for v1 — untunable knobs with no
  feedback data (no log); revisit with measurements.

## Consequences

- `OPENROUTER_API_KEY` (prepaid credits) is a hard dependency of default `pg ingest`; the
  escape hatch keeps keyless environments (CI, fresh clones) ingesting when explicitly
  requested.
- Dropped proposals are unrecoverable without re-ingesting the source (no log) — accepted
  cost of the minimal-machinery stance.
- Jev is pinned to `typesafe/jev-1.13` (dated snapshots serve requests); the
  `~typesafe/jev-latest` alias is reported broken in one measured writeup while OpenRouter
  lists it — unresolved conflict, sidestepped by pinning. `PG_JEV_MODEL` overrides.
- The Decisions API is alpha; the transport is behind a configurable base URL
  (`PG_JEV_BASE_URL`) so a stable-endpoint cutover (`/api/v1/systemone` appears in newer
  docs) is config, not code.
- Per-decision cost is negligible (~$1.3–2.7e-05 measured, output unmetered); filter spend is
  dominated by extraction itself.

## Measured results

(pending live run — no live novelty-filtered ingest has occurred yet)

- Filter effect on the demo graph: **pending measurement** — proposals dropped per tier
  (noise / common_sense), effect on committed edge count, and spot-check of false drops.
- Suite growth: **pending this slice's merge**.