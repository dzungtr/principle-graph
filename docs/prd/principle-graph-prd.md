# Principle Graph — Initial PRD

**Status:** v1.0 — MVP implemented and merged (2026-08-23)
**Owner:** Dzung Tran
**Date:** 2026-08-02

## 1. Summary

Principle Graph is a standalone, long-term knowledge memory layer for AI agents. Unlike vector-based document memory (semantic retrieval) or task memory (episodic, session-scoped), Principle Graph stores **typed, confidence-weighted relationships between entities** — durable, structural knowledge that persists and compresses over time, similar to how specific observations in physics get distilled into general laws.

Its primary purpose is not fact validation. It is **direction generation**: when an agent receives new information, it queries the graph for the entities and relationships connected to that information, and each meaningful connection becomes a candidate direction for a sub-agent to reason down. The graph replaces manual human framing ("think about inflation vs. the stock market") with an autonomous, structurally-grounded fan-out.

## 2. Problem Statement

Coding/reasoning agents that ingest new information (news, diffs, documents) currently reason in whatever direction they're explicitly pointed, or default to shallow, generic exploration. There is no persistent, structural memory of *how entities relate to each other* that the agent can consult to decide what's worth reasoning about — and no mechanism for compressing accumulated knowledge into durable principles the way human expertise does.

## 3. Position in the Memory Stack

| Tier | Type | Scope | Example |
|---|---|---|---|
| 1 | Vector/RAG memory | Document indexing, semantic retrieval | "What did this document say about X?" |
| 2 | Task memory | Episodic, scoped to a task/session | State tracking during active work |
| 3 | **Principle Graph** | Long-term, structural, semantic | "Interest rate hikes reduce inflation, assuming demand-side transmission" |

Principle Graph is deliberately **out of scope from KAPE** — a separate, standalone project.

## 4. Core Concepts

- **Entity** — a typed node (e.g. `economic_indicator`, `policy_action`).
- **Relationship** — a typed, directional edge between entities (e.g. `increases`, `causes`, `correlates_with`).
- **Confidence** — accumulated trust in a relationship, distinct from any single extraction event's confidence.
- **Scope conditions** — qualifiers that keep conditionally-true principles from being wrongly generalized (e.g. "assumes demand-side transmission").
- **Principle reduction** — periodic compression of multiple specific observations into a single higher-level principle, mirroring how raw observations become laws (physics, math). Prevents the graph from growing unboundedly — it should get smarter, not just bigger.

## 5. Key User Workflows

### 5.1 Learning (Ingestion)
Human submits a source (book, article, news, notes). An agent-driven pipeline reads it, proposes candidate entities/relationships, resolves them against the existing graph, and proposes principle reductions. Human reviews and commits.

### 5.2 Reasoning (Fan-out)
Agent receives new information (e.g. "interest rates rising"). It queries the graph for connected entities, and each edge (or cluster of edges) becomes a candidate reasoning branch for a sub-agent — e.g. US stock market, Asian stock market, Vietnamese economy, Japanese inflation — prioritized by confidence.

### 5.3 Conflict Resolution
When new information conflicts with an existing principle:
- **Low graph confidence** → treated as a learning opportunity; sub-agents investigate/argue different interpretations; human decides whether to adopt, question, or test further.
- **High graph confidence** → flagged to the human as likely false, with the specific principle it violates shown as evidence.

## 6. Ingestion Pipeline

One **book = one task**. Chunks within a task are processed **sequentially** (not parallelized) so later chunks can be linked back to concepts established earlier — enabling one unified graph rather than book-scoped silos.

**Stages:**
1. **Intake** — parse and chunk the source (chapter/section-based, not fixed-size).
2. **Working domain model** — a disposable, task-scoped scratch state (entity registry, pending edges, cross-reference index) that exists only for the duration of the ingestion session.
3. **Extraction** — agent proposes candidate triples per chunk via structured tool calls, constrained to only what's asserted/implied in that chunk (no outside knowledge).
4. **Entity resolution** — three-layer check against the permanent graph before committing any new node:
   - Alias/name match (cheap, e.g. fuzzy string match)
   - Semantic similarity (embeddings)
   - Structural corroboration (does the candidate connect to other nodes the way an existing node does — a graph traversal check)
   - Ambiguous cases queue for human confirmation rather than auto-merging.
