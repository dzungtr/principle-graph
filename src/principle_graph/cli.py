"""Command-line entry point for Principle Graph."""
from __future__ import annotations

import argparse
import importlib.resources
import inspect
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .extraction import SequentialExtractor
from .factcheck import fact_check_notice, fact_check_rows
from .label_registry import (
    default_entity_registry_path,
    default_registry_path,
    ensure_working_registry,
    load_label_registry,
    resolve_relation_registry_path,
)
from .ledger import resolve_repeat_mode
from .llm_gateway import OpenAICompatibleMessagesClient
from .neo4j import Neo4jEntityStore, Neo4jGraphWriter, load_existing_edges
from .novelty import NoveltyFilter
from .orchestrator import IngestOrchestrator
from .scan import SourceScanner

# Schema DDL ships as package data so it resolves in any install layout
# (repo checkout, wheel, or the nix-built application in /nix/store).
_SCHEMA = importlib.resources.files(__package__).joinpath("data", "neo4j-schema.cypher")


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
    """Read-only Cypher runner over the live graph for the query/entity commands.

    Replaces the removed ``pg query <text>`` fan-out command: those commands
    now run directly against the vector indexes and ledger wiring, and every
    lookup/output uses ``elementId()`` so results can be chained between
    commands.
    """

    def __init__(self, settings: Settings):
        self.driver = _driver(settings)
        self.database = settings.database

    def close(self) -> None:
        self.driver.close()

    def rows(self, cypher: str, **params):
        with self.driver.session(database=self.database) as session:
            return [dict(record) for record in session.run(cypher, **params)]


# Vector search fetches a wider candidate pool than the requested top-k so a
# similarity-threshold filter can be applied before the final LIMIT.
_VECTOR_SEARCH_FETCH_LIMIT = 100

# The event search spans the :ExtractionEvent evidence vector index
# (ADR-0001); StateEvent rows are reached via `pg entity show --state`.
_EVIDENCE_VECTOR_INDEXES = (
    "extraction_evidence_embedding",
)


def _validated_top_k(top_k: int) -> int | None:
    if top_k <= 0:
        print("--top-k must be a positive integer", file=sys.stderr)
        return None
    return top_k


def _query_embedding(settings: Settings, term: str) -> list[float] | None:
    """Embed a query term via the shared embedder seam; ``None`` when unavailable."""
    embedder = _build_embedder(settings)
    vector = embedder.embed(term) if embedder is not None else None
    return list(vector) if vector is not None else None


def _similarity_rows(graph: _Neo4jQueryGraph, index: str, embedding, limit: int):
    return graph.rows(
        "CALL db.index.vector.queryNodes($index, $limit, $embedding) "
        "YIELD node, score "
        "RETURN elementId(node) AS element_id, labels(node)[0] AS label, "
        "node.name AS name, node.type AS type, "
        "node.relation AS relation, node.evidence AS evidence, "
        "node.source_ref AS source_ref, node.confidence AS confidence, score "
        "ORDER BY score DESC",
        index=index, limit=limit, embedding=embedding,
    )


def _render_search_results_markdown(header: str, results: list[dict]) -> None:
    print(header)
    if not results:
        print("No matches above the similarity threshold.")
        return
    for row in results:
        if row.get("name") is not None:
            print(f"- {row['element_id']}  {row['name']} ({row.get('type') or ''}) "
                  f"score={float(row['score']):.4f}")
        else:
            print(f"- {row['element_id']}  [{row['label']}] relation={row.get('relation') or ''} "
                  f"score={float(row['score']):.4f}")
            print(f"    evidence: {row.get('evidence') or ''}")
            print(f"    source_ref: {row.get('source_ref') or ''} "
                  f"confidence={float(row.get('confidence') or 0.0):.2f}")
            for role in ("subject", "object"):
                node = row.get(role)
                if node:
                    edge = node.get("edge") or ""
                    print(f"    {role}: {node['element_id']}  {node['name']} "
                          f"({node.get('type') or ''}) via {edge}")


def query_entity_command(settings: Settings, term: str, top_k: int, output_format: str) -> int:
    """Vector-similarity search over :Entity.embedding (replaces fan-out seeding)."""
    if _validated_top_k(top_k) is None:
        return 2
    graph = _Neo4jQueryGraph(settings)
    try:
        embedding = _query_embedding(settings, term)
        if embedding is None:
            print("Embedder unavailable; cannot embed the query term.", file=sys.stderr)
            return 1
        rows = _similarity_rows(graph, "entity_embedding", embedding,
                                _VECTOR_SEARCH_FETCH_LIMIT)
        results = [row for row in rows if float(row["score"]) >= settings.query_seed_similarity][:top_k]
        for row in results:
            row.pop("label", None)
            row.pop("relation", None)
            row.pop("evidence", None)
            row.pop("source_ref", None)
            row.pop("confidence", None)
        if output_format == "json":
            print(json.dumps({"query": term, "results": results}))
        else:
            _render_search_results_markdown(f"Entities matching: {term}", results)
    except Exception as error:
        print(f"Neo4j query failed: {error}", file=sys.stderr)
        return 1
    finally:
        graph.close()
    return 0


