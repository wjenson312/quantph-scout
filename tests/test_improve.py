"""The self-improvement loop: proposals, thresholds, and what may self-apply.

The behaviour that matters most here is what the system is NOT allowed to do to
itself. Only `new_discovery_signal` may be applied automatically; a system that
rewrites its own scoring rules or industry list cannot be audited from a diff.
"""

from dataclasses import dataclass

import pytest
import yaml

from scout.improve import (
    AUTO_APPLY_TYPES,
    THRESHOLD,
    Feedback,
    ImprovementResult,
    Proposal,
    apply_auto,
    detect,
    escalate,
    load_feedback,
    load_proposals,
    merge,
    save_proposals,
)


# --- minimal stand-ins for the run outputs the detector reads ----------------


@dataclass
class FakeTag:
    primary_pipeline: str = "sensing"
    confidence: str = "high"
    signal: str = "measured 9.8 pT sensitivity"


@dataclass
class FakePaper:
    arxiv_id: str


@dataclass
class FakeTagged:
    paper: FakePaper
    tag: FakeTag

    @property
    def dropped(self) -> bool:
        return self.tag.primary_pipeline == "none"


@dataclass
class FakeItem:
    check_id: str
    score: int
    note: str = ""


@dataclass
class FakeScore:
    checks: list


@dataclass
class FakeScored:
    arxiv_id: str
    score: FakeScore


@dataclass
class FakeResult:
    best_industry: str
    relevance: int = 3


@dataclass
class FakeMatch:
    arxiv_id: str
    result: FakeResult
    kept: bool
    scored: object = None


def _tagged(n, confidence="high", pipeline="sensing"):
    return FakeTagged(FakePaper(f"2609.{n:05d}"), FakeTag(pipeline, confidence))


def _detect(**kwargs):
    base = dict(
        today="2026-09-30", tagged=[], scored=[], matched=[], selected_ids=set(),
        taxonomy=None, pipelines=None, feedback=[],
    )
    base.update(kwargs)
    return detect(**base)


# --------------------------------------------------------------------------
# What may and may not self-apply
# --------------------------------------------------------------------------


def test_only_discovery_signals_may_self_apply():
    """The whole safety property of this module in one assertion."""
    assert AUTO_APPLY_TYPES == {"new_discovery_signal"}


@pytest.mark.parametrize(
    "kind",
    ["new_pipeline", "new_industry", "retire_industry", "scoring_change", "prompt_change"],
)
def test_structural_changes_are_escalated_never_applied(tmp_path, kind):
    proposal = Proposal(
        id="x", type=kind, proposal="do a structural thing", occurrences=99
    )
    pipelines = tmp_path / "pipelines.yaml"
    pipelines.write_text("pipelines:\n  - id: sensing\n    discovery_signals:\n      - a\n")
    before = pipelines.read_text()

    assert apply_auto([proposal], pipelines, dry_run=False) == []
    assert pipelines.read_text() == before, "config must be untouched"
    assert escalate([proposal]) == [proposal]
    assert proposal.status == "pending"


def test_a_discovery_signal_below_the_threshold_is_not_applied(tmp_path):
    pipelines = tmp_path / "pipelines.yaml"
    pipelines.write_text("pipelines:\n  - id: sensing\n    discovery_signals:\n      - a\n")
    proposal = Proposal(
        id="s", type="new_discovery_signal", target="sensing",
        proposal="Add ... covering: new thing", occurrences=THRESHOLD - 1,
    )
    assert apply_auto([proposal], pipelines, dry_run=False) == []
    assert "new thing" not in pipelines.read_text()


