# Principle Graph

Long-term, structural knowledge memory for AI agents: typed, confidence-weighted
relationships between entities, accumulated across sources and compressed over time.
The graph's purpose is direction generation, not fact validation.

## Language

**Source**:
A Markdown or PDF knowledge document consumed into the graph by one ingestion run.
_Avoid_: Book, corpus, feed

**Ingest**:
The one-shot act of consuming a source end-to-end: chunk → extract → resolve → reduce → review → commit.
_Avoid_: Digest, import, learn

**Chunk**:
A section-scoped slice of a source, processed sequentially so later chunks can link back to earlier ones.

**Extraction event**:
One proposed relationship grounded in exactly one chunk, with its own confidence, evidence, scope conditions, and source reference.
_Avoid_: Fact, observation

**Entity**:
A typed node identified by name and type — the only identity in the graph.
_Avoid_: Concept, node, term

**Relationship**:
A typed, directed edge between two entities, identified by its endpoints plus relation type. A single relationship is updated in place as extractions repeat; it is never duplicated per source.
_Avoid_: Triple, link, fact

**Confidence**:
Accumulated support for a relationship, aggregated across independent extraction events. Not a truth score.
_Avoid_: Score, probability

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

**Fan-out**:
Querying the graph with new information to get ranked candidate reasoning directions.
_Avoid_: Search, lookup