_EVENT_NEIGHBOR_CLAUSE = (
    "OPTIONAL MATCH (subject:Entity)-[subject_edge]->(node) "
    "OPTIONAL MATCH (node)-[object_edge]->(object:Entity) "
)


def _event_search_rows(graph: _Neo4jQueryGraph, embedding, limit: int) -> list[dict]:
    """Search the ExtractionEvent evidence index; hits carry entity wiring."""
    merged: list[dict] = []
    for index in _EVIDENCE_VECTOR_INDEXES:
        merged.extend(graph.rows(
            "CALL db.index.vector.queryNodes($index, $limit, $embedding) "
            "YIELD node, score "
            "WITH node, score, labels(node)[0] AS label "
            + _EVENT_NEIGHBOR_CLAUSE +
            "RETURN elementId(node) AS element_id, label, "
            "coalesce(node.relation, '') AS relation, "
            "coalesce(node.evidence, '') AS evidence, "
            "coalesce(node.source_ref, '') AS source_ref, "
            "coalesce(node.confidence, 0.0) AS confidence, score, "
            "elementId(subject) AS subject_id, subject.name AS subject_name, "
            "subject.type AS subject_type, type(subject_edge) AS subject_edge, "
            "elementId(object) AS object_id, object.name AS object_name, "
            "object.type AS object_type, type(object_edge) AS object_edge",
            index=index, limit=limit, embedding=embedding,
        ))
    return merged


def _event_result(row: dict) -> dict:
    subject = ({"element_id": row["subject_id"], "name": row["subject_name"],
                "type": row["subject_type"], "edge": row.get("subject_edge") or ""}
               if row.get("subject_id") else None)
    object_node = ({"element_id": row["object_id"], "name": row["object_name"],
                    "type": row["object_type"], "edge": row.get("object_edge") or ""}
                   if row.get("object_id") else None)
    return {
        "element_id": row["element_id"],
        "label": row["label"],
        "relation": row["relation"],
        "evidence": row["evidence"],
        "source_ref": row["source_ref"],
        "confidence": float(row["confidence"]),
        "score": float(row["score"]),
        "subject": subject,
        "object": object_node,
    }


def query_event_command(settings: Settings, term: str, top_k: int, output_format: str) -> int:
    """Vector-similarity search over ExtractionEvent evidence embeddings."""
    if _validated_top_k(top_k) is None:
        return 2
    graph = _Neo4jQueryGraph(settings)
    try:
        embedding = _query_embedding(settings, term)
        if embedding is None:
            print("Embedder unavailable; cannot embed the query term.", file=sys.stderr)
            return 1
        rows = _event_search_rows(graph, embedding, _VECTOR_SEARCH_FETCH_LIMIT)
        results = [_event_result(row) for row in rows
                   if float(row["score"]) >= settings.query_seed_similarity]
        results.sort(key=lambda r: -r["score"])
        results = results[:top_k]
        if output_format == "json":
            print(json.dumps({"query": term, "results": results}))
        else:
            _render_search_results_markdown(f"Events matching: {term}", results)
    except Exception as error:
        print(f"Neo4j query failed: {error}", file=sys.stderr)
        return 1
    finally:
        graph.close()
    return 0


def _entity_show_results(graph: _Neo4jQueryGraph, element_id: str, mode: str) -> list[dict]:
    """Read one entity's links by mode: entities, states, or events."""
    if mode == "states":
        query = (
            "MATCH (e:Entity) WHERE elementId(e) = $eid "
            "MATCH (e)-[:HAS_STATE_EVENT]->(s:StateEvent) "
            "RETURN elementId(s) AS element_id, s.state_key AS state_key, "
            "coalesce(s.value, '') AS value, coalesce(s.unit, '') AS unit, "
            "coalesce(s.as_of, '') AS as_of, coalesce(s.confidence, 0.0) AS confidence, "
            "coalesce(s.source_ref, '') AS source_ref "
            "ORDER BY state_key"
        )
    elif mode == "events":
        query = (
            "MATCH (e:Entity) WHERE elementId(e) = $eid "
            "MATCH (e)-[r]-(ev) WHERE ev:ExtractionEvent OR ev:StateEvent "
            "RETURN DISTINCT elementId(ev) AS element_id, labels(ev)[0] AS label, "
            "type(r) AS relation, coalesce(ev.raw_relation, '') AS raw_relation, "
            "coalesce(ev.evidence, '') AS evidence, "
            "coalesce(ev.source_ref, '') AS source_ref, "
            "coalesce(ev.confidence, 0.0) AS confidence "
            "ORDER BY relation, source_ref"
        )
    else:
        query = (
            "MATCH (e:Entity) WHERE elementId(e) = $eid "
            "MATCH (e)-[r]-(n:Entity) "
            "RETURN DISTINCT type(r) AS relation, "
            "CASE WHEN startNode(r) = e THEN 'outgoing' ELSE 'incoming' END AS direction, "
            "elementId(n) AS element_id, n.name AS name, n.type AS type "
            "ORDER BY relation, direction, name"
        )
    return graph.rows(query, eid=element_id)


