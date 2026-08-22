"""Command-line entry point for Principle Graph."""
from __future__ import annotations

import argparse
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Principle Graph local knowledge-graph tools")
    subparsers = parser.add_subparsers(dest="command")
    check = subparsers.add_parser("check", help="verify Neo4j connectivity")
    check.set_defaults(handler=lambda: check_connection(Settings.from_env()))
    init = subparsers.add_parser("init", help="verify connectivity and apply the graph schema")
    init.set_defaults(handler=lambda: apply_schema(Settings.from_env()))
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.handler()


if __name__ == "__main__":
    raise SystemExit(main())
