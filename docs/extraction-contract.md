# Extraction contract

Status: validated for the prototype (2026-08-03)

This contract is the boundary between source chunking and graph/entity-resolution.
The extractor may propose claims only; it does not validate them or infer facts from
outside the supplied chunk.

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
  "source_ref": "chapter-2/section-3"
}
```

All fields are required. Strings are trimmed and must be non-empty. `confidence` is
an extraction-event confidence (not the graph edge's accumulated confidence) and is
inclusive in the range `[0, 1]`. `evidence` is a faithful, short paraphrase—not a
quote invented from memory. `source_ref` identifies the source, page where available,
and chunk (`book-1:chapter-2/section-3/page-14`).

### Labels: an extensible controlled vocabulary

`subject_type`, `object_type`, and `relation` are **normalized labels**, not a closed
enum. They must be lowercase `snake_case` (`^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$`). A
strict enum would reject valid concepts in an unfamiliar book; unrestricted prose
would make resolution and querying unstable. The implementation may maintain a
registry of preferred labels and aliases, but an unseen normalized label is accepted
and flagged for review rather than rejected. Resolution may merge aliases; extraction
must not silently rewrite the source claim.

## Extraction prompt contract

The extraction worker receives exactly one chunk and the chunk metadata. Its system
instructions are:

1. Extract only relationships explicitly asserted or reasonably implied by this chunk.
2. Do not use general/world knowledge to fill missing subjects, objects, types, or
   causal direction. If a field cannot be grounded in the chunk, omit the candidate.
3. Preserve qualification in `scope_conditions`; do not turn conditional language into
   an unconditional claim.
4. Return zero or more `propose_triple` calls and no prose claims outside tool calls.
5. Use the chunk's source reference verbatim as `source_ref`.
6. Set confidence for this extraction event only. Ambiguity lowers confidence; it does
   not justify guessing.

The caller must reject malformed calls before resolution and retain the original chunk
for review. This is an enforceable structural/grounding boundary, not a guarantee that
the model never hallucinates: grounding is checked during review by comparing
`evidence` with the supplied chunk.

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
boundary behavior and reject labels, confidence, and evidence violations.

## Validation outcome

A rough pass against a representative chapter-shaped Markdown fixture and a PDF
page-boundary fixture confirmed that section-path/page metadata is sufficient for
`source_ref`, and that paragraph-sized chunks provide enough context without fixed-size
splits. The prototype therefore uses the rules above. The no-outside-knowledge rule is
prompt-enforced and review-checked; it is intentionally not represented as a claim that
can be proven by schema validation alone.
