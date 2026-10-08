"""Keyword pre-filter, against the real shipped industries.yaml.

Each fixture is a short abstract in the style of a real arXiv summary. The point
of testing against the shipped taxonomy rather than a toy one is that these
assertions break when a keyword edit has a consequence nobody expected.
"""

import pytest

from scout.matcher import (
    GROUP_HIT_TIER,
    match_paper,
    match_papers,
    normalise,
    passes_secondary_gate,
)


def run(taxonomy, index, title, abstract, arxiv_id="2609.00001"):
    return match_paper(taxonomy, index, arxiv_id, title, abstract)


# --------------------------------------------------------------------------
# Normalisation: hyphen and space variants are equivalent
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("cryo-CMOS", "cryo CMOS"),
        ("nitrogen-vacancy", "nitrogen vacancy"),
        ("III-V", "III V"),
        ("Er:YSO", "Er YSO"),
        ("Ge/SiGe", "Ge SiGe"),
        ("grain-oriented", "grain oriented"),
    ],
)
def test_hyphen_and_space_variants_normalise_alike(a, b):
    assert normalise(a) == normalise(b)


def test_matching_is_case_insensitive(project_taxonomy, project_index):
    lower = run(project_taxonomy, project_index, "study of gallium arsenide", "x")
    upper = run(project_taxonomy, project_index, "STUDY OF GALLIUM ARSENIDE", "x")
    assert "gallium" in lower.industries
    assert lower.industries.keys() == upper.industries.keys()


def test_hyphenated_text_matches_a_spaced_keyword(project_taxonomy, project_index):
    """`cryo-CMOS` in the YAML must match "cryo CMOS" in prose, and vice versa."""
    hyphen = run(project_taxonomy, project_index, "A cryo-CMOS controller", "300 mm wafers.")
    spaced = run(project_taxonomy, project_index, "A cryo CMOS controller", "300 mm wafers.")
    assert "mature_node_foundry" in hyphen.industries
    assert "mature_node_foundry" in spaced.industries


def test_word_boundaries_stop_substring_matches(project_taxonomy, project_index):
    """"weld" must not fire on "welded" is NOT the rule -- but it must not fire
    inside an unrelated word either."""
    match = run(project_taxonomy, project_index, "Weldon's theorem", "A purely formal result.")
    assert "welding" not in match.industries


# --------------------------------------------------------------------------
# The fixtures named in the spec
# --------------------------------------------------------------------------


def test_gaas_quantum_dot_source_matches_gallium_and_high_purity_arsenic(
    project_taxonomy, project_index
):
    match = run(
        project_taxonomy,
        project_index,
        "Bright single-photon source from an InGaAs quantum dot",
        "We report a gallium arsenide quantum dot single-photon source grown by "
        "molecular beam epitaxy, reaching 62% extraction efficiency at 4 K.",
    )
    assert {"gallium", "high_purity_arsenic"} <= set(match.industries)
    assert match.is_candidate
    assert match.tier == 1
    # gallium matched specifically, so its group is NOT tagged as well.
    assert "critical_minerals" not in match.groups


def test_nv_lithium_plating_paper_matches_the_battery_chain_group(
    project_taxonomy, project_index
):
    match = run(
        project_taxonomy,
        project_index,
        "Operando magnetic imaging of lithium plating",
        "Using a quantum diamond microscope we perform operando NV magnetometry of "
        "lithium plating in a working lithium-ion cell, resolving 40 um features.",
    )
    assert "battery_chain" in match.groups
    assert match.is_candidate
    # A group hit is reported at tier 2.
    assert match.tier == GROUP_HIT_TIER
    # The whole point of group tagging: not every member industry is tagged.
    assert "lfp_cathode" not in match.industries
    assert "battery_separators" not in match.industries


def test_gravimeter_paper_tags_the_group_not_individual_minerals(
    project_taxonomy, project_index
):
    match = run(
        project_taxonomy,
        project_index,
        "A cold-atom gravimeter for mineral exploration",
        "We demonstrate an atom interferometer gravimeter for mineral exploration, "
        "resolving an ore body at 300 m depth during an airborne magnetometry survey.",
    )
    assert "critical_minerals" in match.groups
    # Not one of the nine individual critical-mineral industries.
    assert not {
        industry_id
        for industry_id in match.industries
        if project_taxonomy.industries[industry_id].group == "critical_minerals"
    }


