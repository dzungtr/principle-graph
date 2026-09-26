# Fan-out query contract

Status: the `pg query <text>` fan-out CLI command was removed (2026-11) and
split into read-only vector-search subcommands (`pg query entity`,
`pg query event`) plus `pg entity show`. The library contract below
(`query_directions`, `render_markdown`, seed matching) is unchanged and is
still used by the demo pipeline; it is no longer reachable through the CLI.

## Input and seed matching

`pg query "<new information>"` accepts free-form text. The query is embedded with the
same model used for entity resolution, then matched against permanent graph entities
by embedding similarity. Exact normalized name and aliases matches are included first
(score 1.0); semantic matches use the query-seeding threshold, which is separate from
and lower than the entity-resolution threshold of `0.85`, because sentence-to-name
cosine similarity tops out well below it (measured ≈0.72 on the demo graph). The
default query-seeding threshold is `0.60`, configurable via the
`PG_QUERY_SEED_SIMILARITY` environment variable (values outside (0, 1] fall back to
the default). Only the best matching seed entities
are used, and their match score is retained for explainability. A query with no matching seeds
returns an empty result with a clear `no matching seeds` message.

When the Ollama embedder is unreachable, the command degrades to exact-name matching
only, and states the degradation explicitly via a stderr notice (the JSON output shape
is unaffected).

## Candidate directions

For each matched seed, inspect its directly connected, committed relationships only. Each
outgoing or incoming edge becomes a candidate direction: the direction includes the seed,
the relationship, the neighboring entity, the edge confidence, and its `scope conditions`.
Rejected review deltas and unresolved ambiguity-queue entries are excluded. Candidate
provenance includes the edge source reference and evidence so a downstream agent can explain
why the direction was selected.

Candidates are deduplicated by `(seed, relationship, neighboring entity)`; repeated edges
remain one candidate because repeat extraction updates one relationship in place. Rank in
descending order by edge confidence, with seed match score as the first tie-breaker and a
stable canonical entity identifier as the final tie-breaker.

## Breadth control and top-K

The command applies a configurable `top-K` limit, defaulting to 5 candidates. It also applies
a per-seed breadth limit of 20 edges before global ranking, preventing a well-connected entity
from consuming the entire fan-out. `--top-k K` and `--max-edges-per-seed N` override these
positive defaults; invalid or non-positive values are rejected. A candidate is never returned
more than once, and fewer than K results are returned when fewer eligible edges exist.

## Output

The default output is Markdown: a numbered ranked list where every item contains the candidate
direction, confidence, scope conditions, and source reference. `--format json` returns an
object with `query`, `seeds`, and `directions` arrays. Each direction object has `rank`,
`seed`, `relation`, `neighbor`, `confidence`, `scope_conditions`, `source_ref`, and `evidence`.
The JSON shape is stable and suitable for an agent; Markdown is intended for interactive use.