def test_a_discovery_signal_at_the_threshold_is_applied(tmp_path):
    pipelines = tmp_path / "pipelines.yaml"
    pipelines.write_text(
        "pipelines:\n  - id: sensing\n    discovery_signals:\n      - existing\n"
    )
    proposal = Proposal(
        id="s", type="new_discovery_signal", target="sensing",
        proposal="Add a discovery signal to pipeline 'sensing' covering: new thing",
        occurrences=THRESHOLD,
    )
    applied = apply_auto([proposal], pipelines, dry_run=False)

    assert applied == [proposal]
    assert proposal.status == "applied"
    text = pipelines.read_text()
    assert "new thing" in text
    assert "existing" in text, "the existing signal must survive"
    # The edit has to be attributable: a reader needs to see what the agent added
    # to its own config, and why.
    assert "auto-added" in text
    assert proposal.id in text


def test_a_dry_run_applies_nothing(tmp_path):
    pipelines = tmp_path / "pipelines.yaml"
    pipelines.write_text("pipelines:\n  - id: sensing\n    discovery_signals:\n      - a\n")
    before = pipelines.read_text()
    proposal = Proposal(
        id="s", type="new_discovery_signal", target="sensing",
        proposal="covering: x", occurrences=THRESHOLD,
    )
    assert apply_auto([proposal], pipelines, dry_run=True) == []
    assert pipelines.read_text() == before


def test_applying_to_a_missing_pipeline_warns_rather_than_corrupting(tmp_path):
    pipelines = tmp_path / "pipelines.yaml"
    pipelines.write_text("pipelines:\n  - id: sensing\n    discovery_signals:\n      - a\n")
    before = pipelines.read_text()
    proposal = Proposal(
        id="s", type="new_discovery_signal", target="does_not_exist",
        proposal="covering: x", occurrences=THRESHOLD,
    )
    assert apply_auto([proposal], pipelines, dry_run=False) == []
    assert pipelines.read_text() == before


# --------------------------------------------------------------------------
# Merging and occurrence counting
# --------------------------------------------------------------------------


def test_a_repeat_proposal_increments_occurrences_once_per_run():
    """Ten papers hitting one gap in a single week is one occurrence, or an unusual
    week would trip the auto-apply threshold on its own."""
    existing = [Proposal(id="gap", type="new_industry", proposal="p", occurrences=1)]
    found = [
        Proposal(id="gap", type="new_industry", proposal="p", evidence=["a", "b", "c"])
    ]
    merged = merge(existing, found)
    assert len(merged) == 1
    assert merged[0].occurrences == 2


def test_a_new_proposal_is_added():
    merged = merge([], [Proposal(id="new", type="new_industry", proposal="p",
                                occurrences=1)])
    assert [p.id for p in merged] == ["new"]


def test_a_rejected_proposal_is_not_resurrected():
    """The reader already answered; re-proposing it weekly would be nagging."""
    existing = [
        Proposal(id="gap", type="new_industry", proposal="p", occurrences=5,
                 status="rejected")
    ]
    merged = merge(existing, [Proposal(id="gap", type="new_industry", proposal="p")])
    assert merged[0].occurrences == 5
    assert merged[0].status == "rejected"


def test_an_applied_proposal_is_not_reapplied():
    existing = [
        Proposal(id="s", type="new_discovery_signal", proposal="p", occurrences=3,
                 status="applied")
    ]
    merged = merge(existing, [Proposal(id="s", type="new_discovery_signal", proposal="p")])
    assert merged[0].occurrences == 3


def test_evidence_accumulates_without_duplicates():
    existing = [Proposal(id="g", type="new_industry", proposal="p", evidence=["a"])]
    merged = merge(existing, [Proposal(id="g", type="new_industry", proposal="p",
                                      evidence=["a", "b"])])
    assert merged[0].evidence == ["a", "b"]


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_repeated_low_confidence_tags_propose_a_new_signal():
    """Three papers guessed into one pipeline means the pipeline's signal list does
    not describe what they did."""
    tagged = [_tagged(n, confidence="low") for n in range(1, 5)]
    found = _detect(tagged=tagged)
    signals = [p for p in found if p.type == "new_discovery_signal"]
    assert len(signals) == 1
    assert signals[0].target == "sensing"
    assert signals[0].occurrences == 1


def test_one_low_confidence_tag_proposes_nothing():
    assert _detect(tagged=[_tagged(1, confidence="low")]) == []


