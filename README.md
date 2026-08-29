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

### Pre-flight and exit codes

Before any model spend, `pg ingest` checks that Neo4j is reachable and that
the uniqueness constraint plus vector index are present. Failures print the
remediation command on stderr and exit non-zero:

- **1** — pre-flight failed (`pg check` / `pg init` hint printed)
- **2** — source path missing
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
