"""industries.yaml loader: effective-keyword resolution and validation failures."""

import textwrap

import pytest

from scout.industries import TaxonomyError, load_taxonomy

MINIMAL = """
version: 1
arxiv:
  primary: [quant-ph]
  secondary: [cond-mat.mtrl-sci]
  secondary_requires: quantum_core
domains: [manufacturing]
tiers:
  1: "Direct linkage."
  2: "Sensing applied."
  3: "Context only."
keyword_sets:
  quantum_core: [qubit, quantum]
  minerals: [ore body, mineral exploration]
  sensing: [magnetometer]
groups:
  critical: {name: "Critical minerals", keyword_sets: [minerals]}
  plain: {name: "No group keywords"}
industries:
  - id: gallium
    name: Gallium
    group: critical
    tier: 1
    domains: [quantum_hardware]
    keywords: [GaAs, gallium arsenide]
    link: "III-V devices"
  - id: welding
    name: Welding
    group: plain
    tier: 2
    keywords: [weld]
    keyword_sets: [sensing]
  - id: cobalt
    name: Cobalt
    group: critical
    tier: 3
    keywords: [cobalt]
"""


def write(tmp_path, text):
    path = tmp_path / "industries.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def taxonomy(tmp_path):
    return load_taxonomy(write(tmp_path, MINIMAL))


# --------------------------------------------------------------------------
# Happy path and effective keywords
# --------------------------------------------------------------------------


def test_loads_industries_groups_and_sets(taxonomy):
    assert set(taxonomy.industries) == {"gallium", "welding", "cobalt"}
    assert set(taxonomy.groups) == {"critical", "plain"}
    assert taxonomy.primary == ("quant-ph",)
    assert taxonomy.secondary == ("cond-mat.mtrl-sci",)


def test_effective_keywords_are_the_union_of_all_three_sources(taxonomy):
    """own keywords + own keyword_sets + the group's keyword_sets."""
    welding = taxonomy.industries["welding"]
    assert welding.own_keywords == {"weld", "magnetometer"}      # own + own set
    assert welding.effective_keywords == {"weld", "magnetometer"}  # group adds nothing

    gallium = taxonomy.industries["gallium"]
    assert gallium.own_keywords == {"GaAs", "gallium arsenide"}
    # The group contributes its keyword_sets on top.
    assert gallium.effective_keywords == {
        "GaAs", "gallium arsenide", "ore body", "mineral exploration",
    }


def test_group_keywords_are_the_union_of_the_groups_sets(taxonomy):
    assert taxonomy.groups["critical"].group_keywords == {"ore body", "mineral exploration"}
    assert taxonomy.groups["plain"].group_keywords == frozenset()


def test_own_keywords_exclude_group_level_terms(taxonomy):
    """The own/group split is what decides industry-vs-group tagging later."""
    gallium = taxonomy.industries["gallium"]
    assert "ore body" not in gallium.own_keywords
    assert "ore body" in gallium.effective_keywords


def test_tier_definitions_come_from_the_file(taxonomy):
    assert taxonomy.tier_definition(1) == "Direct linkage."
    assert taxonomy.tier_definition(3) == "Context only."


def test_secondary_gate_keywords_resolve_through_secondary_requires(taxonomy):
    assert set(taxonomy.secondary_gate_keywords) == {"qubit", "quantum"}


def test_tier_one_uses_link_and_tier_two_falls_back_to_name(taxonomy):
    assert taxonomy.industries["gallium"].description == "III-V devices"
    assert taxonomy.industries["welding"].description == "Welding"


def test_by_tier_partitions_the_taxonomy(taxonomy):
    assert [i.id for i in taxonomy.by_tier(1)] == ["gallium"]
    assert [i.id for i in taxonomy.by_tier(3)] == ["cobalt"]


# --------------------------------------------------------------------------
# Validation failures -- each must name the problem
# --------------------------------------------------------------------------


def test_duplicate_ids_fail(tmp_path):
    broken = MINIMAL + """
  - id: gallium
    name: Gallium again
    group: critical
    tier: 1
    keywords: [GaN]
"""
    with pytest.raises(TaxonomyError, match="duplicate industry id 'gallium'"):
        load_taxonomy(write(tmp_path, broken))


@pytest.mark.parametrize("tier", ["0", "4", "'1'", "null"])
def test_a_tier_outside_1_2_3_fails(tmp_path, tier):
    broken = MINIMAL + f"""
  - id: odd
    name: Odd
    group: critical
    tier: {tier}
    keywords: [oddium]
"""
    with pytest.raises(TaxonomyError, match="tier"):
        load_taxonomy(write(tmp_path, broken))


def test_an_undefined_group_fails(tmp_path):
    broken = MINIMAL + """
  - id: orphan
    name: Orphan
    group: nonexistent_group
    tier: 1
    keywords: [orphanium]
"""
    with pytest.raises(TaxonomyError, match="nonexistent_group"):
        load_taxonomy(write(tmp_path, broken))


def test_an_industry_keyword_set_reference_that_does_not_exist_fails(tmp_path):
    broken = MINIMAL + """
  - id: bad_ref
    name: Bad ref
    group: critical
    tier: 2
    keywords: [something]
    keyword_sets: [no_such_set]
"""
    with pytest.raises(TaxonomyError, match="no_such_set"):
        load_taxonomy(write(tmp_path, broken))


def test_a_group_keyword_set_reference_that_does_not_exist_fails(tmp_path):
    broken = MINIMAL.replace(
        "  plain: {name: \"No group keywords\"}",
        "  plain: {name: \"No group keywords\", keyword_sets: [ghost_set]}",
    )
    with pytest.raises(TaxonomyError, match="ghost_set"):
        load_taxonomy(write(tmp_path, broken))