def entity_show_command(settings: Settings, element_id: str, state: bool, event: bool,
                        output_format: str) -> int:
    """List an entity's linked entities, state rows, or ledger events by elementId."""
    mode = "states" if state else "events" if event else "entities"
    graph = _Neo4jQueryGraph(settings)
    try:
        if not graph.rows("MATCH (e:Entity) WHERE elementId(e) = $eid "
                          "RETURN elementId(e) AS element_id", eid=element_id):
            print(f"Entity not found: {element_id}", file=sys.stderr)
            return 1
        results = _entity_show_results(graph, element_id, mode)
    except Exception as error:
        print(f"Neo4j query failed: {error}", file=sys.stderr)
        return 1
    finally:
        graph.close()
    if output_format == "json":
        print(json.dumps({"element_id": element_id, "mode": mode, "results": results}))
    else:
        print(f"Entity {element_id} — {mode}")
        if not results:
            print("No linked rows.")
        for row in results:
            if mode == "entities":
                print(f"- {row['direction']} {row['relation']}  {row['element_id']}  "
                      f"{row['name']} ({row['type']})")
            elif mode == "states":
                print(f"- {row['element_id']}  {row['state_key']}={row['value']}"
                      f"{(' ' + row['unit']) if row['unit'] else ''} "
                      f"as_of={row['as_of']} confidence={float(row['confidence']):.2f} "
                      f"source_ref={row['source_ref']}")
            else:
                print(f"- {row['element_id']}  [{row['label']}] {row['relation']} "
                      f"(raw: {row['raw_relation'] or 'n/a'}) "
                      f"confidence={float(row['confidence']):.2f} source_ref={row['source_ref']}")
                print(f"    evidence: {row['evidence']}")
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


