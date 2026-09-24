# Principle Graph

Long-term, structural knowledge memory for AI agents: typed, confidence-weighted
relationships between entities, accumulated across sources and compressed over time.
The graph's purpose is direction generation, not fact validation.

## Language

**Source**:
A Markdown or PDF knowledge document consumed into the graph by one ingestion run.
_Avoid_: Book, corpus, feed

**Ingest**:
The two-phase act of consuming a source end-to-end: scan and consolidate (verb menu + entity roster), then chunk → extract → resolve → reduce → review → commit.
_Avoid_: Digest, import, learn, one-shot run

**Chunk**:
A section-scoped slice of a source, processed sequentially so later chunks can link back to earlier ones.

**Extraction event**:
One proposed relationship grounded in exactly one chunk, with its own confidence, evidence, scope conditions, and source reference.
_Avoid_: Fact, observation

**Novelty filter**:
A pre-resolution gate in ingest that challenges each unique proposed relationship against what an educated reader already knows, classifying it noise, common sense, or novel. Noise and common-sense proposals are dropped before entity resolution; only novel proposals enter the graph.
_Avoid_: Dedup, truth check, fact-checker

**Entity**:
A typed node identified by name and type — the only identity in the graph.
_Avoid_: Concept, node, term

**Relationship**:
A typed, directed edge between two entities, identified by its endpoints plus relation type. A single relationship is updated in place as extractions repeat; it is never duplicated per source.
_Avoid_: Triple, link, fact

**Canonical relation**:
The registry-normalized relation type actually written to the ledger identity and the arrow. Alias spellings and inverse directions collapse into it at the write boundary.
_Avoid_: Fixed enum, closed vocabulary

**Raw relation**:
The relation verb exactly as the extractor proposed it, preserved on the ledger row for provenance when normalization changes it.
_Avoid_: Original edge, dirty verb

**Label registry**:
A versioned data file mapping canonical labels to aliases, inverse pairs, and descriptions. One generic loader serves relations, domains, entity types, and state keys; unknown labels pass through flagged, never rejected. Registries hold the system's language only — never entity knowledge.
_Avoid_: Enum, allowlist, code constant, alias file for entities

**Confidence**:
Accumulated support for a relationship, aggregated across independent extraction events. Not a truth score.
_Avoid_: Score, probability

**State assertion**:
A numeric or qualitative measurement of exactly one entity, carried with full provenance. Stored as an append-only StateEvent ledger row plus a denormalized current state on the entity.
_Avoid_: Quantity node, attribute, fact

**Scan pass**:
The lightweight pre-extraction inventory of a source: candidate verbs and entity mentions, consolidated against registries and the live graph before any claim is extracted.
_Avoid_: Pre-pass, survey, digest

**Verb menu**:
The per-source consolidated relation vocabulary injected into extraction prompts.
_Avoid_: Verb list, vocabulary prompt

**Entity roster**:
The per-source canonical entity names and alias hints injected into extraction prompts.
_Avoid_: Entity list, seed list

**Mis-shape dispatch**:
The protocol where deterministic guards classify a mis-shaped proposal and a decision model picks the repair step from a per-category menu, which the pipeline then executes.
_Avoid_: Auto-fix, rejection, cleanup, sanitization

**Domain tag**:
An optional registry-normalized label on an extraction event naming the belief domain it belongs to (e.g. economics). Absent means explicitly untagged.
_Avoid_: Category, topic, folder

**Scope conditions**:
Qualifiers that keep a conditionally-true relationship from being wrongly generalized. Later extractions may add but never erase them.

**Ambiguity queue**:
Resolution candidates that remain plausible against multiple existing entities. Queued candidates are never merged automatically; in the ingest command they surface as review notes and default to create-new.
_Avoid_: Conflict, duplicate

**Graph delta**:
The complete proposed change set for one ingestion run — new entities, new edges, merges, and confidence changes — reviewed as a whole before anything commits.
_Avoid_: Batch, transaction

**Mode-2 review**:
The whole-delta human checkpoint before commit: approve, reject, or edit confidence.
_Avoid_: Approval, sign-off

**Rejected record**:
A delta item turned away at review, retained with full provenance in the local rejected log — never committed, never contributing confidence.
_Avoid_: Discard, failed extraction

**Verdict**:
An append-only, receipt-shaped record of a fact-check over one extraction event: support, refute, or unclear, with its own confidence, evidence URLs, and provenance. Verdicts never mutate rows or confidences; they inform human decisions.
_Avoid_: Status, rating, review score

**Fan-out**:
Querying the graph with new information to get ranked candidate reasoning directions.
_Avoid_: Search, lookup