5. **Principle reduction** — proposes collapsing multiple specific triples into a general principle, only where scope conditions genuinely match.
6. **Human review checkpoint** — three configurable modes:
   - Mode 1: Review each entity individually (max control, highest friction — best for new domains)
   - Mode 2: Review the assembled graph delta as a whole before commit (balanced)
   - Mode 3: No review, auto-commit (only for trusted/low-stakes sources)
7. **Commit** — approved deltas written to the graph with confidence scores. Rejected items are logged, not discarded, in case they're useful evidence later.

### Candidate Extraction Schema
```
propose_triple({
  subject: string,
  subject_type: string,
  relation: string,
  object: string,
  object_type: string,
  confidence: float,        // this extraction event's confidence
  evidence: string,         // paraphrased source snippet
  scope_conditions: string, // qualifiers that limit generalization
  source_ref: string        // chunk id / page
})
```

## 7. System Architecture

**Control plane** (orchestration/state):
- API — accepts document submissions, creates ingestion tasks
- Task queue — dispatches chunk-level work to workers
- State store — tracks task status and the working domain model per session
- Review gateway — surfaces stage-6 checkpoints and receives human decisions

**Data plane** (stateless, horizontally scalable workers):
- Chunking worker
- Extraction worker (LLM tool-use pass)
- Resolution worker (alias → semantic → structural checks against Neo4j)
- Reduction worker
- Commit worker (writes approved deltas to the graph)

Task status lifecycle: `queued → chunking → extracting → reducing → awaiting_review → committed / partially_committed`

## 8. Storage

**Neo4j** (property graph) is the leading candidate over relational triples, because:
- Relationships carry properties natively (`confidence`, `evidence`, `scope_conditions` live on the edge, not in a separate join table)
- Structural corroboration and reasoning fan-out are both traversal-heavy operations — Cypher is purpose-built for this; SQL/Postgres struggles at scale here

Example:
```cypher
(:Entity {name: "Interest Rate", type: "economic_indicator"})
  -[:INCREASES {
      confidence: 0.82,
      evidence: "...",
      scope_conditions: "assumes demand-side transmission",
      source_ref: "book_id:12,chunk:47",
      created_at: datetime()
    }]->
(:Entity {name: "Inflation", type: "economic_indicator"})
```

## 9. Tech Stack (Proposed)

| Layer | Candidate tech |
|---|---|
| Document parsing | PDF/text extraction (pymupdf/unstructured) |
| Extraction model | Claude (Sonnet/Opus via API) — reasoning-heavy, needs strong inference |
| Embeddings | Voyage AI or open-source (BGE) |
| Graph DB | Neo4j |
| Task queue | NATS JetStream or Postgres-backed queue |
| Scratch state | In-memory / Redis / SQLite (session-scoped, disposable) |
| Orchestration | Potentially prototyped as a Claude Code skill/subagent workflow before becoming a standalone service |

## 10. V1 Scope

- **Single-tenant**: one database per user. No multi-tenant, no cross-graph merge/conflict logic.
- **Single unified graph** per user — no book-scoped or source-scoped silos.
- Human-authored ingestion only (no fully automatic extraction without review, except optionally under Mode 3).
- Confidence model: TBD — scalar weight vs. evidence log (open question).

## 11. Open Questions

- Repeat extractions (same edge from multiple sources) — separate relationship instances (preserve all evidence, aggregate confidence) vs. update a single relationship in place?
- Confidence model: scalar weight (simple, fast) vs. evidence log per edge (richer, heavier to query)?
- Fan-out breadth control: how does the graph decide which edges are "worth" spawning a sub-agent for on a well-connected entity, to avoid excessive/expensive fan-out?
- Within-session entity resolution (chunk 2 vs. chunk 10 of the same book) — same three-layer rigor as against the permanent graph, or a cheaper heuristic given it's pre-commit?
- Rejected graph-delta items at Mode 2 review — discard outright, or log as "considered and rejected" evidence?

## 12. Explicitly Out of Scope (V1)

- Multi-tenant / shared graphs across users
- Fully autonomous ingestion without any human checkpoint (beyond opt-in Mode 3)
- Real-time/streaming ingestion (this is document/source-driven, not event-driven)
- Integration with KAPE or any other existing project
