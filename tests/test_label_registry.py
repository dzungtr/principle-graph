"""Label-registry module behavior (issue #77, ADR-0003).

External behavior only: the pure loader/lookup surface — canonicalization,
alias collapse, inverse-pair direction collapse, unknown passthrough, and
malformed-registry rejection. No database, no writer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from principle_graph.label_registry import (
    RegistryError,
    default_registry_path,
    load_label_registry,
)


@pytest.fixture(scope="module")
def registry():
    return load_label_registry(default_registry_path())


VALID_REGISTRY = """\
version: 2
labels:
  supersedes:
    aliases: [replaces, displaces]
    inverse: superseded_by
    description: A later claim displaces the earlier one.
  superseded_by:
    description: Collapses to supersedes.
  supports:
    description: Evidence backs the claim.
"""


def _load(tmp_path: Path, text: str) -> object:
    path = tmp_path / "registry.yaml"
    path.write_text(text)
    return load_label_registry(path)


# --- the packaged registry ---------------------------------------------------


def test_packaged_registry_loads_with_version_and_vocabulary(registry):
    assert registry.version == 1
    assert "supersedes" in registry.vocabulary()
    assert "reduces" in registry.vocabulary()
    assert registry.vocabulary() == tuple(sorted(registry.vocabulary()))


def test_packaged_registry_documented_inverse_pairs_are_balanced(registry):
    for label in registry.vocabulary():
        inverse = registry.entry(label).inverse
        if inverse is not None:
            # Only the canonical-direction member declares the pair.
            assert registry.entry(inverse).inverse is None


# --- canonicalization: alias collapse and direction collapse -----------------


@pytest.mark.parametrize("raw,canonical", [
    ("supersedes", "supersedes"),
    ("replaces", "supersedes"),          # alias collapse
    ("SUPPORTS", "supports"),            # case-insensitive lookup
    ("backs", "supports"),
    ("may_describe", "may_describe"),    # unknown passes through unchanged
])
def test_canonical_for_maps_aliases_and_passes_unknown(registry, raw, canonical):
    assert registry.canonical_for(raw) == canonical


def test_canonicalize_collapses_alias_onto_canonical_triple(registry):
    canon = registry.canonicalize("a", "replaces", "b")
    assert (canon.subject, canon.relation, canon.object) == ("a", "supersedes", "b")
    assert canon.raw_relation == "replaces"
    assert canon.unknown is False


def test_canonicalize_flips_inverse_spelling_to_the_canonical_direction(registry):
    canon = registry.canonicalize("a", "superseded_by", "b")
    assert (canon.subject, canon.relation, canon.object) == ("b", "supersedes", "a")
    assert canon.raw_relation == "superseded_by"
    assert canon.unknown is False


def test_canonicalize_flags_unknown_verbs_without_rejecting_them(registry):
    canon = registry.canonicalize("a", "may_describe", "b")
    assert canon.unknown is True
    assert canon.relation == "may_describe"  # passthrough, unchanged


def test_canonical_alias_of_inverse_declaring_member_does_not_flip(tmp_path):
    # The flip keys off the resolved canonical label: an alias of the member
    # that already IS the canonical direction resolves to that direction and
    # must not flip.
    registry = _load(tmp_path, VALID_REGISTRY)
    canon = registry.canonicalize("a", "displaces", "b")
    assert (canon.subject, canon.relation, canon.object) == ("a", "supersedes", "b")


def test_is_known_answers_aliases_and_canonical_labels(registry):
    assert registry.is_known("replaces")
    assert registry.is_known("SUPERSEDES")
    assert not registry.is_known("may_describe")


# --- malformed registries ----------------------------------------------------


@pytest.mark.parametrize("text,reason", [
    ("version: 0\nlabels: {a: {}}\n", "positive integer"),
    ("version: true\nlabels: {a: {}}\n", "positive integer"),
    ("version: 'one'\nlabels: {a: {}}\n", "positive integer"),
    ("labels: {a: {}}\n", "version"),
    ("version: 1\n", "labels"),
    ("version: 1\nlabels: {}\n", "non-empty"),
    ("version: 1\nlabels: {BadLabel: {}}\n", "snake_case"),
    ("version: 1\nlabels: {a: bad}\n", "mapping"),
    ("version: 1\nlabels: {a: {extra: 1}}\n", "unexpected keys"),
    ("version: 1\nlabels: {a: {aliases: not-a-list}}\n", "aliases must be a list"),
    ("version: 1\nlabels: {a: {aliases: [Bad Alias]}}\n", "aliases must be a list"),
    ("version: 1\nlabels: {a: {aliases: [a]}}\n", "lists itself"),
    ("version: 1\nlabels: {a: {}, b: {aliases: [a]}}\n", "collides with a canonical"),
    ("version: 1\nlabels: {a: {aliases: [c]}, b: {aliases: [c]}}\n", "claimed by both"),
    ("version: 1\nlabels: {a: {inverse: missing}}\n", "unknown inverse"),
    ("version: 1\nlabels: {a: {inverse: a}}\n", "its own inverse"),
    ("version: 1\nlabels: {a: {inverse: b}, b: {inverse: a}}\n", "twice"),
    ("version: 1\nlabels: {a: {description: 3}}\n", "description"),
    ("version: 1\nextra: 1\nlabels: {a: {}}\n", "unexpected top-level"),
])
def test_malformed_registry_is_rejected_with_a_clear_error(tmp_path, text, reason):
    with pytest.raises(RegistryError, match=reason):
        _load(tmp_path, text)


def test_malformed_yaml_is_rejected(tmp_path):
    with pytest.raises(RegistryError, match="not valid YAML"):
        _load(tmp_path, "version: [1\nlabels: {{")


def test_unreadable_registry_file_is_rejected(tmp_path):
    with pytest.raises(RegistryError, match="unreadable"):
        load_label_registry(tmp_path / "missing.yaml")


def test_labels_with_omitted_or_null_aliases_load_with_no_aliases(tmp_path):
    registry = _load(tmp_path, "version: 1\nlabels:\n  a:\n    description: x\n")
    assert registry.entry("a").aliases == ()
