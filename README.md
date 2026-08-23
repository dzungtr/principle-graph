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

## Ingestion and demo status

The acceptance walkthrough documents the intended end-to-end demo:

```sh
# See docs/demo/acceptance-walkthrough.md for the acceptance procedure.
pg ingest docs/demo/demo-source.md
```

The current `pg` CLI registers only `check`, `init`, and `query`; `pg ingest` is
not yet an available subcommand. The ingestion pipeline is covered by the test
suite, but the interactive ingest command has not been exposed by the CLI.

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