def test_secondary_requires_pointing_at_a_missing_set_fails(tmp_path):
    broken = MINIMAL.replace("secondary_requires: quantum_core", "secondary_requires: absent")
    with pytest.raises(TaxonomyError, match="absent"):
        load_taxonomy(write(tmp_path, broken))


def test_an_industry_that_can_never_match_fails(tmp_path):
    """No own keywords and a group that contributes none: dead config."""
    broken = MINIMAL + """
  - id: unmatchable
    name: Unmatchable
    group: plain
    tier: 2
"""
    with pytest.raises(TaxonomyError, match="can never match"):
        load_taxonomy(write(tmp_path, broken))


def test_exclusions_naming_an_unknown_keyword_fail(tmp_path):
    broken = MINIMAL + "\nexclusions:\n  not_a_keyword: [\"some phrase\"]\n"
    with pytest.raises(TaxonomyError, match="not_a_keyword"):
        load_taxonomy(write(tmp_path, broken))


def test_case_sensitive_naming_an_unknown_keyword_fails(tmp_path):
    broken = MINIMAL + "\ncase_sensitive: [NotAKeyword]\n"
    with pytest.raises(TaxonomyError, match="NotAKeyword"):
        load_taxonomy(write(tmp_path, broken))


def test_every_problem_is_reported_not_just_the_first(tmp_path):
    broken = MINIMAL + """
  - id: gallium
    name: Dupe
    group: critical
    tier: 1
    keywords: [GaN]
  - id: bad_ref
    name: Bad ref
    group: critical
    tier: 2
    keywords: [x]
    keyword_sets: [no_such_set]
"""
    with pytest.raises(TaxonomyError) as exc_info:
        load_taxonomy(write(tmp_path, broken))
    message = str(exc_info.value)
    assert "duplicate industry id" in message
    assert "no_such_set" in message
    assert "2 problems" in message


def test_a_missing_file_fails_clearly(tmp_path):
    with pytest.raises(TaxonomyError, match="not found"):
        load_taxonomy(tmp_path / "absent.yaml")


def test_invalid_yaml_fails_clearly(tmp_path):
    path = tmp_path / "industries.yaml"
    path.write_text("version: 1\n  bad indent: [\n", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="not valid YAML"):
        load_taxonomy(path)


# --------------------------------------------------------------------------
# The real shipped file
# --------------------------------------------------------------------------


def test_the_shipped_taxonomy_is_valid(project_taxonomy):
    assert len(project_taxonomy.industries) == 108
    assert {i.tier for i in project_taxonomy.industries.values()} == {1, 2, 3}
    assert project_taxonomy.primary == ("quant-ph",)


def test_the_eligible_industry_set_has_not_shrunk(project_taxonomy):
    """Tier 1+2 is what triage can choose from, and a coverage audit showed a
    narrow list silently discards good papers -- 27 experimental papers in one
    run had no industry that fitted. Pin the count so a deletion is deliberate."""
    eligible = [i for i in project_taxonomy.industries.values() if i.tier in (1, 2)]
    assert len(eligible) >= 78


@pytest.mark.parametrize(
    "industry_id",
    ["hts_tape", "lts_wire", "superconducting_electronics", "fiber_lasers",
     "graphene_2d", "sputtering_targets", "thermoelectrics",
     # Promoted from tier 3 on 2026-09-30, when the premise widened from
     # "quantum tech -> industry" to "experimental physical science -> industry".
     "electrolyzers", "cement", "aluminum_smelting", "pcbs", "lcd_panels",
     "cobalt", "silicon_metal", "inverters", "solar_pv_cells", "bearings"],
)
def test_the_coverage_audit_industries_are_present_and_eligible(
    project_taxonomy, industry_id
):
    """Added after the run-8 audit. Each exists to stop a specific shoehorning:
    superconducting wire papers were being filed under `tungsten`, lasers under
    `led_gan`, ion-trap hardware under `mature_node_foundry`."""
    industry = project_taxonomy.industries[industry_id]
    assert industry.tier in (1, 2), "must be eligible, not tier 3"
    assert industry.effective_keywords


def test_no_eligible_industry_is_defined_in_purely_quantum_terms(project_taxonomy):
    """The description IS what triage reads as the industry's definition.

    While `polysilicon` was described as "feedstock for isotopically enriched
    Si-28 spin-qubit substrates", a perovskite solar paper scored 0 against it --
    even though solar is what polysilicon is actually for.
    """
    import re

    quantum_only = re.compile(
        r"qubit|QKD|spin-qubit|single-photon|entangl|quantum memor|quantum network",
        re.I,
    )
    offenders = [
        i.id
        for i in project_taxonomy.industries.values()
        if i.tier in (1, 2) and quantum_only.search(i.description)
    ]
    assert not offenders, (
        f"{offenders} are defined only in quantum terms, which makes the triage "
        "model score their non-quantum papers 0"
    )


def test_the_tier_definitions_are_domain_neutral(project_taxonomy):
    """Tier 2 used to require "quantum sensing or metrology applied to" the
    industry, which excluded every non-quantum measurement by construction."""
    for tier in (1, 2):
        assert "quantum" not in project_taxonomy.tier_definition(tier).lower()


def test_every_shipped_industry_can_match_something(project_taxonomy):
    for industry in project_taxonomy.industries.values():
        assert industry.effective_keywords, f"{industry.id} has no keywords"
