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


# --- Entity-type registry (issue #96): same loader, no second implementation ---


def test_packaged_entity_registry_loads_through_the_shared_loader():
    from principle_graph.label_registry import default_entity_registry_path
    entity_registry = load_label_registry(default_entity_registry_path())
    assert entity_registry.version >= 1
    canonical = set(entity_registry.vocabulary())
    assert canonical == {
        "person", "organization", "country", "place", "event", "policy",
        "agreement", "product", "technology", "market", "metric", "concept",
    }


def test_entity_type_aliases_collapse_to_canonical():
    from principle_graph.label_registry import default_entity_registry_path
    entity_registry = load_label_registry(default_entity_registry_path())
    assert entity_registry.canonical_for("politician") == "person"
    assert entity_registry.canonical_for("geopolitical_entity") == "organization"
    assert entity_registry.canonical_for("central_bank") == "organization"
    assert entity_registry.is_known("treaty")


def test_unknown_entity_type_passes_through_unchanged():
    from principle_graph.label_registry import default_entity_registry_path
    entity_registry = load_label_registry(default_entity_registry_path())
    assert entity_registry.canonical_for("xenosophy") == "xenosophy"
    assert not entity_registry.is_known("xenosophy")


# --- State-key registry (issue #96): same loader, no second implementation ---


def test_packaged_state_registry_loads_through_the_shared_loader():
    from principle_graph.label_registry import default_state_registry_path
    state_registry = load_label_registry(default_state_registry_path())
    assert state_registry.version >= 1
    for key in (
        "approval_rating", "vote_share", "policy_rate",
        "short_term_borrowing", "trade_volume",
    ):  # named in issue #96
        assert key in state_registry.vocabulary(), key


def test_state_key_aliases_collapse_and_unknown_keys_pass():
    from principle_graph.label_registry import default_state_registry_path
    state_registry = load_label_registry(default_state_registry_path())
    assert state_registry.canonical_for("popularity") == "approval_rating"
    assert state_registry.canonical_for("unheard_of_metric") == "unheard_of_metric"
    assert not state_registry.is_known("unheard_of_metric")


# --- the packaged registry ---------------------------------------------------


def test_packaged_registry_loads_with_version_and_vocabulary(registry):
    assert registry.version >= 1
    assert "supersedes" in registry.vocabulary()
    assert "reduces" in registry.vocabulary()
    assert registry.vocabulary() == tuple(sorted(registry.vocabulary()))


def test_packaged_registry_spans_six_facets_with_about_30_verbs(registry):
    # issue #96: ~30 canonical verbs across causal, structural, temporal/event,
    # control/economic, epistemic, and comparative/constraint facets.
    assert 25 <= len(registry.vocabulary()) <= 35
    facets = [
        "causes",          # causal
        "part_of",         # structural
        "precedes",        # temporal/event
        "funds",           # control/economic
        "declared",        # epistemic
        "exceeds",         # comparative/constraint
    ]
    for facet_probe in facets:
        assert facet_probe in registry.vocabulary(), facet_probe


@pytest.mark.parametrize("raw,canonical", [
    ("CAUSED", "causes"),
    ("DECLARED", "declared"),
    ("VISITED", "visited"),
    ("COVERS", "includes"),
    ("INCLUDE", "includes"),
    ("PREDICTED", "predicts"),
    ("DESCRIBED_AS", "described_as"),
    ("EXPECTS", "expects"),
])
def test_demo_corpus_raw_verbs_normalize(registry, raw, canonical):
    # issue #96: verbs observed in the demo corpus alias-map onto canonicals.
    assert registry.canonical_for(raw) == canonical
    assert registry.is_known(raw)


# --- `proposed:` staging (issue #96) -----------------------------------------


