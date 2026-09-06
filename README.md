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

Query the graph for ranked reasoning directions:

```sh
pg query "interest rates are rising"
```

Use JSON output or adjust ranking limits:

```sh
pg query "interest rates are rising" --format json
pg query "interest rates are rising" --top-k 10 --max-edges-per-seed 20
```

Run `pg query --help` for all options. Queries require a reachable Neo4j instance
and data already committed to the graph.

## Ingest

`pg ingest <path>` runs the full pipeline (load source by extension, chunk,
sequential extraction, resolution, delta assembly, Mode-2 review, commit, stats)
in one blocking invocation:

```sh
pg ingest path/to/source.md
pg ingest path/to/source.pdf
```

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
| `APERTURE_BASE_URL` | `http://localhost:8000` | OpenAI-compatible extraction gateway |
| `LLM_MODEL` / `APERTURE_MODEL` | `z-ai/glm-5.2` | Extraction model id |
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

- [Demo acceptance walkthrough](docs/demo/acceptance-walkthrough.md)
- [Neo4j schema](docs/schema/neo4j-schema.md)
- [Extraction contract](docs/extraction-contract.md)
- [Fan-out query contract](docs/fanout-query.md)