def test_pure_theory_paper_produces_no_industry_matches(project_taxonomy, project_index):
    match = run(
        project_taxonomy,
        project_index,
        "Entanglement entropy bounds in conformal field theory",
        "We derive an exact bound on the entanglement entropy of a bipartite system "
        "and prove saturation for a class of states. No numerics are required.",
    )
    assert match.industries == {}
    assert match.groups == {}
    assert match.tier3_tags == {}
    assert not match.is_candidate
    assert match.tier is None


# --------------------------------------------------------------------------
# False positives
# --------------------------------------------------------------------------


def test_bearing_in_mind_does_not_tag_bearings(project_taxonomy, project_index):
    """The paper is a genuine candidate, so the `bearings` tag is suppressed by the
    exclusion phrase rather than by tier-3 rules doing the work for us."""
    match = run(
        project_taxonomy,
        project_index,
        "A gallium arsenide photonic circuit",
        "Bearing in mind the thermal budget, we measured 3 dB loss per facet.",
    )
    assert match.is_candidate           # gallium matched, so tier-3 tags are live
    assert "bearings" not in match.tier3_tags


def test_a_real_bearing_still_matches(project_taxonomy, project_index):
    """`bearings` was promoted from tier 3 to tier 2 on 2026-09-30, so a real
    bearing hit is now an eligible industry match rather than a context tag."""
    match = run(
        project_taxonomy,
        project_index,
        "A gallium arsenide photonic circuit",
        "We inspected the roller bearing raceway for subsurface cracks.",
    )
    assert "bearings" in match.industries


def test_neural_network_transformer_still_hits_the_keyword(
    project_taxonomy, project_index
):
    """The pre-filter cannot disambiguate this; it is the LLM's job (see
    tests/test_industry_scoring.py). This test pins the handoff."""
    match = run(
        project_taxonomy,
        project_index,
        "A transformer architecture for quantum state tomography",
        "We train a transformer on simulated measurement records and compare with "
        "maximum-likelihood reconstruction.",
    )
    assert "power_grid" in match.groups
    assert match.is_candidate  # deliberately: the model gets to reject it


# --------------------------------------------------------------------------
# Tier rules
# --------------------------------------------------------------------------


def test_tier3_alone_never_creates_a_candidate(project_taxonomy, project_index):
    """Uses industries still inert after the 2026-09-30 promotion -- `cobalt` and
    `magnesium` are tier 1 now, so they would no longer prove anything here."""
    match = run(
        project_taxonomy,
        project_index,
        "Durability of shipping container coatings",
        "We measure wear on a shipping container and an industrial robot arm.",
    )
    assert not match.is_candidate
    assert match.tier3_tags == {}  # dropped, because there was nothing to tag


def test_tier3_tags_attach_once_the_paper_is_a_candidate(project_taxonomy, project_index):
    match = run(
        project_taxonomy,
        project_index,
        "A gallium arsenide device inspected by an industrial robot",
        "We grew gallium arsenide by molecular beam epitaxy; an industrial robot "
        "handled the wafers.",
    )
    assert "gallium" in match.industries
    assert "robot_deployment" in match.tier3_tags


def test_a_tier3_hit_does_not_suppress_its_group_tag(project_taxonomy, project_index):
    """Only a tier-1/2 specific match outranks the group tag; tier 3 is not a match.

    `titanium_sponge` was promoted to tier 1, so this uses a chemicals-group
    industry that is still inert.
    """
    match = run(
        project_taxonomy,
        project_index,
        "Gravimetry over an ore body",
        "An atom interferometer survey of an ore body during mineral exploration; "
        "samples also contained a vulcanization accelerator residue.",
    )
    assert "critical_minerals" in match.groups
    assert "rubber_chemicals" in match.tier3_tags


def test_a_specific_tier1_match_suppresses_its_group_tag(project_taxonomy, project_index):
    match = run(
        project_taxonomy,
        project_index,
        "Erbium-doped crystals surveyed during mineral exploration",
        "We characterise an Er:YSO quantum memory. Separately we note mineral "
        "exploration relevance and an ore body nearby.",
    )
    assert "rare_earth_refining" in match.industries
    assert "critical_minerals" not in match.groups


# --------------------------------------------------------------------------
# The case-sensitive escape hatch
# --------------------------------------------------------------------------


def test_gap_the_english_word_does_not_match_gap_the_compound(
    project_taxonomy, project_index
):
    """`GaP` is in `case_sensitive`, so "band gap" must not tag gallium refining."""
    match = run(
        project_taxonomy,
        project_index,
        "Band gap engineering in a semiconductor heterostructure",
        "We tune the energy gap and observe a spectral gap in the excitation spectrum.",
    )
    assert "gallium" not in match.industries


