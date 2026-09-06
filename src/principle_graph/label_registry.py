"""Generic label registry: versioned YAML vocabularies with deterministic lookup.

Pure module — no database dependency (PRD #76 Implementation Decisions). Serves
the relation registry (ADR-0003) and, by the same loader contract, the domain
registry (slice #78). Consumers: the write boundary (canonicalization before
any Cypher is generated), humans consolidating overflow types, and query-time
prompt embedding.

Registry semantics:
- labels are lowercase ``snake_case`` (the extraction contract's normalized
  label shape); lookup is case-insensitive so writer-side uppercase spellings
  resolve identically;
- ``aliases`` collapse onto their canonical label;
- ``inverse`` declares the canonical direction of an inverse pair: the entry
  that declares ``inverse: other`` is the pair's canonical spelling, and an
  incoming edge spelled with ``other`` flips to ``(object, canonical,
  subject)`` — one fact, one arrow (ADR-0003 direction collapse);
- unknown labels pass through unchanged and are flagged by the caller, never
  rejected (extraction contract: an unfamiliar book still ingests).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_LABEL = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class RegistryError(ValueError):
    """A registry file is missing, unreadable, or malformed (with its path)."""


@dataclass(frozen=True)
class CanonicalRelation:
    """One canonicalized write: triple plus the verb exactly as extracted."""

    subject: str
    relation: str
    object: str
    raw_relation: str
    unknown: bool


@dataclass(frozen=True)
class LabelEntry:
    canonical: str
    aliases: tuple[str, ...]
    # Set on the canonical-direction member of an inverse pair only.
    inverse: str | None
    description: str


class LabelRegistry:
    """Immutable lookup over one loaded registry file."""

    def __init__(self, version: int, labels: dict[str, LabelEntry]) -> None:
        self.version = version
        self._labels = dict(labels)
        self._alias_to_canonical: dict[str, str] = {}
        for canonical, entry in labels.items():
            for alias in entry.aliases:
                self._alias_to_canonical[alias] = canonical
        # Non-canonical-direction member -> canonical-direction member.
        self._flip_to: dict[str, str] = {
            entry.inverse: label
            for label, entry in labels.items()
            if entry.inverse is not None
        }

    def is_known(self, label: str) -> bool:
        key = str(label).strip().lower()
        return key in self._labels or key in self._alias_to_canonical

    def canonical_for(self, label: str) -> str:
        """Canonical spelling of *label*; unknown labels pass through unchanged."""
        key = str(label).strip().lower()
        return self._alias_to_canonical.get(key, key)

    def canonicalize(self, subject: str, relation: str, object_: str) -> CanonicalRelation:
        """Alias collapse + inverse-pair direction collapse for one incoming write."""
        raw = str(relation)
        canonical = self.canonical_for(raw)
        flipped = self._flip_to.get(canonical)
        unknown = not self.is_known(raw)
        if flipped is not None:
            return CanonicalRelation(
                str(object_), flipped, str(subject), raw, unknown
            )
        return CanonicalRelation(str(subject), canonical, str(object_), raw, unknown)

    def vocabulary(self) -> tuple[str, ...]:
        """The canonical vocabulary, sorted — for query-time prompt embedding."""
        return tuple(sorted(self._labels))

    def entry(self, label: str) -> LabelEntry:
        return self._labels[str(label).strip().lower()]


def default_registry_path() -> Path:
    """The packaged relation registry (ADR-0003); override via Settings.registry_path."""
    return Path(__file__).parent / "data" / "relation-registry.yaml"


def load_label_registry(path: str | Path) -> LabelRegistry:
    """Load and validate a registry YAML file; malformed files raise ``RegistryError``."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RegistryError(f"registry file unreadable: {path}: {error}") from error
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise RegistryError(f"registry file is not valid YAML: {path}: {error}") from error
    _validate_document(document, path)

    labels: dict[str, LabelEntry] = {}
    for name, spec in document["labels"].items():
        aliases = tuple(spec.get("aliases", ()))
        inverse = spec.get("inverse")
        labels[name] = LabelEntry(
            canonical=name,
            aliases=aliases,
            inverse=inverse,
            description=str(spec.get("description", "")),
        )
    return LabelRegistry(int(document["version"]), labels)


def _validate_document(document: Any, path: Path) -> None:
    def fail(reason: str) -> RegistryError:
        return RegistryError(f"malformed registry file {path}: {reason}")

    if not isinstance(document, dict):
        raise fail("top level must be a mapping with 'version' and 'labels'")
    if set(document) - {"version", "labels"}:
        raise fail(f"unexpected top-level keys: {sorted(set(document) - {'version', 'labels'})}")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise fail("'version' must be a positive integer")
    labels = document.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise fail("'labels' must be a non-empty mapping")

    for name, spec in labels.items():
        if not isinstance(name, str) or not _LABEL.fullmatch(name):
            raise fail(f"label {name!r} is not lowercase snake_case")
        if not isinstance(spec, dict):
            raise fail(f"label {name!r} entry must be a mapping")
        unexpected = set(spec) - {"aliases", "inverse", "description"}
        if unexpected:
            raise fail(f"label {name!r} has unexpected keys: {sorted(unexpected)}")
        # An omitted or null aliases key means "no aliases"; YAML may also
        # deliver the empty form as None rather than a missing key.
        aliases = spec.get("aliases") or []
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not _LABEL.fullmatch(alias) for alias in aliases
        ):
            raise fail(f"label {name!r} aliases must be a list of lowercase snake_case strings")
        if name in aliases:
            raise fail(f"label {name!r} lists itself as an alias")
        inverse = spec.get("inverse")
        if inverse is not None and (not isinstance(inverse, str) or not _LABEL.fullmatch(inverse)):
            raise fail(f"label {name!r} inverse must be a lowercase snake_case label")
        if inverse == name:
            raise fail(f"label {name!r} cannot be its own inverse")
        description = spec.get("description", "")
        if not isinstance(description, str):
            raise fail(f"label {name!r} description must be a string")

    seen_aliases: dict[str, str] = {}
    for name, spec in labels.items():
        for alias in spec.get("aliases") or []:
            if alias in labels:
                raise fail(f"alias {alias!r} collides with a canonical label")
            if alias in seen_aliases:
                raise fail(
                    f"alias {alias!r} is claimed by both {seen_aliases[alias]!r} and {name!r}"
                )
            seen_aliases[alias] = name

    for name, spec in labels.items():
        inverse = spec.get("inverse")
        if inverse is None:
            continue
        if inverse not in labels:
            raise fail(f"label {name!r} declares unknown inverse {inverse!r}")
        partner = labels[inverse]
        if partner.get("inverse") is not None:
            raise fail(
                f"inverse pair {name!r}/{inverse!r} declares a canonical direction twice"
            )


__all__ = [
    "CanonicalRelation",
    "LabelEntry",
    "LabelRegistry",
    "RegistryError",
    "default_registry_path",
    "load_label_registry",
]
