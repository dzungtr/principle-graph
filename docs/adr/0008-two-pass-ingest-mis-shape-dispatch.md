# 0008 — Two-pass ingest and mis-shape dispatch: Jev promoted from admission gate to shaping dispatcher

Date: 2026-09-24 · Status: accepted · Spec: #95

The demo-notes ingest showed the pipeline controls *whether* claims enter (ADR-0006) but nothing controls *how knowledge is shaped*: 88% of 82 distinct relation verbs were used once; ~56 of 122 entities were surface variants of ~10 real actors (type drift — `politician` vs `person` — made same-type resolution structurally unable to merge); propositions materialized as clause-nodes ("France and Canada building infrastructure and trade architecture bypassing the United States"). One root cause: the extractor invents surface forms blind — no feedback of what already exists, no canonical vocabulary guidance, and only one shape for every kind of knowledge.

## Decision 1 — Two-pass ingest

Ingest becomes two-phase. A lightweight per-source **scan pass** inventories candidate verbs and entity mentions (batched, not per-chunk — it is inventory, not extraction). **Consolidation** anchors verbs against the relation registry and verbs live in the graph (new `list_relation_types()` store method), and entity mentions against graph entities via the existing resolution machinery — then one LLM clustering call over the unmatched remainder. The resulting **source verb menu** and **source entity roster** are injected into every extraction chunk prompt, so extraction reuses canonical forms instead of inventing variants. Scan-discovered verbs auto-append to a `proposed:` staging section of the registry; staging participates in matching immediately, and git is the review gate.

Defining stances, each grilled and confirmed by the owner:

- **Registries hold the system's language; knowledge lives in the graph.** Verbs, domains, entity types, and state keys are registry-canonicalized; entity identities and alias equivalences are *never* hardcoded in config — alias knowledge is learned per-source at scan time and accumulates as `aliases` on canonical entity nodes. (This is why no static EU↔Europe alias file exists, by explicit owner decision.)
- **Scan is inventory, not extraction** — no grounding discipline needed; it proposes, consolidation anchors.
- **Scan failure aborts the ingest** — fail-fast consistency with the novelty gate.
- **Keep-first re-ingest idempotency survives the two-phase shape.**

Considered: static prompt guidance without a scan pass — rejected: new domains would need registry edits before ingest works, and cross-source surface forms still fragment. Session-roster-only feedback (no scan) — rejected: guidance arrives too late, since a chunk only learns what earlier chunks introduced.

## Decision 2 — Mis-shape dispatch

Deterministic **guards classify** mis-shaped proposals into a fixed set of classes (`numeric_endpoint`, `qualitative_endpoint`, `clause`, `list`, `compound_actor`, `deictic`, `self_loop`, `description_in_name`) — classification only, never disposal, thresholds tunable. Classified candidates pass to the Jev decision model with the bare claim + evidence + **the original chunk** (grounding preserved: repairs generate from source text, not model memory) + a per-category menu of handling steps; Jev returns a typed step + executable payload; the pipeline executes it (`to_state`, `decompose`, `pairwise_joint_edge`, `to_source`, `normalize_name`, `missing_endpoint`, `pass_flagged`, `drop_noise`). Repaired candidates re-enter the stream *before* the novelty gate — shaping before admission; ADR-0006's epistemic gate is unchanged. This promotes Jev from admission classifier to **shaping dispatcher**. Well-formed triples bypass dispatch entirely (zero extra calls). Dispatcher unreachable ⇒ hard abort; invalid payload ⇒ rejected log + flag, never a bad write.

Supersessions recorded: the numeric-endpoint soft-flag stance survives only as the `pass_flagged` menu choice; the decompose-family hard-reject became the executed `decompose` step. The rejected log keeps only `drop_noise` verdicts and failures — nothing is silently lost.

Considered: hard-reject with repair hints (log-only) — rejected by the owner: mis-shapes carry recoverable knowledge and the decision model can route them; rejection hoards repair work on the human. Soft-flag everything for Mode-2 review — rejected: review becomes the repair bottleneck the pipeline should own.

## Consequences

- The glossary entry **Ingest** becomes the two-phase act (scan + consolidate, then extract → resolve → reduce → review → commit).
- Guards stay deterministic and dumb; all repair intelligence lives in one dispatch decision with typed outputs — auditable, replayable in database-free tests with recorded responses.
- Self-loops become diagnostics (missing endpoint vs reflexive claim), not silent garbage.
- Deictic references ("previous video") route to the Source provenance layer (ADR-0004), keeping entity space clean.
- The 2026-09-24 demo graph is deliberately not migrated; a fresh re-ingest under the new pipeline is the validation path (baseline metrics recorded in spec #95).