def test_gap_the_compound_still_matches_when_written_correctly(
    project_taxonomy, project_index
):
    match = run(
        project_taxonomy, project_index, "A GaP photonic waveguide", "Measured at 4 K."
    )
    assert "gallium" in match.industries


# --------------------------------------------------------------------------
# Secondary-category gate
# --------------------------------------------------------------------------


def test_gate_admits_a_quantum_paper_reporting_a_measurement(
    project_taxonomy, project_index
):
    assert passes_secondary_gate(
        project_taxonomy,
        project_index,
        "Spin qubit readout in a silicon device. We measure a qubit coherence time.",
    )


def test_gate_admits_a_NON_quantum_paper_reporting_a_measurement(
    project_taxonomy, project_index
):
    """The whole point of the 2026-09-30 change.

    Under the old `quantum_core` gate this ceramics paper was discarded for not
    mentioning quantum -- along with perovskite solar cells, core-loss
    measurements on electrical steel, and phonon transport in thermoelectrics.
    All of those bear on industries in this file.
    """
    assert passes_secondary_gate(
        project_taxonomy,
        project_index,
        "Thermal conductivity of sintered alumina ceramics measured by laser flash.",
    )


@pytest.mark.parametrize(
    "text",
    [
        "Perovskite tandem solar cells fabricated by vapour deposition.",
        "Core loss measured in grain-oriented electrical steel laminations.",
        "We demonstrate enhanced critical current in a coated conductor.",
        "Degradation of alkaline electrolyser membranes characterized over 500 hours.",
    ],
)
def test_gate_admits_the_applied_papers_the_old_gate_threw_away(
    project_taxonomy, project_index, text
):
    assert passes_secondary_gate(project_taxonomy, project_index, text)


@pytest.mark.parametrize(
    "text",
    [
        "A theoretical framework for thermal transport in disordered solids.",
        "We prove a bound on the conductivity of random resistor networks.",
        "Numerical simulation of grain growth using a phase-field model.",
    ],
)
def test_gate_still_rejects_work_with_no_measurement(
    project_taxonomy, project_index, text
):
    """The gate is not gone, it changed axis: theory with no measurement is out.

    This is what stops eight whole cond-mat/physics subcategories from flooding
    triage now that the quantum requirement is lifted.
    """
    assert not passes_secondary_gate(project_taxonomy, project_index, text)


# --------------------------------------------------------------------------
# Run-level report
# --------------------------------------------------------------------------


class FakePaper:
    def __init__(self, arxiv_id, title, abstract):
        self.arxiv_id = arxiv_id
        self.title = title
        self.abstract = abstract


def test_match_papers_counts_candidates_by_tier(project_taxonomy, project_index):
    papers = [
        FakePaper("1", "A gallium arsenide quantum dot", "Grown by molecular beam epitaxy."),
        FakePaper("2", "Operando NV magnetometry", "Imaging lithium plating in a cell."),
        FakePaper("3", "Entanglement entropy bounds", "A purely formal derivation."),
    ]
    report = match_papers(project_taxonomy, project_index, papers)

    assert report.total == 3
    assert len(report.candidates) == 2
    assert report.tier_counts[1] == 1   # the GaAs paper
    assert report.tier_counts[2] == 1   # the battery-group paper
    assert report.industry_counts["gallium"] == 1
    assert report.group_counts["battery_chain"] == 1


def test_most_matched_reports_display_names(project_taxonomy, project_index):
    papers = [
        FakePaper(str(n), "A gallium arsenide quantum dot", "MBE-grown.") for n in range(3)
    ]
    report = match_papers(project_taxonomy, project_index, papers)
    top = dict(report.most_matched(project_taxonomy, limit=5))
    assert top["Gallium refining"] == 3


def test_noisy_keywords_excludes_terms_that_cannot_tag_anything(
    project_taxonomy, project_index
):
    """`quantum` is gate vocabulary; it is meant to fire everywhere and is not noise."""
    papers = [FakePaper(str(n), "A quantum study", "quantum quantum") for n in range(10)]
    report = match_papers(project_taxonomy, project_index, papers)
    assert report.keyword_counts["quantum"] == 10
    assert "quantum" not in {kw for kw, _, _ in report.noisy_keywords(min_share=0.0)}


def test_keyword_counts_cover_non_candidates_too(project_taxonomy, project_index):
    """Noise triage needs hits from papers that never became candidates."""
    papers = [FakePaper("1", "Entanglement entropy", "A formal bound on entanglement.")]
    report = match_papers(project_taxonomy, project_index, papers)
    assert report.candidates == []
    assert report.keyword_counts["entanglement"] == 1
