# Principle Graph

A local Python prototype for storing and querying evidence-backed reasoning in Neo4j.

## Requirements

- Python 3.12 or newer
- Docker Compose (or a compatible Compose implementation)
- Neo4j Community 5.x (provided by `docker-compose.yml`)

## Install

From the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

The package installs the `pg` command and its Neo4j and PyMuPDF dependencies.

## Start Neo4j

Create the local environment file used by Compose and the CLI:

```sh
cp .env.example .env
set -a; . ./.env; set +a
```

Start Neo4j Community:

```sh
docker compose up -d
```

Neo4j is exposed at `bolt://localhost:7687` and its browser at
`http://localhost:7474`. The defaults in `.env.example` are `neo4j` / `principlegraph`.
Override `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, or `NEO4J_DATABASE` in `.env`
as needed.

If using rootless Podman through its Docker-compatible socket, set
`DOCKER_HOST=unix://${XDG_RUNTIME_DIR}/podman/podman.sock` before running Compose.

Verify connectivity and apply the graph schema:

```sh
pg check
pg init
```

## Query

The old fan-out `pg query <text>` command (ranked reasoning directions) has
been replaced by read-only vector-search subcommands. Every match carries its
Neo4j `elementId`, so commands chain: search first, then show by elementId.

Search entities by embedding similarity (duplicates are expected when
same-named entities differ by type):

```sh
pg query entity "interest rates" 
pg query entity "interest rates" --top-k 10 --format json
```

Search evidence text across `:ExtractionEvent` and `:StateEvent` ledger rows;
 hits carry the entity pair they connect:

```sh
pg query event "rates rising"
pg query event "rates rising" --format json
```

Both commands require a reachable Neo4j instance, a reachable Ollama embedder,
and data already committed to the graph; the similarity threshold reuses
`PG_QUERY_SEED_SIMILARITY`.

Inspect one entity by its elementId — linked entities by default, or its state
rows / ledger rows with `--state` / `--event` (mutually exclusive):

```sh
pg entity show 4:abc:1
pg entity show 4:abc:1 --state
pg entity show 4:abc:1 --event
```

Run `pg query --help` and `pg entity show --help` for all options. JSON output
is structured on stdout; notices go to stderr.

## Reset

`pg reset` deletes every node and relationship in the graph. It is intentionally
coarse — there is no per-source undo — and only runs behind the explicit `--yes`
confirmation flag:

```sh
pg reset --yes
```

Without `--yes` it refuses and prints a reminder (exit 1). On success it prints
the deletion counters (`nodes_deleted`, `relationships_deleted`). Use it to
re-ingest a demo corpus from a clean graph without ad-hoc Cypher.

## Ingest

`pg ingest <path>` runs the full pipeline (load source by extension, chunk,
sequential extraction, resolution, delta assembly, Mode-2 review, commit, stats)
in one blocking invocation:

```sh
pg ingest path/to/source.md
pg ingest path/to/source.pdf
pg ingest path/to/notes/   # folder ingest
```

When `<path>` is a **directory**, every `*.md` / `*.markdown` file directly
inside it (non-recursive, sorted by filename) is ingested one at a time through
the same pipeline: a pre-flight runs once up front, then each file goes through
extraction, review, and commit with its own stats block, followed by an
aggregate summary line (e.g. `Ingested 3 files: OK 2, skipped 1`). An empty
directory fails fast with exit code 2 before any model spend; an unsupported
file inside the folder is skipped with a stderr notice and the rest continue.
Source references stay keyed by filename, so `keep-first` repeat mode works
across re-ingests of the same folder.

### Mode-2 review interaction

By default the final Graph delta review is **interactive**: the delta is printed
and the terminal prompts `approve, reject, or edit confidence`:

- **approve** (`a`) — commit the delta and print the end-of-run stats (exit 0)
- **reject** (`r`) — commit nothing; Rejected records with full provenance
  (candidate edge, source ref, evidence, scope conditions, reason, decision)
  are appended to `PG_REJECTED_LOG_PATH` and the command exits **4**
- **edit** (`e`) — set a new confidence (0.0–1.0) on the first edge and
  re-render the delta before deciding

For smoke runs and agents, pass `--yes` to skip the prompt and approve the
delta scripted:

```sh
pg ingest path/to/source.md --yes
```

If stdin closes before a decision (e.g. piped input without `--yes`), the run
fails with a reminder to use `--yes` rather than silently approving.

