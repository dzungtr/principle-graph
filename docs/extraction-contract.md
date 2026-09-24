# Extraction contract

Status: validated for the prototype (contract v2)

This contract is the boundary between source chunking and graph/entity-resolution.
The extractor may propose claims only; it does not validate them or infer facts from
outside the supplied chunk. Contract v2 gives it exactly two tools: `propose_triple`
for relationships and `propose_state` for entity measurements.

## `propose_triple`

One tool call contains one directed candidate edge:

```json
{
  "subject": "interest rate hikes",
  "subject_type": "policy_action",
  "relation": "reduces",
  "object": "consumer borrowing",
  "object_type": "economic_behavior",
  "confidence": 0.84,
  "evidence": "The author says higher rates make borrowing more expensive.",
  "scope_conditions": "In the short run; for variable-rate borrowers.",
  "domain": "economics",
  "source_ref": "chapter-2/section-3"
}
```

All fields except `domain` are required. Strings are trimmed and must be non-empty.
`confidence` is an extraction-event confidence (not the graph edge's accumulated
confidence) and is inclusive in the range `[0, 1]`. `evidence` is a faithful, short
paraphrase—not a quote invented from memory. `source_ref` identifies the source, page
where available, and chunk (`book-1:chapter-2/section-3/page-14`). `domain` is an
optional topic tag (`snake_case` when present); omitting it leaves the row explicitly
untagged. Canonicalization against the domain registry happens at the write boundary,
not here.

## `propose_state`

One tool call contains one measurement of exactly one entity:

```json
{
  "entity": "European Central Bank",
  "entity_type": "organization",
  "state_key": "deposit_rate",
  "value": "3.5",
  "unit": "percent",
  "as_of": "2026-01",
  "confidence": 0.91,
  "evidence": "The ECB raised its deposit facility rate to 3.5 percent.",
  "scope_conditions": "",
  "source_ref": "book-1:chunk-7"
}
```

All fields are required; `unit` is an empty string for qualitative values. `value` may
be numeric or qualitative (`elevated`, `ample`) and is normalized to a string, so both
shapes share one schema — the unit carries the numeric dimension. Routing rule:
quantities and measurements route to `propose_state`, the entity carries the state,
and the measurement never becomes its own node or an edge endpoint.

### Labels: an extensible controlled vocabulary

`subject_type`, `object_type`, and `relation` (and `entity_type`, `state_key`) are
**normalized labels**, not a closed enum. They must be lowercase `snake_case`
(`^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$`). A strict enum would reject valid concepts in an
unfamiliar book; unrestricted prose would make resolution and querying unstable.
Canonicalization against the registries happens at the write boundary: alias spellings
collapse, unknown labels pass through flagged, never rejected. Resolution may merge
aliases; extraction must not silently rewrite the source claim.

## Shaping rules

The extraction system prompt enforces five shaping rules on every entity the model
names. They exist so mis-shaped prose never reaches resolution or the graph:

1. **Atomicity:** subject and object each name exactly one thing (a person,
   organization, country, place, event, policy, agreement, product, technology,
   market, or concept). Never use a clause, sentence, list, or possessive description
   as an entity; move such content into the relation, `scope_conditions`, or
   `evidence` instead.
2. **Canonical naming:** use the canonical form — name people by their full name
   ("Friedrich Merz", never "Merz" or "Germany's chancellor") and organizations by
   one standard short name ("Bank of Japan", never "BOJ" and "Bank of Japan" in the
   same run).
3. **Decomposition:** a proposition about several actors becomes one separate
   `propose_triple` per actor, all sharing the same evidence sentence from the chunk.
   Never collapse a multi-actor claim into a compound entity such as
   "France and Canada".
4. **Event-nodes are second-class:** if a subject-verb-object sentence carries the
   whole claim, emit it as the edge itself. Create an event entity only for events
   discussed as things ("EU-Canada summit", "November 10 truce expiry").
5. **Names stay bare:** move parenthetical qualifiers ("the ECB (European Central
   Bank)", "rates (2024)") into `scope_conditions` or `evidence` and keep the name
   itself clean.

## Two-pass scan and prompt injection

Before any extraction, the orchestrator runs a lightweight per-source scan pass. It
inventories candidate relation verbs and entity mentions over batched chunks with one
tool (`scan_candidates`), then consolidates them deterministically: relation-registry
exact/alias matches, live-graph verbs, and existing graph entities anchor first, and
one LLM clustering call runs over the unmatched remainder only (`consolidate_candidates`).