def test_staged_verbs_participate_in_canonicalization_immediately(tmp_path):
    # Scan-discovered verbs land in the staging section and canonicalize like
    # any promoted verb — appending is a pure data-file edit, no code change.
    registry = _load(tmp_path, """
version: 1
labels:
  causes:
    description: Subject brings the object about.
proposed:
  labels:
    ghosted:
      aliases: [ghosted_out]
      description: Scan-discovered; awaiting git-review promotion.
""")
    assert registry.is_known("ghosted")
    assert registry.canonical_for("ghosted_out") == "ghosted"
    assert "ghosted" in registry.vocabulary()
    assert registry.staged_labels() == ("ghosted",)


def test_staged_labels_are_distinguishable_from_promoted_labels(tmp_path):
    registry = _load(tmp_path, """
version: 1
labels:
  causes: {}
proposed:
  labels:
    ghosted: {}
""")
    assert "causes" not in registry.staged_labels()
    assert set(registry.staged_labels()) == {"ghosted"}
    assert set(registry.vocabulary()) == {"causes", "ghosted"}


def test_staged_section_colliding_with_canonical_labels_is_rejected(tmp_path):
    with pytest.raises(RegistryError, match="collides with a canonical"):
        _load(tmp_path, """
version: 1
labels:
  causes: {}
proposed:
  labels:
    causes: {}
""")


def test_staged_alias_colliding_with_canonical_alias_is_rejected(tmp_path):
    with pytest.raises(RegistryError, match="claimed by both"):
        _load(tmp_path, """
version: 1
labels:
  a: {aliases: [shared]}
  b: {}
proposed:
  labels:
    c: {aliases: [shared]}
""")


def test_staged_inverse_pair_against_promoted_label_resolves(tmp_path):
    registry = _load(tmp_path, """
version: 1
labels:
  causes:
    inverse: caused_by
  caused_by:
    description: collapses to causes.
proposed:
  labels:
    triggered:
      inverse: triggered_by
    triggered_by:
      description: staged pair, declarer is triggered.
""")
    canon = registry.canonicalize("a", "triggered_by", "b")
    assert (canon.subject, canon.relation, canon.object) == ("b", "triggered", "a")



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


# --- Domain registry (issue #78): same loader, no second implementation ---

def test_packaged_domain_registry_loads_through_the_shared_loader():
    from principle_graph.label_registry import default_domain_registry_path
    domain_registry = load_label_registry(default_domain_registry_path())
    assert domain_registry.version >= 1
    assert "economics" in domain_registry.vocabulary()


def test_domain_alias_collapses_to_canonical():
    from principle_graph.label_registry import default_domain_registry_path
    domain_registry = load_label_registry(default_domain_registry_path())
    assert domain_registry.canonical_for("macroeconomics") == "economics"
    assert domain_registry.is_known("macroeconomics")


def test_unknown_domain_passes_through_unchanged():
    from principle_graph.label_registry import default_domain_registry_path
    domain_registry = load_label_registry(default_domain_registry_path())
    assert domain_registry.canonical_for("xenosophy") == "xenosophy"
    assert not domain_registry.is_known("xenosophy")


def test_ensure_working_registry_seeds_once(tmp_path, monkeypatch):
    # Working-registry slice: first call seeds from the packaged file; an
    # existing working file is never overwritten.
    import shutil
    from principle_graph.label_registry import (
        default_registry_path, ensure_working_registry)
    monkeypatch.chdir(tmp_path)
    working = ensure_working_registry()
    assert working.read_text(encoding="utf-8") == default_registry_path().read_text(encoding="utf-8")
    working.write_text("version: 999\nlabels: {}\n", encoding="utf-8")
    ensure_working_registry()
    assert working.read_text(encoding="utf-8") == "version: 999\nlabels: {}\n"


def test_resolve_relation_registry_path_prefers_working(tmp_path, monkeypatch):
    from principle_graph.label_registry import (
        default_registry_path, ensure_working_registry,
        resolve_relation_registry_path)
    monkeypatch.chdir(tmp_path)
    assert resolve_relation_registry_path() == default_registry_path()
    ensure_working_registry()
    assert resolve_relation_registry_path().name == "relation-registry.yaml"
    assert ".pg" in str(resolve_relation_registry_path())
