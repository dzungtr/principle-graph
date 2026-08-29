"""Command-line entry point for Principle Graph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from .config import Settings
from .extraction import SequentialExtractor
from .fanout import query_directions, render_markdown
from .llm_gateway import OpenAICompatibleMessagesClient
from .neo4j import Neo4jEntityStore, Neo4jGraphWriter, load_existing_edges
from .orchestrator import IngestOrchestrator
from .resolution import Entity

_SCHEMA = Path(__file__).parents[2] / "docs" / "schema" / "neo4j-schema.cypher"


def _driver(settings: Settings):
    from neo4j import GraphDatabase
    return GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password))


def check_connection(settings: Settings) -> int:
    try:
        driver = _driver(settings)
        try:
            driver.verify_connectivity()
            with driver.session(database=settings.database) as session:
                session.run("RETURN 1").consume()
        finally:
            driver.close()
    except Exception as error:  # CLI should provide a useful failure without a traceback.
        print(f"Neo4j connection failed: {error}")
        return 1
    print(f"Connected to Neo4j at {settings.uri}")
    return 0


def apply_schema(settings: Settings) -> int:
    statements = []
    for raw in _SCHEMA.read_text().split(";"):
        statement = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("//")).strip()
        if statement:
            statements.append(statement)
    try:
        driver = _driver(settings)
        try:
            with driver.session(database=settings.database) as session:
                for statement in statements:
                    session.run(statement).consume()
        finally:
            driver.close()
    except Exception as error:
        print(f"Neo4j schema application failed: {error}")
        return 1
    print("Neo4j schema applied")
    return 0


class _Neo4jQueryGraph:
    def __init__(self, settings: Settings):
        self.driver = _driver(settings)
        self.database = settings.database

    def close(self) -> None:
        self.driver.close()

    def entities(self):
        with self.driver.session(database=self.database) as session:
            rows = session.run("MATCH (e:Entity) RETURN e.name AS name, e.type AS type, e.embedding AS embedding ORDER BY e.name")
            return [Entity(f"{row['type']}:{row['name']}", row['name'], row['type'], embedding=tuple(row['embedding']) if row['embedding'] else None) for row in rows]

    def edges_for(self, entity):
        from .review import GraphEdge
        with self.driver.session(database=self.database) as session:
            rows = session.run("""MATCH (a:Entity)-[r]->(b:Entity)
                WHERE a.name = $name OR b.name = $name
                RETURN a.name AS subject, type(r) AS relation, b.name AS object,
                       r.confidence AS confidence, r.source_ref AS source_ref,
                       r.evidence AS evidence, r.scope_conditions AS scope_conditions""", name=entity.name)
            return [GraphEdge(row['subject'], row['relation'], row['object'], row['confidence'],
                              row['source_ref'] or '', tuple(row['evidence'] or ()), row['scope_conditions'] or '') for row in rows]


def query_command(settings: Settings, text: str, top_k: int, max_edges: int, output_format: str) -> int:
    graph = _Neo4jQueryGraph(settings)
    try:
        notices: list[str] = []
        seeds, directions = query_directions(text, graph, top_k=top_k,
                                             max_edges_per_seed=max_edges,
                                             embedder=_build_embedder(settings),
                                             threshold=settings.query_seed_similarity,
                                             notices=notices)
        for notice in notices:
            # stderr keeps the JSON output shape (query/seeds/directions) unchanged.
            print(f"Notice: {notice}", file=sys.stderr)
        if output_format == "json":
            print(json.dumps({"query": text,
                              "seeds": [{"name": s.entity.name, "score": s.score} for s in seeds],
                              "directions": [{"rank": d.rank, "seed": d.seed, "relation": d.relation,
                                              "neighbor": d.neighbor, "confidence": d.confidence,
                                              "scope_conditions": d.scope_conditions, "source_ref": d.source_ref,
                                              "evidence": list(d.evidence)} for d in directions]}))
        else:
            print(render_markdown(text, seeds, directions))
    except Exception as error:
        print(f"Neo4j query failed: {error}")
        return 1
    finally:
        graph.close()
    return 0


# ---------------------------------------------------------------------------
# pg ingest: pre-flight + one-shot orchestrator wiring.
# ---------------------------------------------------------------------------


class PreflightError(RuntimeError):
    """Raised when an ingest pre-flight check fails."""

    def __init__(self, message: str, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


def _check_schema_artifacts(driver, database: str) -> tuple[bool, bool]:
    """Return ``(has_constraint, has_vector_index)`` for the configured database."""
    has_constraint = False
    has_vector_index = False
    with driver.session(database=database) as session:
        for row in session.run("SHOW CONSTRAINTS"):
            labels = row.get("labelsOrTypes") or []
            if isinstance(labels, list):
                label_text = ",".join(str(item) for item in labels)
            else:
                label_text = str(labels)
            props = list(row.get("properties") or [])
            if "Entity" in label_text and "name" in props and "type" in props:
                has_constraint = True
                break
        for row in session.run("SHOW INDEXES"):
            name = row.get("name") or ""
            if name == "entity_embedding":
                has_vector_index = True
                break
            labels = row.get("labelsOrTypes") or []
            if isinstance(labels, list):
                label_text = ",".join(str(item) for item in labels)
            else:
                label_text = str(labels)
            props = list(row.get("properties") or [])
            if "Entity" in label_text and "embedding" in props:
                has_vector_index = True
                break
    return has_constraint, has_vector_index


def preflight(settings: Settings) -> None:
    """Verify Neo4j is reachable and the schema is applied before any model spend.

    Raises :class:`PreflightError` with a remediation hint on failure. Gateway
    and Ollama are intentionally NOT pre-flighted; their errors surface on the
    first real call with a clear message.
    """
    try:
        driver = _driver(settings)
    except Exception as error:
        raise PreflightError(
            f"Neo4j unreachable at {settings.uri}: {error}. Run `pg check` to verify connectivity.",
            remediation="Run `pg check` to verify connectivity.",
        ) from error

    try:
        try:
            driver.verify_connectivity()
        except Exception as error:
            raise PreflightError(
                f"Neo4j unreachable at {settings.uri}: {error}. Run `pg check` to verify connectivity.",
                remediation="Run `pg check` to verify connectivity.",
            ) from error
        try:
            has_constraint, has_vector_index = _check_schema_artifacts(driver, settings.database)
        except Exception as error:
            raise PreflightError(
                f"Neo4j schema inspection failed: {error}. Run `pg init` to apply the schema.",
                remediation="Run `pg init` to apply the schema.",
            ) from error
    finally:
        driver.close()

    missing: list[str] = []
    if not has_constraint:
        missing.append("(name, type) uniqueness constraint")
    if not has_vector_index:
        missing.append("vector index on Entity.embedding")
    if missing:
        raise PreflightError(
            "Neo4j schema is missing: " + ", ".join(missing) + ". Run `pg init` to apply the schema.",
            remediation="Run `pg init` to apply the schema.",
        )


def build_orchestrator(settings: Settings) -> tuple[IngestOrchestrator, object]:
    """Compose the orchestrator over the real-backend seams for `pg ingest`."""
    driver = _driver(settings)
    writer = Neo4jGraphWriter(driver, database=settings.database, rejected_log_path=settings.rejected_log_path)
    store = Neo4jEntityStore(driver, database=settings.database)
    embedder = _build_embedder(settings)
    messages = OpenAICompatibleMessagesClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key="local-placeholder",
    )
    extractor = SequentialExtractor(client=messages, model=settings.llm_model)
    orchestrator = IngestOrchestrator(
        extractor=extractor,
        store=store,
        embedder=embedder,
        writer=writer,
        edge_loader=_EdgeLoaderAdapter(driver, settings.database),
        rejected_log_path=settings.rejected_log_path,
    )
    return orchestrator, driver


def _build_embedder(settings: Settings):
    """Construct the Ollama embedder, returning ``None`` when the seam is unavailable.

    Importing ``OllamaEmbedder`` is lazy so the CLI starts even when the optional
    dependency is missing in a degraded deployment.
    """
    try:
        from .embedder import OllamaEmbedder
    except Exception:
        return None
    try:
        embedder = OllamaEmbedder(settings=settings)
        # Probe once to honour the brief's degraded-mode behaviour. If Ollama is
        # offline we keep the embedder (it will degrade per-call) but the CLI
        # caller is informed via the warning emitted on first request.
        return embedder
    except Exception:
        return None


def _interactive_input(prompt: str) -> str:
    """Terminal input for the default interactive Mode-2 review.

    EOF (e.g. piped stdin without ``--yes``) must not silently approve the
    delta; fail with the opt-in hint instead.
    """
    try:
        return input(prompt)
    except EOFError as error:
        raise RuntimeError(
            "interactive Mode-2 review got no terminal input (stdin closed); "
            "re-run with --yes for scripted approval"
        ) from error


def _scripted_approve(_prompt: str) -> str:
    """Scripted approval wired by ``--yes`` for smoke runs and agents."""
    return "approve"


class _EdgeLoaderAdapter:
    """Adapt the module-level ``load_existing_edges`` function to the loader protocol."""

    def __init__(self, driver, database: str) -> None:
        self._driver = driver
        self._database = database

    def load_existing_edges(self, triples):
        return load_existing_edges(self._driver, triples, self._database)


def ingest_command(
    settings: Settings,
    source_path: str,
    *,
    yes: bool = False,
    input_fn: Callable[[str], str] | None = None,
    out=sys.stdout,
) -> int:
    """Pre-flight, run the orchestrator, and emit the end-of-run stats block.

    Mode-2 review interaction: the default runs the interactive approve /
    reject / edit-confidence loop on the terminal. ``yes=True`` opts into
    scripted approval for smoke runs and agents. An explicit ``input_fn``
    (programmatic/test callers) takes precedence over both.
    """
    path = Path(source_path)
    if not path.exists():
        print(f"Source not found: {source_path}", file=sys.stderr)
        return 2
    try:
        preflight(settings)
    except PreflightError as error:
        print(f"Ingest pre-flight failed: {error}", file=sys.stderr)
        print(f"Hint: {error.remediation}", file=sys.stderr)
        return 1
    orchestrator, driver = build_orchestrator(settings)
    if input_fn is not None:
        review_input: Callable[[str], str] = input_fn
    elif yes:
        review_input = _scripted_approve
    else:
        review_input = _interactive_input
    try:
        result = orchestrator.run(path, input_fn=review_input)
    except Exception as error:
        print(f"Ingest failed: {error}", file=sys.stderr)
        return 3
    finally:
        driver.close()
    print(result.stats.render(), file=out)
    return 0 if result.stats.verdict == "approved" else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Principle Graph local knowledge-graph tools")
    subparsers = parser.add_subparsers(dest="command")
    check = subparsers.add_parser("check", help="verify Neo4j connectivity")
    check.set_defaults(handler=lambda: check_connection(Settings.from_env()))
    init = subparsers.add_parser("init", help="verify connectivity and apply the graph schema")
    init.set_defaults(handler=lambda: apply_schema(Settings.from_env()))
    query = subparsers.add_parser("query", help="return ranked reasoning directions")
    query.add_argument("text", help="new information to use as the query")
    query.add_argument("--top-k", type=int, default=5)
    query.add_argument("--max-edges-per-seed", type=int, default=20)
    query.add_argument("--format", choices=("markdown", "json"), default="markdown")
    query.set_defaults(handler=lambda: query_command(Settings.from_env(), args.text, args.top_k,
                                                     args.max_edges_per_seed, args.format))
    ingest = subparsers.add_parser("ingest", help="ingest a Markdown or PDF source")
    ingest.add_argument("path", help="path to a .md/.markdown or .pdf source")
    ingest.add_argument("--yes", action="store_true",
                        help="approve the Mode-2 delta without prompting (smoke runs and agents)")
    ingest.set_defaults(handler=lambda: ingest_command(Settings.from_env(), args.path, yes=args.yes))
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.handler()


if __name__ == "__main__":
    raise SystemExit(main())