### Repeat-extraction behavior

By default a claim re-ingested from the same source reference is a **no-op**
(`keep-first`: the matched ledger row is never overwritten, so re-ingest can
never wobble the graph). To let a source deliberately refine its claim, opt
into `refresh`: the matched row's values are replaced and the arrow's
aggregate recomputes from all rows.

```sh
pg ingest path/to/source.md --repeat-mode refresh
```

Precedence: the `--repeat-mode` flag overrides the `PG_REPEAT_MODE` env var,
which overrides the default (`keep-first`). Invalid values fail fast with a
clear error (exit 2) before any pre-flight or graph write.

### End-of-run transcript

After the review decision, `pg ingest` prints the end-of-run stats transcript.
Beyond the basics (source, chunks, extraction/embedding request counts,
ambiguity notes, Mode-2 verdict, committed counts, elapsed seconds) it includes
the v2 pipeline stages:

- **Scan calls** — `scan calls: N (consolidation calls: M)` from the two-pass
  scan: N batched inventory calls plus one clustering consolidation call over
  the unmatched remainder. Omitted when the scan produced nothing or was not run.
- **Dispatch counts** — `dispatch: N calls, B bypassed, D dropped` with a
  per-step breakdown (`steps: decompose=2, ...`) and one `flagged:` note per
  candidate routed to review. `bypassed` are well-formed triples that skipped
  dispatch entirely; `dropped` were discarded as noise or invalid payloads.
  Omitted when the dispatcher is opted out (`--no-novelty-filter`, which skips
  both Jev seams).
- **State tracking** — `states: N committed; filtered states: ...` plus unknown
  state keys passed through uncanonicalized (never-reject stance).

### Source provenance

Every ledger row links to a `:Source` node — one per source id (the
`source_ref` prefix before the first colon, e.g. `book-1` in
`book-1:chapter-2/page-14`) — via a `FROM_SOURCE` edge (ADR-0004). New
ingestion writes the link automatically. Existing graphs backfill with:

```sh
pg backfill-sources
```

The pass is idempotent: a second run changes no state. The denormalized
`source_ref` string on rows is untouched — ledger identity depends on it — and
rows whose reference lacks a valid source id prefix are reported as errors
rather than linked.

To walk everything one source claimed (rows later contradicted included):

```sh
pg provenance book-1
```

### Environment

`pg ingest` reads the same `.env` settings as the rest of the CLI. The relevant
variables (all optional; defaults shown):

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_GATEWAY_URL` | `http://localhost:8000` | OpenAI-compatible extraction gateway |
| `LLM_MODEL` / `MODEL_GATEWAY_MODEL` | `z-ai/glm-5.3-flash` | Extraction model id |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local bge-m3 embeddings service |
| `OLLAMA_MODEL` | `bge-m3` | Embedding model id |
| `PG_REJECTED_LOG_PATH` | `.pg/rejected.jsonl` | Where rejected review records are appended |
| `PG_REPEAT_MODE` | `keep-first` | Repeat-extraction behavior (`keep-first` or `refresh`); overridden by `--repeat-mode` |

### Pre-flight and exit codes

Before any model spend, `pg ingest` checks that Neo4j is reachable and that
the uniqueness constraint plus vector index are present. Failures print the
remediation command on stderr and exit non-zero:

- **1** — pre-flight failed (`pg check` / `pg init` hint printed)
- **2** — source path missing, or an invalid `--repeat-mode` / `PG_REPEAT_MODE` value
- **3** — orchestrator error during the run
- **4** — Mode-2 review rejected the delta (nothing committed, JSONL log appended)
- **0** — approved and committed

Gateway and Ollama are intentionally NOT pre-flighted; their errors surface on
the first real call.

### Degraded mode (Ollama offline)

When Ollama is unreachable the embedder seam degrades gracefully: it warns once
and returns `None`, the semantic resolution layer is skipped, and the alias and
structural layers still run. Entities ingested without embeddings can be
re-embedded later once the service is back.

## Tests

Run the full suite from the repository root:

```sh
PYTHONPATH=src python -m pytest -q
```

Run the demo tests only:

```sh
PYTHONPATH=src python -m pytest tests/test_demo.py -v
```

## Further documentation

- [Neo4j schema](docs/schema/neo4j-schema.md)
- [Extraction contract](docs/extraction-contract.md)
- [Fan-out query contract](docs/fanout-query.md)
