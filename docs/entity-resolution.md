# Entity resolution contract

Status: reviewed prototype contract (2026-08-03)

Entity resolution runs before a candidate entity is committed to the permanent graph. It
returns one of **auto-resolve**, **create**, or **ambiguity queue**. The resolver never
silently changes the extracted spelling or relation; it records the candidate and the
selected canonical identity.

## Matching layers and thresholds

Checks run in this order, stopping when an outcome is decisive:

1. **Alias/name match (cheap):** normalize case, surrounding whitespace, and punctuation,
   then compare registered aliases and names. An exact normalized name/type match
   auto-resolves. A fuzzy name match is eligible to auto-resolve only at a similarity of
   `0.90` or higher. Below `0.90`, continue to the next layer.
2. **Semantic similarity:** compare the candidate embedding with existing entities of the
   same type using local `bge-m3` cosine similarity (ADR-0001). A score of `0.85` or higher is a semantic
   match; it auto-resolves only when the top candidate leads the second candidate by at
   least `0.05`. Otherwise, continue or queue as ambiguous. A score below `0.85` does not
   match.
3. **Structural corroboration:** inspect the candidate's already-resolved neighboring
   entities and directed relations in Neo4j. A candidate is corroborated when at least two
   expected neighbors and relation directions agree with the existing entity. Structural
   corroboration is a confirmation signal, not a standalone reason to merge an entity with
   no name or embedding match.

If no existing entity passes these checks, the outcome is **create**. New labels/types are
allowed (they remain review-visible), consistent with the extraction contract.

## Ambiguity and human review

Queue a candidate when multiple existing entities remain plausible, scores straddle a
threshold, or structural evidence conflicts with name/semantic evidence. The queue item
contains the original candidate, ranked matches with scores, structural evidence, and the
source/chunk reference. Mode 2 review surfaces the queue alongside the graph delta; no
ambiguous candidate is merged automatically.

**Amendment (ingest command v1, 2026-08-23):** queued candidates are rendered as review
notes and default to **create-new**; interactive canonical-entity selection at review is
deferred. The reviewer's escape hatch for a wrong queue note is rejecting the whole delta
and rerunning. The semantic layer compares embeddings from the local `bge-m3` model
(ADR-0001); thresholds are unchanged.

## Within-session policy

Chunks are processed sequentially. The scratch registry is authoritative for entities
introduced earlier in the current session: exact normalized matches and registered aliases
reuse that scratch identity without repeating an embedding lookup. Fuzzy or semantic
matches still use the same thresholds and are queued when ambiguous. Before commit, every
scratch identity is checked against the permanent graph using the full three-layer policy;
this prevents a chunk 10 alias from bypassing a graph collision discovered after chunk 2.
The scratch registry is disposable and is never persisted as a separate book-scoped graph.

## Prototype exclusions

Structural corroboration uses the cheap bounded neighbor check described above, not an
unbounded traversal. Confidence aggregation and repeat-extraction updates are specified
by issue #4. Resolution emits decisions for review; commit behavior belongs to later
pipeline slices.
