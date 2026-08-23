"""Command-line entry point for Principle Graph."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fanout import query_directions, render_markdown
from .resolution import Entity

from .config import Settings

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
        seeds, directions = query_directions(text, graph, top_k=top_k, max_edges_per_seed=max_edges)
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
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.handler()


if __name__ == "__main__":
    raise SystemExit(main())