The output is a **verb menu** (the source's consolidated relation vocabulary) and an
**entity roster** (canonical names plus alias hints). Both are injected into every
extraction chunk prompt, after the chunk metadata block and before the chunk text:

```
Extract from this chunk.

source_ref: book-1:chunk-3
chunk_id: chunk-3
verb menu: cuts, raises, reduces
entity roster: European Central Bank (aliases: ECB); Federal Reserve

chunk text:
...
```

Scan-discovered new verbs auto-append to the relation registry's `proposed.labels`
staging section, where they participate in canonicalization immediately; promoting
them to `labels:` is a plain data-file edit reviewed through git. A scan failure
raises `ScanError` and aborts the ingest before any extraction or write — no partial
state.

## Mis-shape dispatch: guards, menu, execution

Because prompts do not guarantee shapes, a dispatch stage sits between extraction and
the novelty gate: deterministic guards classify, a decision model picks the repair,
and the pipeline executes. Guards never repair and never dispose — classification
only. The guard classes (pattern checks on endpoints, with tunable thresholds):

`numeric_endpoint`, `qualitative_endpoint`, `deictic`, `self_loop`,
`description_in_name`, `clause`, `list`, `compound_actor`.

Well-formed triples bypass dispatch entirely (zero extra calls). Each guard-flagged
candidate gets one decision call carrying the bare claim, the evidence, and the
original chunk text (repairs generate from source text, not model memory), plus the
handling menu for its class — for example `list` offers
`decompose_per_member`, `joint_with_scope`, `drop_noise`. The full class→menu table
lives in `src/principle_graph/dispatch.py` and is the authoritative reference.

The chosen step is a typed answer (step + payload) the executor materializes:

- repaired triples re-enter the candidate stream (sharing the original evidence);
- repaired states flow through the state path described below;
- `pass_flagged` / `to_source` surface in Mode-2 review visibility;
- `drop_noise` and payloads the executor cannot run land in the rejected log with an
  auditable verdict, never as a bad write.

A dispatcher outage raises `DispatchError` and hard-aborts the ingest (the novelty
gate's failure stance); it never silently degrades to unguarded writes.

## Extraction prompt contract

The extraction worker receives exactly one chunk, the chunk metadata, and (when the
scan produced them) the verb menu and entity roster. Its system instructions are:

1. Extract only relationships and measurements explicitly asserted or reasonably
   implied by this chunk.
2. Do not use general/world knowledge to fill missing subjects, objects, types, values,
   or causal direction. If a field cannot be grounded in the chunk, omit the candidate.
3. Preserve qualification in `scope_conditions`; do not turn conditional language into
   an unconditional claim.
4. Return zero or more `propose_triple` / `propose_state` calls and no prose claims
   outside tool calls. There is no cap on candidates per chunk.
5. Shape every entity by the five shaping rules above; route measurements to
   `propose_state`, never to a triple endpoint.
6. Use the chunk's source reference verbatim as `source_ref` — the caller rejects any
   call whose `source_ref` does not match the supplied chunk.
7. Set confidence for this extraction event only. Ambiguity lowers confidence; it does
   not justify guessing.
8. Include `domain` only when the chunk itself grounds the claim in a topic area.

The caller must reject malformed calls before resolution and retain the original chunk
for review. This is an enforceable structural/grounding boundary, not a guarantee that
the model never hallucinates: grounding is checked during review by comparing
`evidence` with the supplied chunk.

## State storage

Accepted state candidates follow the ledger two-layer precedent (ADR-0007): each is an
append-only `:StateEvent` row with identity `(entity, state_key, source_ref)`, enforced
by the write layer's pattern MERGE under the default `keep-first` mode. The entity is
matched on its canonical `(name, type)` identity and the `entity_type` is
registry-canonicalized at the write boundary, so alias-typed state candidates never
fragment the entity. State keys canonicalize against the state registry the same way
relations do: aliases collapse, unknown keys pass through flagged with an `unknown_key`
marker on the row.

On top of the ledger, the entity carries a denormalized `state` map — the current-state
read model, recomputed from the entity's full `:StateEvent` row set on every state
write: latest `as_of` wins, then confidence. The ledger rows are the source of truth;
disagreement rows remain queryable. States pass the same novelty gate as claims,
rendered as plain claims and deduped by `(entity, state_key, value)`; the ledger write
happens only on an approved review, keeping the triple pipeline's commit semantics.

## Chunking acceptance checks

Chunking is semantic and deterministic:

- Markdown: split at the heading hierarchy (`#` through `######`), retaining heading
  text with its following content. Blank-line paragraphs remain together; a paragraph
  is not split merely to meet a size target.
- PDF: split at detected chapter/section headings and paragraph boundaries, retaining
  page numbers. A paragraph crossing a page break remains one chunk with a page range.
- A chunk has one stable ID, non-empty text, and source metadata. IDs are ordered in
  source order and are unique within a source.
- Lists, block quotes, code blocks, and tables stay intact where the parser can detect
  them. If a single block is too large, split only at a block boundary and mark the
  resulting chunks with the same section path and an ordinal.
- Chunking must be lossless: joining chunk text in source order reproduces the normalized
  source text (aside from parser whitespace normalization).

The following representative Markdown and PDF-shaped fixtures, plus contract checks,
are kept in `tests/test_extraction_contract.py`. They cover heading/paragraph and page
boundary behavior and reject labels, confidence, and evidence violations. Dispatch
guard/menu behavior is covered in `tests/test_dispatch.py`; the scan pass in
`tests/test_scan.py`.

## Validation outcome

A rough pass against a representative chapter-shaped Markdown fixture and a PDF
page-boundary fixture confirmed that section-path/page metadata is sufficient for
`source_ref`, and that paragraph-sized chunks provide enough context without fixed-size
splits. The prototype therefore uses the rules above. The no-outside-knowledge rule is
prompt-enforced, guard-dispatched, and review-checked; it is intentionally not
represented as a claim that can be proven by schema validation alone.