def build_orchestrator(settings: Settings, repeat_mode: str | None = None,
                       novelty_filter: "NoveltyFilter | None" = None) -> tuple[IngestOrchestrator, object]:
    """Compose the orchestrator over the real-backend seams for `pg ingest`.

    ``repeat_mode`` overrides ``settings.repeat_mode`` when given; the effective
    value lands on the writer, which validates it before any session opens.
    """
    driver = _driver(settings)
    entity_registry = load_label_registry(default_entity_registry_path())
    writer = Neo4jGraphWriter(driver, database=settings.database,
                              rejected_log_path=settings.rejected_log_path,
                              repeat_mode=repeat_mode if repeat_mode is not None
                              else settings.repeat_mode,
                              entity_registry=entity_registry)
    store = Neo4jEntityStore(driver, database=settings.database)
    # Entity-type registry (issues #96/#99): canonicalization before matching
    # in the resolver and at the entity write boundary share one loaded copy.
    embedder = _build_embedder(settings)
    writer.evidence_embedder = embedder
    messages = OpenAICompatibleMessagesClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key="local-placeholder",
    )
    extractor = SequentialExtractor(client=messages, model=settings.llm_model)
    # Issue #102 two-pass scan: one scanner over the same LLM seam, the
    # relation registry (its staging section is the auto-append target), the
    # entity-type registry, and the live-graph store seam. The staging write
    # target is the working registry under .pg/ — seeded from the packaged
    # file, so ingestion never dirties a git-tracked file (issue #96).
    working_registry = ensure_working_registry()
    scanner = SourceScanner(
        client=messages,
        relation_registry=load_label_registry(working_registry),
        store=store,
        embedder=embedder,
        entity_registry=entity_registry,
        registry_path=working_registry,
        model=settings.llm_model,
    )
    orchestrator = IngestOrchestrator(
        extractor=extractor,
        store=store,
        embedder=embedder,
        writer=writer,
        edge_loader=_EdgeLoaderAdapter(driver, settings.database),
        rejected_log_path=settings.rejected_log_path,
        novelty_filter=novelty_filter,
        entity_registry=entity_registry,
        dispatcher=_dispatcher_for(settings, no_novelty_filter=False),
        scanner=scanner,
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


def reset_command(settings: Settings, yes: bool, out=sys.stdout) -> int:
    """Delete every node and relationship in the graph (issue #97).

    Destructive and intentionally coarse: `pg reset` clears the whole graph so
    re-ingesting a demo corpus needs no ad-hoc Cypher. It only ever runs
    behind the explicit ``--yes`` confirmation flag.
    """
    if not yes:
        print(
            "Refusing to reset: this deletes every node and relationship. "
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 1
    try:
        driver = _driver(settings)
        try:
            with driver.session(database=settings.database) as session:
                summary = session.run("MATCH (n) DETACH DELETE n").consume()
                counters = summary.counters
        finally:
            driver.close()
    except Exception as error:  # CLI should provide a useful failure without a traceback.
        print(f"Graph reset failed: {error}", file=sys.stderr)
        return 1
    print(
        "Graph reset complete: "
        f"nodes_deleted={counters.nodes_deleted}, "
        f"relationships_deleted={counters.relationships_deleted}",
        file=out,
    )
    return 0


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


def _novelty_filter_for(settings: Settings, no_novelty_filter: bool):
    """Construct the Jev client for the default always-on gate; ``None`` when opted out."""
    if no_novelty_filter:
        return None
    from .novelty import JevDecisionsClient
    return JevDecisionsClient(
        base_url=settings.jev_base_url, model=settings.jev_model,
        api_key=os.getenv("OPENROUTER_API_KEY", ""), timeout=settings.jev_timeout,
    )


def _dispatcher_for(settings: Settings, no_novelty_filter: bool):
    """Construct the mis-shape dispatch client (issue #101); ``None`` when opted out.

    Shares the novelty gate's key and opt-out: both are OpenRouter Decisions
    calls over the same credential, so one flag skips both Jev seams.
    """
    if no_novelty_filter:
        return None
    from .dispatch import JevDispatchClient
    return JevDispatchClient(
        # getattr fallbacks keep minimal Settings stubs in older tests working.
        base_url=getattr(settings, "jev_base_url",
                         "https://openrouter.ai/api/alpha/decisions"),
        model=getattr(settings, "jev_model", "typesafe/jev-1.13"),
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        timeout=getattr(settings, "jev_timeout", 10.0),
    )


def ingest_command(
    settings: Settings,
    source_path: str,
    *,
    yes: bool = False,
    input_fn: Callable[[str], str] | None = None,
    out=sys.stdout,
    repeat_mode: str | None = None,
    no_novelty_filter: bool = False,
) -> int:
    """Pre-flight, run the orchestrator, and emit the end-of-run stats block.

    Mode-2 review interaction: the default runs the interactive approve /
    reject / edit-confidence loop on the terminal. ``yes=True`` opts into
    scripted approval for smoke runs and agents. An explicit ``input_fn``
    (programmatic/test callers) takes precedence over both.

    Repeat-extraction behavior (issue #59): ``repeat_mode`` (the
    ``--repeat-mode`` flag) takes precedence over the ``PG_REPEAT_MODE`` env var
    (already resolved into ``settings``); the default is ``keep-first``. Invalid
    values fail fast with exit code 2 before any pre-flight, model spend, or
    graph write.

    Folder ingest: when ``source_path`` is a directory, every ``*.md`` /
    ``*.markdown`` file inside it (non-recursive, sorted by filename for
    determinism) is ingested through the same orchestrator flow per file, so
    pre-flight, the novelty-gate key check, and the review loop behave exactly
    as a single-file ingest. Source_id stays the filename, so keep-first repeat
    mode still keys per file across re-ingests of the same folder. An empty
    directory fails fast with exit code 2 before any pre-flight; an unsupported
    file inside the folder is skipped with a stderr notice and the rest
    continue. A per-file stats block is emitted, followed by an aggregate
    summary line for folder ingests.
    """
    try:
        effective_mode = resolve_repeat_mode(
            repeat_mode if repeat_mode is not None else settings.repeat_mode
        )
    except ValueError as error:
        print(f"Invalid repeat mode: {error}", file=sys.stderr)
        return 2
    path = Path(source_path)
    if not path.exists():
        print(f"Source not found: {source_path}", file=sys.stderr)
        return 2
    # Folder ingest: markdown only (non-recursive), sorted for determinism.
    skipped_files: list[Path] = []
    if path.is_dir():
        entries = sorted(path.iterdir(), key=lambda p: p.name)
        source_files = [p for p in entries
                        if p.is_file() and p.suffix.lower() in (".md", ".markdown")]
        skipped_files = [p for p in entries
                         if p.is_file() and p not in source_files]
        if not source_files:
            print(f"No .md/.markdown files found in directory: {source_path}",
                  file=sys.stderr)
            return 2
    else:
        source_files = [path]
    # ADR-0006 decision 4/6: the gate is always-on, so the Decisions API key is
    # required unless explicitly opted out. Fail before any pre-flight spend.
    if not no_novelty_filter and not os.getenv("OPENROUTER_API_KEY"):
        print(
            "Novelty filter is enabled but OPENROUTER_API_KEY is unset; "
            "set it or pass --no-novelty-filter to opt out.",
            file=sys.stderr,
        )
        return 2
    try:
        preflight(settings)
    except PreflightError as error:
        print(f"Ingest pre-flight failed: {error}", file=sys.stderr)
        print(f"Hint: {error.remediation}", file=sys.stderr)
        return 1
    orchestrator, driver = build_orchestrator(
        settings, repeat_mode=effective_mode,
        novelty_filter=_novelty_filter_for(settings, no_novelty_filter))
    # Issue #101 mis-shape dispatch shares the novelty gate's key and opt-out;
    # set on the composed orchestrator so patched build_orchestrator fakes in
    # tests keep their signature.
    orchestrator.dispatcher = _dispatcher_for(settings, no_novelty_filter)
    if input_fn is not None:
        review_input: Callable[[str], str] = input_fn
    elif yes:
        review_input = _scripted_approve
    else:
        review_input = _interactive_input
    for unsupported in skipped_files:
        print(f"Skipping unsupported file: {unsupported.name}", file=sys.stderr)
    ingest_ok = 0
    ingest_failed = 0
    first_failure_code = 0
    try:
        for source_file in source_files:
            code = _ingest_single_file(orchestrator, source_file,
                                       review_input=review_input, out=out)
            if code == 0:
                ingest_ok += 1
            else:
                ingest_failed += 1
                if not first_failure_code:
                    first_failure_code = code
    finally:
        driver.close()
    if path.is_dir():
        summary = (f"Ingested {len(source_files)} files: OK {ingest_ok}, "
                   f"skipped {len(skipped_files)}")
        if ingest_failed:
            summary += f", failed {ingest_failed}"
        print(summary, file=out)
    return first_failure_code


def _ingest_single_file(orchestrator, source_path: Path, *,
                        review_input: Callable[[str], str], out) -> int:
    """Run the orchestrator over one source file and emit its stats block.

    Returns the single-file exit code: 0 approved, 4 rejected, 3 orchestrator
    error. Used by both the single-file and folder ingest paths so the review
    loop and stats emission behave identically per file.
    """
    try:
        result = orchestrator.run(source_path, input_fn=review_input)
    except Exception as error:
        print(f"Ingest failed: {error}", file=sys.stderr)
        return 3
    print(result.stats.render(), file=out)
    # Decide-mode trigger surfacing (issue #80, ADR-0005): domains that the
    # Decide-mode trigger surfacing (issue #80, ADR-0005): domains among the
    # REVIEW-APPROVED rows are announced for fact-check. Deriving from
    # result.review.approved (not result.delta) keeps a rejected ingest from
    # advertising fact-check candidates that were never committed.
    approved = [*result.review.approved.new_edges, *result.review.approved.updated_edges]
    domains = sorted({edge.domain for edge in approved if edge.domain})
    for line in fact_check_notice(
            domains, getattr(result.graph, "rows_for_domain", None)):
        print(line, file=out)
    return 0 if result.stats.verdict == "approved" else 4


def migrate_ledger_command(settings: Settings, out=sys.stdout) -> int:
    """Run the idempotent ledger backfill and print its report."""
    try:
        driver = _driver(settings)
        try:
            report = Neo4jGraphWriter(driver, database=settings.database).migrate_ledger()
        finally:
            driver.close()
    except Exception as error:  # CLI should provide a useful failure without a traceback.
        print(f"Ledger migration failed: {error}", file=sys.stderr)
        return 1
    print(
        "Ledger migration complete: "
        + ", ".join(f"{key}={value}" for key, value in report.items()),
        file=out,
    )
    return 0


def backfill_sources_command(settings: Settings, out=sys.stdout) -> int:
    """Create :Source nodes and FROM_SOURCE edges for existing ledger rows."""
    try:
        driver = _driver(settings)
        try:
            report = Neo4jGraphWriter(driver, database=settings.database).backfill_sources()
        finally:
            driver.close()
    except Exception as error:  # CLI should provide a useful failure without a traceback.
        print(f"Source backfill failed: {error}", file=sys.stderr)
        return 1
    print(
        "Source backfill complete: "
        + ", ".join(f"{key}={value}" for key, value in report.items()),
        file=out,
    )
    return 0


def backfill_evidence_command(settings: Settings, out=sys.stdout) -> int:
    """Embed un-embedded ledger evidence snippets in place."""
    try:
        driver = _driver(settings)
        try:
            embedder = _build_embedder(settings)
            report = Neo4jGraphWriter(
                driver, database=settings.database, evidence_embedder=embedder
            ).backfill_evidence_embeddings()
        finally:
            driver.close()
    except Exception as error:  # CLI should provide a useful failure without a traceback.
        print(f"Evidence backfill failed: {error}", file=sys.stderr)
        return 1
    print(
        "Evidence backfill complete: "
        + ", ".join(f"{key}={value}" for key, value in report.items()),
        file=out,
    )
    return 0


def promote_verbs_command(settings: Settings, apply: bool, out=sys.stdout) -> int:
    """Fold staged verbs from the working registry into the packaged registry.

    Dry-run by default: prints the promotion without touching the packaged
    file. ``--apply`` merges the ``proposed:`` labels into the packaged
    ``labels:`` section and clears the staging block — the deliberate human
    review gate that replaces per-ingest diffs (issue #96).
    """
    import yaml

    packaged_path = default_registry_path()
    working_path = ensure_working_registry()
    packaged_text = packaged_path.read_text(encoding="utf-8")
    packaged_doc = yaml.safe_load(packaged_text)
    working_doc = yaml.safe_load(working_path.read_text(encoding="utf-8"))
    staged = (working_doc.get("proposed") or {}).get("labels") or {}
    canonical = set(packaged_doc.get("labels", {}))
    fresh = {name: spec for name, spec in staged.items() if name not in canonical}
    if not fresh:
        print("No staged verbs to promote.", file=out)
        return 0
    if not apply:
        print(f"Would promote {len(fresh)} staged verb(s) to {packaged_path}:", file=out)
        for name in sorted(fresh):
            print(f"  {name}", file=out)
        print("Re-run with --apply to write.", file=out)
        return 0
    # Promote each staged label as a canonical label; drop the staging block.
    block = ""
    for name, spec in sorted(fresh.items()):
        description = str(
            (spec or {}).get("description", "promoted from working registry")
        ).replace("\n", " ")
        block += f"  {name}:\n    description: {description}\n"
    # Append to the end of the file: labels: entries are top-level keys, and
    # the packaged registry's trailing sections tolerate appended labels.
    text = packaged_text.rstrip("\n") + "\n" + block
    packaged_path.write_text(text, encoding="utf-8")
    working_doc["proposed"] = {"labels": {}}
    working_path.write_text(yaml.safe_dump(working_doc, sort_keys=False), encoding="utf-8")
    print(f"Promoted {len(fresh)} verb(s) to {packaged_path}; staging cleared.", file=out)
    return 0


def normalize_relations_command(settings: Settings, out=sys.stdout) -> int:
    """Run the one-off relation normalization pass and print its report."""
    try:
        driver = _driver(settings)
        try:
            report = Neo4jGraphWriter(
                driver, database=settings.database
            ).normalize_relations()
        finally:
            driver.close()
    except Exception as error:  # CLI should provide a useful failure without a traceback.
        print(f"Relation normalization failed: {error}", file=sys.stderr)
        return 1
    print(
        "Relation normalization complete: "
        + ", ".join(f"{key}={value}" for key, value in report.items()),
        file=out,
    )
    return 0


def provenance_command(settings: Settings, source_id: str, out=sys.stdout) -> int:
    """Walk everything one source claimed, including later-contradicted rows."""
    try:
        driver = _driver(settings)
        try:
            rows = Neo4jGraphWriter(driver, database=settings.database).provenance_for_source(source_id)
        finally:
            driver.close()
    except Exception as error:
        print(f"Provenance walk failed: {error}", file=sys.stderr)
        return 1
    print(f"Claims by source '{source_id}': {len(rows)} row(s)", file=out)
    for row in rows:
        print(
            f"- {row.subject} -[{row.relation}]-> {row.object} "
            f"confidence={row.confidence} source_ref={row.source_ref} "
            f"evidence={row.evidence}",
            file=out,
        )
    return 0


def _build_fact_check_seams(settings: Settings):
    """Real fact-check seams: writer, DuckDuckGo searcher, local verdict LLM."""
    from .factcheck import HtmlWebSearcher, LocalVerdictLLM
    from .llm_gateway import OpenAICompatibleMessagesClient

    driver = _driver(settings)
    writer = Neo4jGraphWriter(driver, database=settings.database)
    messages = OpenAICompatibleMessagesClient(
        base_url=settings.llm_base_url, model=settings.llm_model,
        api_key="local-placeholder",
    )
    llm = LocalVerdictLLM(messages, model=settings.llm_model)
    return driver, writer, HtmlWebSearcher(), llm


def fact_check_command(
    settings: Settings,
    *,
    domain: str | None = None,
    source: str | None = None,
    out=sys.stdout,
) -> int:
    """Run decide-mode fact-checking over a domain's or a source's rows (ADR-0005)."""
    if (domain is None) == (source is None):
        print("Provide exactly one of --domain or --source.", file=sys.stderr)
        return 2
    try:
        driver, writer, searcher, llm = _build_fact_check_seams(settings)
        try:
            if domain is not None:
                rows = writer.rows_for_domain(domain)
                label = f"domain '{domain}'"
            else:
                rows = writer.provenance_for_source(source)
                label = f"source '{source}'"
            receipts = fact_check_rows(
                rows, searcher=searcher, llm=llm,
                model=settings.llm_model, search_provider="duckduckgo",
                now=lambda: datetime.now(timezone.utc).isoformat(),
            )
            report = writer.save_verdicts(receipts)
            walked = (
                writer.verdicts_for_source(source) if source is not None else None
            )
        finally:
            if driver is not None:
                driver.close()
    except Exception as error:
        print(f"Fact-check failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Fact-check over {label}: {len(rows)} row(s) checked, "
        f"{report['verdicts_created']} verdict(s) appended "
        f"({report['rows_not_found']} row(s) not found).",
        file=out,
    )
    for receipt in receipts:
        print(
            f"- {receipt.subject} -[{receipt.relation}]-> {receipt.object} "
            f"= {receipt.verdict} (confidence {receipt.confidence:.2f}, "
            f"model={receipt.model}, search={receipt.search_provider})",
            file=out,
        )
    if walked:
        print(f"Verdicts recorded for source '{source}': {len(walked)}", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The ``pg`` CLI parser with all subcommands registered."""
    parser = argparse.ArgumentParser(description="Principle Graph local knowledge-graph tools")
    subparsers = parser.add_subparsers(dest="command")
    check = subparsers.add_parser("check", help="verify Neo4j connectivity")
    check.set_defaults(handler=lambda: check_connection(Settings.from_env()))
    init = subparsers.add_parser("init", help="verify connectivity and apply the graph schema")
    init.set_defaults(handler=lambda: apply_schema(Settings.from_env()))
    query = subparsers.add_parser(
        "query",
        help="vector search over entities and evidence",
        description=(
            "Vector-similarity search over the graph's embeddings: entities "
            "(`pg query entity`) and evidence text on ledger rows (`pg query "
            "event`). Read-only; every match carries its elementId so the next "
            "command can be bound to it. Requires a reachable Ollama embedder."
        ),
    )
    query_sub = query.add_subparsers(dest="query_command")
    qentity = query_sub.add_parser(
        "entity",
        help="vector-similarity search over :Entity embeddings",
        description=(
            "Embed the search term and rank :Entity nodes by embedding "
            "similarity (entity_embedding index) above the "
            "PG_QUERY_SEED_SIMILARITY threshold. Duplicates are expected "
            "when same-named entities differ by type; elementId binds the "
            "next command."
        ),
    )
    qentity.add_argument("term", help="search text embedded and matched against entity embeddings")
    qentity.add_argument("--top-k", type=int, default=5)
    qentity.add_argument("--format", choices=("markdown", "json"), default="markdown")
    qentity.set_defaults(
        handler=lambda a: query_entity_command(Settings.from_env(), a.term, a.top_k, a.format))
    qevent = query_sub.add_parser(
        "event",
        help="vector-similarity search over evidence embeddings",
        description=(
            "Embed the search term and rank :ExtractionEvent ledger rows by "
            "evidence-text similarity over the extraction_evidence_embedding "
            "index above the PG_QUERY_SEED_SIMILARITY threshold. "
            "Hits carry the entity pair the row connects."
        ),
    )
    qevent.add_argument("term", help="search text embedded and matched against evidence embeddings")
    qevent.add_argument("--top-k", type=int, default=5)
    qevent.add_argument("--format", choices=("markdown", "json"), default="markdown")
    qevent.set_defaults(
        handler=lambda a: query_event_command(Settings.from_env(), a.term, a.top_k, a.format))
    entity = subparsers.add_parser(
        "entity",
        help="inspect one entity by elementId",
        description=(
            "Read-only inspection of a single :Entity node, addressed by its "
            "Neo4j elementId (from `pg query entity` output)."
        ),
    )
    entity_sub = entity.add_subparsers(dest="entity_command")
    show = entity_sub.add_parser(
        "show",
        help="list an entity's linked entities, states, or events",
        description=(
            "List what one entity links to. Default lists distinct neighbor "
            "entities over typed edges with direction; --state lists "
            "StateEvent rows via HAS_STATE_EVENT; --event lists linked "
            "ExtractionEvent/StateEvent ledger rows. --state and --event are "
            "mutually exclusive."
        ),
    )
    show.add_argument("element_id", help=":Entity elementId (from `pg query entity` output)")
    show_flags = show.add_mutually_exclusive_group()
    show_flags.add_argument("--state", action="store_true",
                            help="list StateEvent rows linked via HAS_STATE_EVENT")
    show_flags.add_argument("--event", action="store_true",
                            help="list linked ExtractionEvent/StateEvent ledger rows")
    show.add_argument("--format", choices=("markdown", "json"), default="markdown")
    show.set_defaults(
        handler=lambda a: entity_show_command(Settings.from_env(), a.element_id,
                                              a.state, a.event, a.format))
    ingest = subparsers.add_parser("ingest", help="ingest a Markdown or PDF source")
    ingest.add_argument(
        "path",
        help="path to a .md/.markdown or .pdf source, or a directory whose "
             "*.md/*.markdown files are ingested (non-recursive)",
    )
    ingest.add_argument("--yes", action="store_true",
                        help="approve the Mode-2 delta without prompting (smoke runs and agents)")
    ingest.add_argument("--repeat-mode", default=None,
                        help="repeat-extraction behavior for a claim re-ingested from the "
                             "same source: keep-first (default; re-ingest is a no-op) or "
                             "refresh (replace the matched ledger row, then recompute the "
                             "arrow). Overrides PG_REPEAT_MODE.")
    ingest.add_argument("--no-novelty-filter", action="store_true",
                        help="skip the Jev novelty gate (ADR-0006) for bulk "
                             "re-ingests and backfills; default gate requires "
                             "OPENROUTER_API_KEY")
    ingest.set_defaults(handler=lambda a: ingest_command(Settings.from_env(), a.path, yes=a.yes,
                                                       repeat_mode=a.repeat_mode,
                                                       no_novelty_filter=a.no_novelty_filter))
    migrate = subparsers.add_parser(
        "migrate-ledger",
        help="backfill one :ExtractionEvent ledger row per existing typed edge (ADR-0002)",
        description=(
            "Backfill the extraction ledger in place (ADR-0002, PRD #57): every "
            "existing typed edge becomes exactly one :ExtractionEvent row seeded "
            "from that edge's confidence, evidence (first item — the row shape "
            "carries a single evidence string), and source ref; single-row "
            "aggregates equal the prior confidence, so arrow values do not move. "
            "Idempotent: re-running matches previously created rows by "
            "(subject, relation, object, source_ref) identity and changes no "
            "state — timestamps included — so the command is safe to repeat. "
            "Legacy evidence/source_ref properties are removed from migrated "
            "arrows; apply 'pg init' first so the ledger index exists."
        ),
    )
    migrate.set_defaults(handler=lambda: migrate_ledger_command(Settings.from_env()))
    backfill = subparsers.add_parser(
        "backfill-sources",
        help="create :Source nodes and FROM_SOURCE edges for existing ledger rows (ADR-0004)",
        description=(
            "Backfill per-source provenance in place (ADR-0004, issue #79): one "
            ":Source node per source id — the source_ref prefix before the first "
            "colon — carrying the id and first-seen metadata, plus a FROM_SOURCE "
            "edge from every ledger row to its source. The denormalized "
            "source_ref string on rows is untouched; ledger identity depends on "
            "it. Idempotent: re-running matches already-linked rows and existing "
            ":Source nodes and changes no state — timestamps included — so the "
            "command is safe to repeat. New ingestion writes the link "
            "automatically; only pre-existing rows need this pass."
        ),
    )
    backfill.set_defaults(handler=lambda: backfill_sources_command(Settings.from_env()))
    provenance = subparsers.add_parser(
        "provenance",
        help="walk everything one source claimed, including rows later contradicted (ADR-0004)",
        description=(
            "Provenance walk (ADR-0004, issue #79): follow FROM_SOURCE edges from "
            "the :Source node back to every ledger row that source produced — "
            "rows a later verdict or re-aggregation contradicted included — with "
            "their refs and evidence. Read-only over the ledger: what a source "
            "claimed stays visible whatever happened to it afterwards."
        ),
    )
    provenance.add_argument(
        "source_id",
        help=":Source node id — the source_ref prefix before the first colon, e.g. book-1",
    )
    provenance.set_defaults(
        handler=lambda a: provenance_command(Settings.from_env(), a.source_id))
    backfill_ev = subparsers.add_parser(
        "backfill-evidence-embeddings",
        help="embed un-embedded ledger evidence snippets (evidence-search slice)",
        description=(
            "Idempotent backfill: every ExtractionEvent / StateEvent without an "
            "evidence_embedding gets one via the local Ollama bge-m3 embedder "
            "(ADR-0001). Rows whose embedder is unavailable are skipped and "
            "picked up on the next run. Requires Ollama to be reachable."
        ),
    )
    backfill_ev.set_defaults(
        handler=lambda: backfill_evidence_command(Settings.from_env()))
    normalize = subparsers.add_parser(
        "normalize-relations",
        help="re-canonicalize ledger relations through the relation registry (ADR-0003)",
        description=(
            "One-off relation normalization pass (ADR-0003, PRD #76 slice 1): "
            "every ledger row is re-canonicalized through the versioned "
            "relation registry — alias spellings collapse onto their canonical "
            "verb, inverse-pair spellings flip to the canonical direction, and "
            "same-source verb variants collapse onto one row whose identity "
            "uses the canonical verb. The extracted verb is preserved as the "
            "row's raw_relation; unknown verbs pass through uncanonicalized "
            "and are counted in the report. Idempotent: a second run computes "
            "an empty plan and changes no state — timestamps included — so the "
            "command is safe to repeat after registry edits."
        ),
    )
    normalize.set_defaults(
        handler=lambda: normalize_relations_command(Settings.from_env())
    )
    promote = subparsers.add_parser(
        "promote-verbs",
        help="fold staged verbs from the working registry into the packaged registry",
        description=(
            "Deliberate promotion gate (issue #96): the working registry at "
            ".pg/relation-registry.yaml accumulates scan-staged verbs during "
            "ingestion; this command reviews and merges them into the "
            "git-tracked packaged registry. Dry-run by default; pass --apply "
            "to write."
        ),
    )
    promote.add_argument(
        "--apply", action="store_true",
        help="write the promotion into the packaged registry (default: dry-run)",
    )
    promote.set_defaults(
        handler=lambda a: promote_verbs_command(Settings.from_env(), apply=a.apply)
    )
    factcheck = subparsers.add_parser(
        "fact-check",
        help="decide-mode fact-checking over ledger rows (ADR-0005)",
        description=(
            "Fact-check ledger rows (PRD #76 slice 4, ADR-0005): for every row "
            "in a domain (--domain) or claimed by a source (--source), a "
            "web-search-grounded verdict LLM produces support / refute / "
            "unclear verdicts with confidence, evidence URLs, and model + "
            "search provenance, stored as append-only :Verdict nodes CHECKS-"
            "wired to the rows. Rows and arrows are never mutated — verdicts "
            "inform, a human decides. Re-runs append fresh verdicts; per-source "
            "walking rides the :Source layer."
        ),
    )
    group = factcheck.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--domain",
        help="check every ledger row carrying this domain tag",
    )
    group.add_argument(
        "--source",
        help="check every ledger row claimed by this source id",
    )
    factcheck.set_defaults(
        handler=lambda args: fact_check_command(
            Settings.from_env(), domain=args.domain, source=args.source)
    )
    reset = subparsers.add_parser(
        "reset",
        help="delete all nodes and relationships (destructive; issue #97)",
        description=(
            "Delete every node and relationship in the graph (PRD #95 story "
            "25): resetting a demo corpus becomes one command instead of "
            "ad-hoc Cypher. Destructive: the whole graph is cleared, nothing "
            "is archived. Only runs behind the --yes confirmation flag; "
            "without it the command refuses and changes no state."
        ),
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive delete of all nodes and relationships",
    )
    reset.set_defaults(
        handler=lambda args: reset_command(Settings.from_env(), yes=args.yes)
    )
    for sub in (check, init, query, ingest, migrate, backfill, provenance,
                normalize, factcheck, reset, entity):
        handler = sub._defaults.get("handler")
        if handler is not None:
            params = len(inspect.signature(handler).parameters)
            if params == 0:
                sub.set_defaults(handler=lambda a, h=handler: h())
            else:
                sub.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        # Bare `pg query` / `pg entity` with no subcommand: usage error, not a crash.
        print("Missing subcommand; see `pg --help`.", file=sys.stderr)
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
