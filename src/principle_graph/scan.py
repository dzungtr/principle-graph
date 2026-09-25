"""Two-pass scan (issue #102, PRD #95 *Solution* bullet 1).

Per-source batched lightweight scan pass that inventories candidate verbs and
entity mentions, consolidates them deterministically first (relation registry
exact/alias match from #96, live-graph verbs via ``list_relation_types()``,
graph entities via the existing resolution lookups), then one LLM clustering
call over the unmatched remainder only. Outputs a source verb menu + entity
roster injected into every extraction chunk prompt (injection point from
#100).

Scan-discovered new verbs auto-append to the registry's ``proposed:`` staging
section — git is the review gate (#96). Alias knowledge accumulates on entity
nodes in the graph (via resolution merges), never in config.

Scan failures raise :class:`ScanError`; the orchestrator runs the scan before
any extraction or write, so a failure aborts the ingest with no partial state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

import yaml

from .extraction_contract import Chunk
from .label_registry import (
    LabelRegistry,
    default_registry_path,
    ensure_working_registry,
    load_label_registry,
    resolve_relation_registry_path,
)

SCAN_TOOL: dict[str, Any] = {
    "name": "scan_candidates",
    "description": (
        "Report candidate relation verbs and entity mentions occurring in the "
        "supplied source text. Lightweight inventory only — no relationship "
        "claims, no extraction."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verbs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Relation verbs asserted between entities (e.g. 'reduces', 'signed').",
            },
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "entity_type": {"type": "string"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "description": "Named entities mentioned in the text, with their surface forms.",
            },
        },
        "required": ["verbs", "entities"],
        "additionalProperties": False,
    },
}

CLUSTER_TOOL: dict[str, Any] = {
    "name": "consolidate_candidates",
    "description": (
        "Cluster unmatched verb and entity candidates into canonical forms. "
        "Group surface-form variants under one canonical name and give the "
        "variants back as aliases."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verbs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical": {"type": "string"},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["canonical", "aliases"],
                    "additionalProperties": False,
                },
            },
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical": {"type": "string"},
                        "entity_type": {"type": "string"},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["canonical", "entity_type", "aliases"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["verbs", "entities"],
        "additionalProperties": False,
    },
}


class ScanError(RuntimeError):
    """A scan or consolidation call failed or returned malformed output."""


class ScanClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class ScanStore(Protocol):
    """Minimal store seam for consolidation; ``list_relation_types`` is new (#102)."""

    def list_relation_types(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class RosterEntry:
    """One entity-roster row: canonical name plus alias hints for the prompt."""

    canonical: str
    entity_type: str
    aliases: tuple[str, ...] = ()

    def render(self) -> str:
        line = f"{self.canonical} ({self.entity_type}"
        if self.aliases:
            line += f"; aka: {', '.join(self.aliases)}"
        return line + ")"


@dataclass(frozen=True)
class ScanResult:
    """Scan outputs injected into every extraction chunk prompt."""

    verb_menu: tuple[str, ...]
    entity_roster: tuple[RosterEntry, ...]
    # New verbs appended to the registry's `proposed:` staging section this scan.
    staged_verbs: tuple[str, ...] = ()
    scan_calls: int = 0
    consolidation_calls: int = 0


def normalize_verb(verb: str) -> str:
    """Lightweight snake_case normalization matching the registry's label shape."""
    return re.sub(r"[^a-z0-9]+", "_", str(verb).casefold()).strip("_") or "unknown"


@dataclass
class SourceScanner:
    """Composition root for one source scan: client + registries + store seam."""

    client: ScanClient
    relation_registry: LabelRegistry | None = None
    store: Any | None = None
    embedder: Any | None = None
    entity_registry: LabelRegistry | None = None
    registry_path: str | Path | None = None
    model: str = "claude-sonnet-4-20250514"
    batch_size: int = 4
    scan_system: str = (
        "You scan a source document and inventory candidate relation verbs and "
        "entity mentions. Report only what occurs in the supplied text. Use the "
        "scan_candidates tool and no prose."
    )
    cluster_system: str = (
        "You consolidate unmatched verb and entity candidates. Group surface-form "
        "variants under one canonical name (people by full name, organizations by "
        "standard short name) and list the variants as aliases. Verbs get a "
        "lowercase snake_case canonical form. Use the consolidate_candidates tool "
        "and no prose."
    )
    # Scratch state for the last scan.
    last_result: ScanResult | None = field(default=None, repr=False)

    def scan(self, chunks: Sequence[Chunk]) -> ScanResult:
        result = scan_source(
            chunks,
            self.client,
            relation_registry=self.relation_registry,
            store=self.store,
            embedder=self.embedder,
            entity_registry=self.entity_registry,
            registry_path=self.registry_path,
            model=self.model,
            batch_size=self.batch_size,
            scan_system=self.scan_system,
            cluster_system=self.cluster_system,
        )
        self.last_result = result
        return result


def scan_source(
    chunks: Sequence[Chunk],
    client: ScanClient,
    *,
    relation_registry: LabelRegistry | None = None,
    store: Any | None = None,
    embedder: Any | None = None,
    entity_registry: LabelRegistry | None = None,
    registry_path: str | Path | None = None,
    model: str = "claude-sonnet-4-20250514",
    batch_size: int = 4,
    scan_system: str = "Scan the supplied source text; report verbs and entity mentions via the scan_candidates tool.",
    cluster_system: str = "Cluster the unmatched candidates via the consolidate_candidates tool.",
) -> ScanResult:
    """Scan one source: batched inventory → deterministic anchoring → one clustering call."""
    if relation_registry is None:
        relation_registry = load_label_registry(resolve_relation_registry_path())
    raw_verbs: list[str] = []
    raw_entities: list[dict[str, str]] = []
    scan_calls = 0
    for batch in _batch(chunks, max(1, batch_size)):
        response = client.create(
            model=model,
            system=scan_system,
            max_tokens=16384,
            tools=[SCAN_TOOL],
            tool_choice={"type": "tool", "name": "scan_candidates"},
            messages=[{"role": "user", "content": _batch_text(batch)}],
        )
        scan_calls += 1
        payload = _tool_input(response, "scan_candidates")
        if payload is None:
            raise ScanError("scan call did not return a scan_candidates tool call")
        raw_verbs.extend(str(v) for v in payload.get("verbs", ()))
        raw_entities.extend(payload.get("entities", ()) or ())

    anchored_verbs, unmatched_verbs = _anchor_verbs(raw_verbs, relation_registry, store)
    anchored_roster, unmatched_entities = _anchor_entities(
        raw_entities, store, embedder, entity_registry, chunks)

    consolidation_calls = 0
    menu = set(anchored_verbs)
    roster = list(anchored_roster)
    staged: list[str] = []
    if unmatched_verbs or unmatched_entities:
        consolidation_calls = 1
        clustered = client.create(
            model=model,
            system=cluster_system,
            max_tokens=16384,
            tools=[CLUSTER_TOOL],
            tool_choice={"type": "tool", "name": "consolidate_candidates"},
            messages=[{"role": "user", "content": _remainder_text(
                unmatched_verbs, unmatched_entities)}],
        )
        payload = _tool_input(clustered, "consolidate_candidates")
        if payload is None:
            raise ScanError("consolidation call did not return a consolidate_candidates tool call")
        for entry in payload.get("verbs", ()) or ():
            canonical = normalize_verb(entry.get("canonical", ""))
            menu.add(canonical)
            if relation_registry is not None and not relation_registry.is_known(canonical):
                staged.append(canonical)
        seen: set[str] = {r.canonical.casefold() for r in roster}
        for entry in payload.get("entities", ()) or ():
            canonical = str(entry.get("canonical", "")).strip()
            if not canonical or canonical.casefold() in seen:
                continue
            seen.add(canonical.casefold())
            roster.append(RosterEntry(
                canonical,
                str(entry.get("entity_type", "") or "concept"),
                tuple(str(a) for a in entry.get("aliases", ()) or ()),
            ))
    if staged:
        # Staging writes go to the working registry under .pg/ (issue #96):
        # seeded from the packaged file on first use, so ingestion never
        # dirties a git-tracked packaged file. Deliberate promotion back into
        # the packaged registry is the human review gate (pg promote-verbs).
        path = registry_path if registry_path is not None else ensure_working_registry()
        append_proposed_verbs(path, staged)

    return ScanResult(
        verb_menu=tuple(sorted(menu)),
        entity_roster=tuple(roster),
        staged_verbs=tuple(sorted(set(staged))),
        scan_calls=scan_calls,
        consolidation_calls=consolidation_calls,
    )


def append_proposed_verbs(registry_path: str | Path, verbs: Sequence[str]) -> tuple[str, ...]:
    """Append new verbs to a registry YAML's ``proposed.labels`` staging section.

    Idempotent: already-canonical or already-staged verbs are skipped. Git is
    the review gate — the edit is a plain file change a human promotes (#96).
    """
    path = Path(registry_path)
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    existing = set(document.get("labels", {}))
    proposed = document.get("proposed") or {}
    staged = set(proposed.get("labels", {}))
    fresh = sorted({normalize_verb(v) for v in verbs} - existing - staged - {"unknown"})
    if not fresh:
        return ()
    entry_lines = "".join(f"  {verb}:\n    description: proposed by scan (issue #102).\n" for verb in fresh)
    nested_lines = "".join(f"    {verb}:\n      description: proposed by scan (issue #102).\n" for verb in fresh)
    inline = re.search(r"^proposed:\n(\s+labels:)\s*\{\}\s*$", text, re.M)
    block = re.search(r"^(proposed:\n\s+labels:)\s*$", text, re.M)
    if inline:
        text = text[:inline.start()] + f"proposed:\n{inline.group(1)}\n{nested_lines}" + text[inline.end():]
    elif block:
        text = text[:block.end(1)] + "\n" + nested_lines + text[block.end():]
    else:
        text = text.rstrip("\n") + "\nproposed:\n  labels:\n" + entry_lines
    path.write_text(text, encoding="utf-8")
    return tuple(fresh)


def render_verb_menu(verbs: Sequence[str]) -> str:
    return ", ".join(verbs)


def render_entity_roster(entries: Sequence[Any]) -> str:
    rendered = [e.render() if isinstance(e, RosterEntry) else str(e) for e in entries]
    return "; ".join(rendered)


# --- internals ---------------------------------------------------------------

def _batch(chunks: Sequence[Chunk], size: int):
    for start in range(0, len(chunks), size):
        yield chunks[start:start + size]


def _batch_text(batch: Sequence[Chunk]) -> str:
    return "\n\n".join(f"[{chunk.id}]\n{chunk.text}" for chunk in batch)


def _remainder_text(unmatched_verbs: Sequence[str], unmatched_entities: Sequence[dict[str, str]]) -> str:
    lines = ["Unmatched verb candidates:"]
    if unmatched_verbs:
        lines.extend(f"- {verb}" for verb in unmatched_verbs)
    else:
        lines.append("- (none)")
    lines.append("Unmatched entity candidates:")
    if unmatched_entities:
        lines.extend(
            f"- {entity.get('name', '')} ({entity.get('entity_type', '')})"
            for entity in unmatched_entities)
    else:
        lines.append("- (none)")
    return "\n".join(lines)


def _anchor_verbs(raw_verbs: Sequence[str], registry: LabelRegistry,
                  store: Any | None) -> tuple[list[str], list[str]]:
    """Deterministic first: registry exact/alias match, then live-graph verb types."""
    graph_verbs: set[str] = set()
    if store is not None:
        list_types = getattr(store, "list_relation_types", None)
        if callable(list_types):
            graph_verbs = {normalize_verb(v) for v in list_types()}
    anchored: list[str] = []
    unmatched: list[str] = []
    for verb in raw_verbs:
        canonical = normalize_verb(verb)
        if registry.is_known(canonical):
            anchored.append(registry.canonical_for(canonical))
        elif canonical in graph_verbs:
            anchored.append(canonical)
        else:
            unmatched.append(verb)
    return anchored, unmatched


def _anchor_entities(raw_entities: Sequence[dict[str, str]], store: Any | None,
                     embedder: Any | None, entity_registry: LabelRegistry | None,
                     chunks: Sequence[Chunk]) -> tuple[list[RosterEntry], list[dict[str, str]]]:
    """Deterministic first: existing resolution lookups against the live graph."""
    if not raw_entities or store is None:
        return [], list(raw_entities)
    # Import here so scan stays importable without resolution side effects.
    from .resolution import EntityResolver, SessionRegistry

    resolver = EntityResolver(store, embedder, SessionRegistry(),
                              entity_registry=entity_registry)
    source_ref = chunks[0].source_ref if chunks else ""
    anchored: list[RosterEntry] = []
    unmatched: list[dict[str, str]] = []
    seen: set[str] = set()
    for mention in raw_entities:
        name = str(mention.get("name", "")).strip()
        if not name:
            continue
        entity_type = str(mention.get("entity_type", "") or "concept")
        resolution = resolver.resolve(name, entity_type, source_ref=source_ref)
        canonical = resolution.canonical
        if canonical is not None and resolution.outcome != "create":
            if canonical.name.casefold() not in seen:
                seen.add(canonical.name.casefold())
                anchored.append(RosterEntry(
                    canonical.name, canonical.type,
                    tuple(dict.fromkeys(
                        tuple(canonical.aliases)
                        + tuple(resolution.new_aliases)
                        + ((name,) if name != canonical.name else ()))),
                ))
        else:
            unmatched.append(mention)
    return anchored, unmatched


def _tool_input(response: Any, tool_name: str) -> dict[str, Any] | None:
    for block in getattr(response, "content", ()):
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                return payload
    return None


__all__ = [
    "CLUSTER_TOOL",
    "RosterEntry",
    "SCAN_TOOL",
    "ScanError",
    "ScanResult",
    "SourceScanner",
    "append_proposed_verbs",
    "normalize_verb",
    "render_entity_roster",
    "render_verb_menu",
    "scan_source",
]