def test_a_shortlisted_paper_with_no_industry_proposes_a_new_industry():
    matched = [FakeMatch("2609.00001", FakeResult("", 0), kept=False)]
    found = _detect(matched=matched)
    assert [p.type for p in found] == ["new_industry"]
    assert found[0].evidence == ["2609.00001"]


def test_an_inert_readiness_check_proposes_a_scoring_change():
    """Every paper answering identically means the check contributes a constant."""
    scored = [
        FakeScored(
            f"2609.{n:05d}",
            FakeScore([FakeItem("reproducibility", 1, "not stated")]),
        )
        for n in range(12)
    ]
    found = _detect(scored=scored)
    assert any(p.type == "scoring_change" for p in found)
    assert any("reproducibility" in p.proposal for p in found)


def test_a_check_that_varies_proposes_nothing():
    scored = [
        FakeScored(f"2609.{n:05d}", FakeScore([FakeItem("scalability", n % 3, "x")]))
        for n in range(12)
    ]
    assert not any(p.type == "scoring_change" for p in _detect(scored=scored))


def test_consistently_low_ratings_propose_retiring_an_industry():
    matched = [
        FakeMatch(f"2609.{n:05d}", FakeResult("tunnel_boring"), kept=True)
        for n in range(3)
    ]
    feedback = [Feedback(f"2609.{n:05d}", rating=1) for n in range(3)]
    found = _detect(matched=matched, feedback=feedback)
    assert any(p.type == "retire_industry" for p in found)


def test_a_single_low_rating_proposes_nothing():
    """One low score is an opinion about a paper, not a signal about the system."""
    feedback = [Feedback("2609.00001", rating=1)]
    assert _detect(feedback=feedback) == []


# --------------------------------------------------------------------------
# State files
# --------------------------------------------------------------------------


def test_proposals_round_trip(tmp_path):
    path = tmp_path / "proposals.yaml"
    original = [
        Proposal(id="a", type="new_industry", proposal="p", evidence=["x"],
                 first_seen="2026-09-30", occurrences=2, note="why")
    ]
    save_proposals(path, original)
    reloaded = load_proposals(path)
    assert len(reloaded) == 1
    assert reloaded[0].id == "a"
    assert reloaded[0].occurrences == 2
    assert reloaded[0].note == "why"


def test_a_missing_proposals_file_is_not_an_error(tmp_path):
    assert load_proposals(tmp_path / "absent.yaml") == []


def test_a_corrupt_proposals_file_does_not_fail_the_run(tmp_path):
    """The improvement layer's own bookkeeping must never sink a digest."""
    path = tmp_path / "proposals.yaml"
    path.write_text("proposals: [\n  - broken")
    assert load_proposals(path) == []


def test_the_saved_file_is_valid_yaml_with_a_header(tmp_path):
    path = tmp_path / "proposals.yaml"
    save_proposals(path, [Proposal(id="a", type="new_industry", proposal="p")])
    text = path.read_text()
    assert text.lstrip().startswith("#")
    assert isinstance(yaml.safe_load(text)["proposals"], list)


def test_feedback_loads_and_ignores_out_of_range_ratings(tmp_path):
    path = tmp_path / "feedback.yaml"
    path.write_text(
        "ratings:\n"
        '  - {arxiv_id: "a", rating: 4}\n'
        '  - {arxiv_id: "b", rating: 9}\n'
        '  - {arxiv_id: "c", rating: "x"}\n'
    )
    loaded = load_feedback(path)
    assert [f.arxiv_id for f in loaded] == ["a"]


def test_a_missing_feedback_file_is_not_an_error(tmp_path):
    assert load_feedback(tmp_path / "absent.yaml") == []


def test_the_result_summary_reads_cleanly():
    result = ImprovementResult(
        proposals=[Proposal(id="a", type="new_industry", proposal="p")],
        applied=[], escalated=[],
    )
    assert "1 proposals tracked" in result.summary()
