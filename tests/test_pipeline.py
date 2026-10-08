"""Stage wiring, selection, digest rendering, prompts, and the run lock.

The model is stubbed out: these tests prove the plumbing without spending money.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from scout import db
from scout.budget import apply_guard, project
from scout.config import load_config
from scout.digest import DigestData, Entry, build_entries, render_digest, render_json
from scout.industries import load_taxonomy
from scout.ingest import Paper, select_new_submissions
from scout.lock import LockBusy, RunLock
from scout.llm import _sleep_for, gather_limited
from scout.prefilter import apply_prefilter
from scout.prompts import get_prompt, split_prompt
from scout.schemas import FinalAssessment
from scout.select import Pair, select
from scout.batching import align, chunk
from scout.match import (
    MatchOutcome,
    candidate_industries,
    eligible_industries,
    group_by_pipeline,
    match_papers,
    render_industry_list,
)
from scout.pipelines import load_pipelines
from scout.readiness import ReadinessReport, ScoredPaper, score_papers
from scout.schemas import (
    PipelineTag,
    PipelineTagBatch,
    ReadinessBatch,
    ReadinessItem,
    ReadinessScore,
    TriageBatch,
    TriageItem,
)
from scout.tagging import TagOutcome, tag_papers
from tests.test_schemas import VALID_FINAL


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def taxonomy(cfg):
    return load_taxonomy(cfg.taxonomy_path)


@pytest.fixture
def pipeline_set(cfg):
    return load_pipelines(cfg.pipelines_path)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def test_split_prompt_extracts_both_sections():
    system, user = split_prompt("# T\n\n## System\n\nBe strict.\n\n## User\n\nTitle: {title}\n")
    assert system == "Be strict."
    assert user == "Title: {title}"


def test_split_prompt_rejects_a_file_missing_a_section():
    from scout.config import ConfigError

    with pytest.raises(ConfigError):
        split_prompt("## System\n\nonly a system prompt")


@pytest.mark.parametrize(
    "name,placeholders",
    [
        ("tag", ["n", "papers"]),
        ("readiness", ["n", "papers"]),
        ("match", ["n", "papers"]),
        ("final", ["arxiv_id", "title", "categories", "abstract", "source_block",
                   "triage_industry", "triage_reason"]),
        ("verify", ["arxiv_id", "title", "industry_id", "what_they_found",
                    "why_it_matters", "the_catch", "source"]),
    ],
)
def test_shipped_prompts_load_and_expose_the_expected_placeholders(cfg, name, placeholders):
    system, user = get_prompt(cfg, name)
    assert system and len(system) > 200
    for placeholder in placeholders:
        assert "{" + placeholder + "}" in user, f"{name} is missing {{{placeholder}}}"


def test_prompts_live_on_disk_not_in_code(cfg):
    """A prompt edit must take effect without touching Python."""
    for name in ("tag", "readiness", "match", "final", "verify"):
        assert (cfg.prompt_dir / f"{name}.md").exists()


def test_superseded_prompts_are_gone(cfg):
    """A stale copy on disk would be loaded by nothing but read by humans."""
    for name in ("analyze", "critic", "industry", "triage"):
        assert not (cfg.prompt_dir / f"{name}.md").exists(), name


def test_match_system_prompt_takes_the_industry_list_and_tier_definitions(cfg):
    system, _ = get_prompt(cfg, "match")
    assert "{industry_list}" in system
    assert "{tier_definitions}" in system


def test_tag_system_prompt_takes_the_pipeline_list(cfg):
    system, _ = get_prompt(cfg, "tag")
    assert "{pipeline_list}" in system


def test_readiness_prompt_takes_the_checks_and_explains_the_incumbent_rule(cfg):
    """Scored literally, operating_conditions gives every superconducting and
    quantum paper a 0 by definition. The prompt must say to compare against the
    pipeline's incumbent, or whole industries silently vanish from the report."""
    system, _ = get_prompt(cfg, "readiness")
    assert "{readiness_checks}" in system
    assert "incumbent" in system.lower()
    assert "not stated" in system.lower()


def test_tag_prompt_tells_the_model_to_prefer_a_low_confidence_guess_over_none(cfg):
    """A paper wrongly dropped at Stage A is gone for good; one wrongly passed on
    costs a fraction of a cent."""
    system, _ = get_prompt(cfg, "tag")
    assert "low" in system.lower()
    assert "gone for good" in system.lower()


def test_final_prompt_demands_a_concrete_catch_in_plain_language(cfg):
    """Both are spec requirements, so they get a test rather than a code review."""
    system, _ = get_prompt(cfg, "final")
    lowered = system.lower()
    assert "the catch" in lowered
    assert "no jargon" in lowered
    assert "twelve-year-old" in lowered


def test_verify_prompt_allows_finding_nothing_wrong(cfg):
    """A critic that must find fault invents fault. The prompt has to say so."""
    system, _ = get_prompt(cfg, "verify")
    lowered = system.lower()
    assert "do not invent problems" in lowered
    assert "valid" in lowered


def test_tier3_is_never_offered_to_the_model(taxonomy):
    """Tier 3 stays in the YAML but must not reach a prompt -- it is not eligible."""
    listing = render_industry_list(taxonomy)
    tier3_ids = [i.id for i in taxonomy.industries.values() if i.tier == 3]
    assert tier3_ids, "the fixture should still contain tier-3 industries"
    for industry_id in tier3_ids:
        assert industry_id not in listing
    assert "tier 3" not in listing.lower()


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Batched stages with a stubbed model: tag (A), readiness (B), match
# --------------------------------------------------------------------------


class FakeClient:
    """Stands in for ModelClient: returns queued results, records the calls."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def structured(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _paper(n: int, **overrides) -> Paper:
    fields = dict(
        arxiv_id=f"2609.0000{n}",
        version="v1",
        title=f"Paper {n}",
        abstract="We measured a thing in a fabricated device.",
        authors=["A. Author", "B. Author"],
        primary_category="quant-ph",
        categories=["quant-ph", "cond-mat.mtrl-sci"],
        published_utc=datetime(2026, 9, 20, tzinfo=timezone.utc).isoformat(),
        updated_utc=datetime(2026, 9, 20, tzinfo=timezone.utc).isoformat(),
        abs_url=f"https://arxiv.org/abs/2609.0000{n}",
        pdf_url=f"https://arxiv.org/pdf/2609.0000{n}",
        comment=None,
    )
    fields.update(overrides)
    return Paper(**fields)


def _tag(ref: int, *, pipeline="sensing", secondary="", confidence="high") -> PipelineTag:
    return PipelineTag(
        ref=ref, primary_pipeline=pipeline, secondary_pipeline=secondary,
        confidence=confidence, signal="measured 9.8 pT sensitivity",
    )


def _tag_batch(*tags) -> PipelineTagBatch:
    return PipelineTagBatch(results=list(tags))


def _matched(n: int, *, industry="gallium", relevance=3, pipeline="sensing",
             confidence="high", kept=True) -> MatchOutcome:
    """Readiness now scores industry-matched papers, so its input is a MatchOutcome."""
    return MatchOutcome(
        tagged=TagOutcome(paper=_paper(n), tag=_tag(n, pipeline=pipeline,
                                                   confidence=confidence)),
        result=TriageItem(ref=n, best_industry=industry, relevance=relevance,
                          experimental=True, reason="ok"),
        kept=kept,
    )


def _score(ref: int, scores, *, limiting="scalability", concreteness=2) -> ReadinessScore:
    """`scores` maps check_id -> 0/1/2 and must cover every shipped check."""
    return ReadinessScore(
        ref=ref,
        checks=[
            ReadinessItem(check_id=cid, score=s, note="because")
            for cid, s in scores.items()
        ],
        most_limiting=limiting,
        concreteness=concreteness,
    )


def _all_checks(pipeline_set, value=2):
    return {cid: value for cid in pipeline_set.check_ids()}


# --- Stage A: pipeline tagging -------------------------------------------------


async def test_tagging_assigns_a_pipeline_to_every_paper(cfg, pipeline_set):
    papers = [_paper(1), _paper(2)]
    client = FakeClient([_tag_batch(_tag(1), _tag(2, pipeline="photonics"))])

    tagged, failures = await tag_papers(client, cfg, pipeline_set, papers)

    assert [t.tag.primary_pipeline for t in tagged] == ["sensing", "photonics"]
    assert failures == []


async def test_many_papers_go_in_one_tagging_request(cfg, pipeline_set):
    """The whole point of batching: 9 papers must not be 9 calls."""
    papers = [_paper(n) for n in range(1, 10)]
    client = FakeClient([_tag_batch(*(_tag(n) for n in range(1, 10)))])

    await tag_papers(client, cfg, pipeline_set, papers)

    assert len(client.calls) == 1
    user = client.calls[0]["user"]
    for paper in papers:
        assert paper.title in user


async def test_a_none_tag_is_recorded_but_marked_dropped(cfg, pipeline_set):
    """Dropped papers are still written down, so the improvement pass can see
    what was discarded and learn from it."""
    client = FakeClient([_tag_batch(_tag(1, pipeline="none"))])

    tagged, _ = await tag_papers(client, cfg, pipeline_set, [_paper(1)])

    assert len(tagged) == 1
    assert tagged[0].dropped is True
    assert tagged[0].pipeline_ids == ()


async def test_an_invented_pipeline_id_is_refused(cfg, pipeline_set):
    client = FakeClient([_tag_batch(_tag(1, pipeline="warp_drive"))])

    tagged, _ = await tag_papers(client, cfg, pipeline_set, [_paper(1)])

    assert tagged[0].tag.primary_pipeline == "none"
    assert tagged[0].dropped is True


async def test_an_invented_secondary_pipeline_is_dropped_but_the_primary_survives(
    cfg, pipeline_set
):
    client = FakeClient([_tag_batch(_tag(1, pipeline="sensing", secondary="nonsense"))])

    tagged, _ = await tag_papers(client, cfg, pipeline_set, [_paper(1)])

    assert tagged[0].tag.primary_pipeline == "sensing"
    assert tagged[0].tag.secondary_pipeline == ""
    assert tagged[0].dropped is False


async def test_a_secondary_that_repeats_the_primary_is_cleared(cfg, pipeline_set):
    client = FakeClient([_tag_batch(_tag(1, pipeline="sensing", secondary="sensing"))])

    tagged, _ = await tag_papers(client, cfg, pipeline_set, [_paper(1)])

    assert tagged[0].tag.secondary_pipeline == ""
    assert tagged[0].pipeline_ids == ("sensing",)


async def test_tagging_sends_abstracts_and_the_rendered_pipeline_list(cfg, pipeline_set):
    client = FakeClient([_tag_batch(_tag(1))])
    await tag_papers(client, cfg, pipeline_set, [_paper(1)])

    call = client.calls[0]
    assert call["model"] == cfg.models.tag
    assert call["output_format"] is PipelineTagBatch
    assert "sensing" in call["system"]
    assert "{pipeline_list}" not in call["system"]
    assert "arxiv.org" not in call["user"]  # no full text at this stage


async def test_tagging_an_empty_list_makes_no_calls(cfg, pipeline_set):
    client = FakeClient([])
    tagged, failures = await tag_papers(client, cfg, pipeline_set, [])
    assert (tagged, failures, client.calls) == ([], [], [])


# --- the shared batching machinery -------------------------------------------


def test_chunking_splits_on_the_configured_size():
    papers = [_paper(n) for n in range(1, 8)]
    assert [len(c) for c in chunk(papers, 3)] == [3, 3, 1]
    assert [len(c) for c in chunk(papers, 100)] == [7]
    # A nonsense size must not produce empty batches or an infinite loop.
    assert [len(c) for c in chunk(papers, 0)] == [1] * 7


def test_align_matches_by_ref_not_by_position():
    """A reordered response must not hand paper 1's verdict to paper 3.

    This is the failure mode that makes batching dangerous, so it is pinned on the
    shared helper rather than once per stage.
    """
    papers = [_paper(1), _paper(2), _paper(3)]
    results = [_tag(3, pipeline="photonics"), _tag(1, pipeline="sensing"),
               _tag(2, pipeline="logic_memory")]

    paired, unanswered = align(papers, results)

    assert unanswered == []
    assert {p.arxiv_id: t.primary_pipeline for p, t in paired} == {
        "2609.00001": "sensing",
        "2609.00002": "logic_memory",
        "2609.00003": "photonics",
    }


def test_align_reports_a_paper_that_was_never_answered_for():
    papers = [_paper(1), _paper(2)]
    paired, unanswered = align(papers, [_tag(1)])
    assert [p.arxiv_id for p, _ in paired] == ["2609.00001"]
    assert [p.arxiv_id for p in unanswered] == ["2609.00002"]


def test_align_ignores_a_ref_outside_the_range():
    papers = [_paper(1)]
    paired, unanswered = align(papers, [_tag(7)])
    assert paired == []
    assert len(unanswered) == 1


def test_align_keeps_the_first_answer_for_a_duplicated_ref():
    """A model that answered twice for one paper has not told us which it meant."""
    papers = [_paper(1)]
    paired, _ = align(papers, [_tag(1, pipeline="sensing"), _tag(1, pipeline="photonics")])
    assert paired[0][1].primary_pipeline == "sensing"


async def test_a_paper_the_batch_skipped_is_retried_individually(cfg, pipeline_set):
    papers = [_paper(1), _paper(2), _paper(3)]
    client = FakeClient(
        [
            _tag_batch(_tag(1), _tag(3)),            # paper 2 unanswered
            _tag_batch(_tag(1, pipeline="photonics")),  # the individual retry
        ]
    )
    tagged, failures = await tag_papers(client, cfg, pipeline_set, papers)

    assert len(client.calls) == 2
    assert failures == []
    by_id = {t.paper.arxiv_id: t for t in tagged}
    assert set(by_id) == {"2609.00001", "2609.00002", "2609.00003"}
    # The retried paper keeps its own verdict, not its neighbour's.
    assert by_id["2609.00002"].tag.primary_pipeline == "photonics"


async def test_a_paper_never_answered_for_is_reported_as_a_failure(cfg, pipeline_set):
    papers = [_paper(1), _paper(2)]
    client = FakeClient([_tag_batch(_tag(1)), _tag_batch()])  # retry returns nothing

    tagged, failures = await tag_papers(client, cfg, pipeline_set, papers)

    assert [t.paper.arxiv_id for t in tagged] == ["2609.00001"]
    assert [p.arxiv_id for p, _ in failures] == ["2609.00002"]


async def test_a_failed_batch_reports_every_paper_in_it(cfg, pipeline_set):
    """Not one failure standing in for the batch -- otherwise the failure-rate
    guard undercounts and a total outage looks like a quiet week."""
    papers = [_paper(1), _paper(2)]
    client = FakeClient([RuntimeError("model exploded")])

    tagged, failures = await tag_papers(client, cfg, pipeline_set, papers)

    assert tagged == []
    assert len(failures) == 2


# --- Stage B: readiness scoring ----------------------------------------------


async def test_readiness_scores_and_shortlists(cfg, pipeline_set):
    matched = [_matched(1), _matched(2)]
    client = FakeClient(
        [
            ReadinessBatch(
                results=[
                    _score(1, _all_checks(pipeline_set, 2)),   # 10/10
                    _score(2, _all_checks(pipeline_set, 1)),   # 5/10
                ]
            )
        ]
    )
    report, failures = await score_papers(client, cfg, pipeline_set, matched)

    assert failures == []
    assert [p.total for p in report.scored] == [10, 5]
    assert report.max_score == 10
    assert [p.arxiv_id for p in report.shortlist] == ["2609.00001", "2609.00002"]


async def test_readiness_shortlist_is_capped(cfg, pipeline_set):
    from dataclasses import replace

    tight = replace(cfg, readiness=replace(cfg.readiness, shortlist_size=1))
    matched = [_matched(n) for n in (1, 2)]
    client = FakeClient(
        [
            ReadinessBatch(
                results=[
                    _score(1, _all_checks(pipeline_set, 1)),
                    _score(2, _all_checks(pipeline_set, 2)),
                ]
            )
        ]
    )
    report, _ = await score_papers(client, tight, pipeline_set, matched)

    assert len(report.shortlist) == 1
    assert report.shortlist[0].arxiv_id == "2609.00002"  # the higher score survives


async def test_an_incomplete_readiness_answer_is_retried(cfg, pipeline_set):
    """A partial answer would silently lower the total and push the paper down the
    ranking for no real reason, so it is rejected rather than scored."""
    matched = [_matched(1)]
    partial = {cid: 2 for cid in list(pipeline_set.check_ids())[:2]}
    client = FakeClient(
        [
            ReadinessBatch(results=[_score(1, partial)]),
            ReadinessBatch(results=[_score(1, _all_checks(pipeline_set, 2))]),
        ]
    )
    report, failures = await score_papers(client, cfg, pipeline_set, matched)

    assert len(client.calls) == 2
    assert failures == []
    assert report.scored[0].total == 10


async def test_readiness_carries_the_pipeline_so_conditions_are_judged_in_context(
    cfg, pipeline_set
):
    """`operating_conditions` is scored against the pipeline's incumbent, so the
    prompt has to say which pipeline and what it competes with."""
    matched = [_matched(1, pipeline="energy_transport")]
    client = FakeClient(
        [ReadinessBatch(results=[_score(1, _all_checks(pipeline_set, 2))])]
    )
    await score_papers(client, cfg, pipeline_set, matched)

    user = client.calls[0]["user"]
    assert "energy_transport" in user
    assert "Competes with" in user
    system = client.calls[0]["system"]
    assert "operating_conditions" in system
    assert "incumbent" in system.lower()


def test_the_weakest_check_prefers_a_stated_barrier_over_silence(pipeline_set):
    """Both score 0, but one was measured and one was merely not mentioned. The
    report's catch should name the real barrier."""
    score = ReadinessScore(
        ref=1,
        checks=[
            ReadinessItem(check_id="operating_conditions", score=0, note="not stated"),
            ReadinessItem(check_id="scalability", score=0, note="single flake only"),
            ReadinessItem(check_id="integration", score=2, note="standard fab"),
            ReadinessItem(check_id="performance_margin", score=2, note="10x"),
            ReadinessItem(check_id="reproducibility", score=1, note="not stated"),
        ],
        most_limiting="reproducibility",   # not actually the lowest
        concreteness=2,
    )
    paper = ScoredPaper(matched=_matched(1), score=score)
    assert paper.total == 5
    assert paper.weakest() == "scalability"


def test_the_models_own_choice_of_limiting_check_is_honoured_when_coherent(pipeline_set):
    score = ReadinessScore(
        ref=1,
        checks=[
            ReadinessItem(check_id="operating_conditions", score=0, note="needs 10 mK"),
            ReadinessItem(check_id="scalability", score=0, note="one sample"),
            ReadinessItem(check_id="integration", score=2, note="ok"),
            ReadinessItem(check_id="performance_margin", score=2, note="ok"),
            ReadinessItem(check_id="reproducibility", score=2, note="ok"),
        ],
        most_limiting="operating_conditions",
        concreteness=2,
    )
    paper = ScoredPaper(matched=_matched(1), score=score)
    assert paper.weakest() == "operating_conditions"


def test_an_inert_check_is_visible_in_the_distribution(pipeline_set):
    """"not stated" scores 1, so a check every paper ignores contributes a
    constant. The run logs this so it can be noticed rather than assumed."""
    papers = [
        ScoredPaper(
            matched=_matched(n),
            score=_score(n, {**_all_checks(pipeline_set, 2), "reproducibility": 1}),
        )
        for n in range(1, 4)
    ]
    report = ReadinessReport(scored=papers, max_score=10)
    assert "reproducibility 1.00" in report.describe_distribution()


# --- industry matching --------------------------------------------------------


async def test_matching_assigns_an_industry_and_drops_weak_fits(
    cfg, taxonomy, pipeline_set
):
    tagged = [TagOutcome(paper=_paper(n), tag=_tag(n)) for n in (1, 2)]
    client = FakeClient(
        [
            TriageBatch(
                results=[
                    TriageItem(ref=1, best_industry="gallium", relevance=3,
                               experimental=True, reason="ok"),
                    TriageItem(ref=2, best_industry="gallium", relevance=1,
                               experimental=True, reason="thin"),
                ]
            )
        ]
    )
    matched, failures = await match_papers(
        client, cfg, taxonomy, pipeline_set, tagged
    )

    assert failures == []
    assert [m.kept for m in matched] == [True, False]


async def test_matching_refuses_an_invented_industry(cfg, taxonomy, pipeline_set):
    client = FakeClient(
        [
            TriageBatch(
                results=[
                    TriageItem(ref=1, best_industry="unobtainium", relevance=3,
                               experimental=True, reason="x")
                ]
            )
        ]
    )
    matched, _ = await match_papers(
        client, cfg, taxonomy, pipeline_set,
        [TagOutcome(paper=_paper(1), tag=_tag(1))],
    )
    assert matched[0].result.best_industry == ""
    assert matched[0].kept is False


def test_the_pipeline_narrows_the_industry_candidates(taxonomy, pipeline_set):
    """The point of tagging first: choose among a handful, not 78."""
    everything = eligible_industries(taxonomy)
    narrowed = candidate_industries(
        taxonomy, pipeline_set, ("energy_transport",), min_candidates=2
    )
    assert 0 < len(narrowed) <= len(everything)


def test_a_thin_overlap_falls_back_to_the_full_list(taxonomy, pipeline_set):
    """A narrowing that leaves almost nothing means the pipeline vocabulary did not
    describe that corner of the taxonomy -- not that only two industries fit. The
    right answer might be outside it, so widen rather than force a bad choice."""
    everything = eligible_industries(taxonomy)
    narrowed = candidate_industries(
        taxonomy, pipeline_set, ("design_tools",), min_candidates=999
    )
    assert len(narrowed) == len(everything)


def test_an_untagged_paper_sees_the_full_industry_list(taxonomy, pipeline_set):
    assert len(
        candidate_industries(taxonomy, pipeline_set, (), min_candidates=6)
    ) == len(eligible_industries(taxonomy))


async def test_matching_an_empty_input_makes_no_calls(cfg, taxonomy, pipeline_set):
    client = FakeClient([])
    matched, failures = await match_papers(client, cfg, taxonomy, pipeline_set, [])
    assert (matched, failures, client.calls) == ([], [], [])


def test_papers_are_batched_by_pipeline_so_narrowing_stays_narrow():
    """A mixed batch's candidate list is the union of what every paper allows, which
    for a mixed batch is most of the taxonomy -- so the narrowing stops narrowing."""
    tagged = [
        TagOutcome(paper=_paper(1), tag=_tag(1, pipeline="sensing")),
        TagOutcome(paper=_paper(2), tag=_tag(2, pipeline="photonics")),
        TagOutcome(paper=_paper(3), tag=_tag(3, pipeline="sensing")),
    ]
    groups = dict(group_by_pipeline(tagged))
    assert sorted(groups) == ["photonics", "sensing"]
    assert len(groups["sensing"]) == 2


# --------------------------------------------------------------------------
# Selection: one industry per paper, no duplicate industries, at most 10
# --------------------------------------------------------------------------


def _pair(
    paper: str, industry: str, relevance=3, disruption=3, readiness=8,
    confidence="high", pipeline_id="sensing",
) -> Pair:
    return Pair(
        arxiv_id=paper, industry_id=industry,
        relevance=relevance, disruption=disruption, readiness=readiness,
        confidence=confidence, pipeline_id=pipeline_id,
    )


def test_selection_gives_each_paper_exactly_one_industry():
    """A paper offered under two industries must appear once, under the better one."""
    result = select(
        [_pair("a", "gallium", disruption=5), _pair("a", "germanium", disruption=2)],
        limit=10,
    )
    assert len(result.selected) == 1
    assert result.selected[0].industry_id == "gallium"


def test_no_industry_appears_twice():
    result = select(
        [
            _pair("a", "gallium", disruption=5),
            _pair("b", "gallium", disruption=4),
            _pair("c", "germanium", disruption=1),
        ],
        limit=10,
    )
    industries = [p.industry_id for p in result.selected]
    assert industries == sorted(set(industries), key=industries.index)
    assert len(industries) == len(set(industries))
    assert [p.arxiv_id for p in result.selected] == ["a", "c"]


def test_the_paper_crowded_out_of_an_industry_is_reported():
    result = select(
        [_pair("a", "gallium", disruption=5), _pair("b", "gallium", disruption=4)],
        limit=10,
    )
    assert [p.arxiv_id for p in result.crowded_out] == ["b"]


def test_at_most_ten_papers_are_selected():
    pairs = [_pair(f"p{n}", f"industry{n}") for n in range(25)]
    assert len(select(pairs, limit=10).selected) == 10


def test_fewer_qualifying_papers_means_a_shorter_report():
    """Explicitly in the brief: if only 4 qualify, the report has 4."""
    pairs = [_pair(f"p{n}", f"industry{n}") for n in range(4)]
    assert len(select(pairs, limit=10).selected) == 4


def test_an_empty_pool_selects_nothing():
    assert select([], limit=10).selected == []


def test_ranking_leads_with_readiness():
    """The addendum orders by readiness. Disruption only breaks ties now."""
    result = select(
        [
            _pair("ready", "a", readiness=9, disruption=1),
            _pair("disruptive", "b", readiness=4, disruption=5),
        ],
        limit=10,
    )
    assert [p.arxiv_id for p in result.selected] == ["ready", "disruptive"]


def test_disruption_breaks_a_readiness_tie():
    """Readiness is a small integer over ~10 papers, so ties are common; without
    this the order would fall back to arXiv ID, which is arbitrary."""
    result = select(
        [
            _pair("boring", "a", readiness=7, disruption=1),
            _pair("big", "b", readiness=7, disruption=5),
        ],
        limit=10,
    )
    assert [p.arxiv_id for p in result.selected] == ["big", "boring"]


def test_tag_confidence_outranks_disruption_in_a_readiness_tie():
    result = select(
        [
            _pair("sure", "a", readiness=7, disruption=1, confidence="high"),
            _pair("guess", "b", readiness=7, disruption=5, confidence="low"),
        ],
        limit=10,
    )
    assert [p.arxiv_id for p in result.selected] == ["sure", "guess"]


def test_ties_are_broken_deterministically():
    """Two identically scored papers must not swap places between runs."""
    pairs = [_pair("zzz", "a"), _pair("aaa", "b")]
    first = [p.arxiv_id for p in select(pairs, limit=10).selected]
    second = [p.arxiv_id for p in select(list(reversed(pairs)), limit=10).selected]
    assert first == second == ["aaa", "zzz"]


def test_a_pair_with_no_industry_is_not_selectable():
    assert select([_pair("a", "")], limit=10).selected == []


def test_a_zero_limit_selects_nothing():
    assert select([_pair("a", "gallium")], limit=0).selected == []


def test_the_database_also_refuses_a_duplicate_industry(conn):
    """select.py enforces the rule; the UNIQUE constraint is the backstop."""
    import sqlite3

    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=True)
    db.insert_papers(conn, [_paper(1), _paper(2)], run)
    db.insert_selections(conn, run, [(_pair("2609.00001", "gallium"), True)])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO selections (arxiv_id, run_id, industry_id, rank, relevance, "
            "disruption, verified, created_utc) VALUES (?,?,?,?,?,?,?,?)",
            ("2609.00002", run, "gallium", 2, 3, 3, 1, "now"),
        )


# --------------------------------------------------------------------------
# Digest rendering
# --------------------------------------------------------------------------


def _entry(
    n=1, industry="gallium", name="Gallium refining", disruption=3, verified=True,
    readiness=8, pipeline="sensing", pipeline_name="Environmental sensitivity -> Sensing",
    most_limiting="scalability", limiting_note="single flake only",
):
    return Entry(
        arxiv_id=f"2609.0000{n}",
        title=f"Paper {n}",
        abs_url=f"https://arxiv.org/abs/2609.0000{n}",
        first_author="A. Author",
        institution="",
        industry_id=industry,
        industry_name=name,
        relevance=3,
        disruption=disruption,
        assessment=FinalAssessment.model_validate(
            {**VALID_FINAL, "best_industry": industry, "disruption": disruption}
        ),
        verified=verified,
        pipeline_id=pipeline,
        pipeline_name=pipeline_name,
        readiness=readiness,
        max_readiness=10,
        most_limiting=most_limiting,
        limiting_note=limiting_note,
    )


def test_an_empty_week_is_exactly_one_line():
    """Straight from the brief."""
    markdown = render_digest(DigestData(entries=[], week_of="2026-09-30"))
    assert markdown.strip() == "No highly relevant papers this week."
    assert len(markdown.strip().splitlines()) == 1


def test_the_report_opens_with_the_funnel_counts():
    """The addendum asks for one line of funnel counts: scanned, past Stage A,
    shortlisted, featured."""
    markdown = render_digest(
        DigestData(
            entries=[_entry(1)], week_of="w", n_fetched=1168, n_evaluated=278,
            n_after_tag=140, n_shortlisted=20,
        )
    )
    assert "278 scanned" in markdown
    assert "140 passed pipeline tagging" in markdown
    assert "20 shortlisted" in markdown
    assert "1 featured" in markdown


def test_the_fetch_count_is_still_reported_but_not_as_the_denominator():
    """Quoting the fetch count as the denominator reads as a 0.1% hit rate when
    most of those papers were already judged in an earlier week."""
    markdown = render_digest(
        DigestData(
            entries=[_entry(1)], week_of="w", n_fetched=1168, n_evaluated=278,
            n_after_tag=140, n_shortlisted=20,
        )
    )
    assert "1168 papers fetched" in markdown
    assert "890 already seen" in markdown


def test_the_report_has_the_required_shape():
    data = DigestData(
        entries=[_entry(1)], week_of="2026-09-30", n_fetched=1168,
        n_evaluated=278, n_after_tag=140, n_shortlisted=20, cost_usd=1.79,
    )
    markdown = render_digest(data)
    assert markdown.startswith("# arXiv Industry Scout — week of 2026-09-30")
    assert "run cost $1.79" in markdown
    assert "## 1. Gallium refining" in markdown
    assert "**Paper:** [Paper 1](https://arxiv.org/abs/2609.00001)" in markdown
    assert "A. Author et al." in markdown
    assert "**Pipeline:** Environmental sensitivity -> Sensing" in markdown
    assert "**Readiness score:** 8/10" in markdown
    assert "**What they found:**" in markdown
    assert "**Why it matters for Gallium refining:**" in markdown
    assert "**The catch:**" in markdown


def test_the_readiness_line_names_the_weakest_check():
    """The addendum says base the lab-to-practice gap on the lowest-scoring
    checks, so the reader has to be able to see which one that was."""
    markdown = render_digest(
        DigestData(entries=[_entry(1)], week_of="w", n_evaluated=1)
    )
    assert "weakest: scalability" in markdown
    assert "single flake only" in markdown


def test_entries_are_ordered_by_disruption():
    """The brief says order by disruption, so rendering must not trust input order."""
    data = DigestData(
        entries=[
            _entry(1, industry="a", name="Aaa", disruption=2),
            _entry(2, industry="b", name="Bbb", disruption=5),
        ],
        week_of="2026-09-30",
    )
    markdown = render_digest(data)
    assert markdown.index("## 1. Bbb") < markdown.index("## 2. Aaa")


def test_the_json_ordering_matches_the_markdown_ordering():
    data = DigestData(
        entries=[
            _entry(1, industry="a", name="Aaa", disruption=2),
            _entry(2, industry="b", name="Bbb", disruption=5),
        ],
        week_of="2026-09-30",
    )
    picks = json.loads(render_json(data))["picks"]
    assert [p["industry_id"] for p in picks] == ["b", "a"]


def test_the_report_adds_no_paper_sections_beyond_the_specified_ones():
    """The brief says don't add sections. The addendum then adds exactly two of its
    own -- the feedback prompt, and improvements when there are any -- so the test
    pins the set rather than requiring just one."""
    markdown = render_digest(
        DigestData(entries=[_entry(1)], week_of="2026-09-30", n_fetched=10)
    )
    headings = [line for line in markdown.splitlines() if line.startswith("#")]
    assert headings == [
        "# arXiv Industry Scout — week of 2026-09-30",
        "## 1. Gallium refining",
        "## Rate this week",
    ]


def test_an_unverified_paper_says_so_on_the_page():
    """A failed check must not look like a passed one."""
    markdown = render_digest(
        DigestData(entries=[_entry(1, verified=False)], week_of="2026-09-30")
    )
    assert "unverified" in markdown


def test_a_verified_paper_carries_no_marker():
    markdown = render_digest(
        DigestData(entries=[_entry(1, verified=True)], week_of="2026-09-30")
    )
    assert "unverified" not in markdown


def test_provenance_notes_are_rendered_as_a_footer_not_a_section():
    markdown = render_digest(
        DigestData(
            entries=[_entry(1)], week_of="2026-09-30",
            notes=["Budget guard trimmed 900 papers."],
        )
    )
    assert "Budget guard trimmed 900 papers." in markdown
    assert not any(
        line.startswith("#") and "udget" in line for line in markdown.splitlines()
    )


def test_singular_and_plural_featured_counts():
    one = render_digest(DigestData(entries=[_entry(1)], week_of="w", n_evaluated=5))
    two = render_digest(
        DigestData(
            entries=[_entry(1, industry="a", name="Aaa"),
                     _entry(2, industry="b", name="Bbb")],
            week_of="w", n_evaluated=5,
        )
    )
    assert "1 featured" in one
    assert "2 featured" in two


# --------------------------------------------------------------------------
# The machine-readable sibling
# --------------------------------------------------------------------------


def test_json_sibling_is_parseable_and_carries_the_picks():
    data = DigestData(
        entries=[_entry(1), _entry(2, industry="germanium", name="Germanium refining")],
        week_of="2026-09-30", n_fetched=955, cost_usd=1.79,
    )
    payload = json.loads(render_json(data))
    assert payload["schema_version"] == 3
    assert payload["week_of"] == "2026-09-30"
    assert payload["n_fetched"] == 955
    assert payload["n_selected"] == 2
    assert [p["industry_id"] for p in payload["picks"]] == ["gallium", "germanium"]
    assert [p["rank"] for p in payload["picks"]] == [1, 2]
    assert payload["picks"][0]["the_catch"]
    # The pipeline layer's fields must survive into the machine-readable sibling,
    # or a trend analysis cannot see readiness at all.
    assert payload["picks"][0]["readiness"] == 8
    assert payload["picks"][0]["pipeline_id"] == "sensing"
    assert payload["max_readiness"] == 10


def test_json_sibling_of_an_empty_week_is_still_valid_json():
    payload = json.loads(render_json(DigestData(entries=[], week_of="2026-09-30")))
    assert payload["picks"] == []
    assert payload["n_selected"] == 0


# --------------------------------------------------------------------------
# The free pre-filter
# --------------------------------------------------------------------------

SECONDARY = ("cond-mat.mtrl-sci", "physics.optics")


def test_prefilter_keeps_a_paper_that_any_enabled_proxy_likes(cfg):
    paper = _paper(1, abstract="We demonstrate a new thing.", categories=["quant-ph"])
    kept, report = apply_prefilter(cfg.prefilter, [paper], secondary_categories=SECONDARY)
    assert len(kept) == 1
    assert report.dropped == 0


def test_prefilter_drops_a_paper_no_proxy_likes(cfg):
    paper = _paper(1, abstract="We prove a theorem about groups.", categories=["quant-ph"])
    kept, report = apply_prefilter(cfg.prefilter, [paper], secondary_categories=SECONDARY)
    assert kept == []
    assert report.dropped == 1
    assert report.dropped_ids == ["2609.00001"]


def test_a_relevant_cross_list_alone_is_enough(cfg):
    """'any' means any: a theory paper cross-listed into materials science survives."""
    paper = _paper(
        1, abstract="We prove a theorem.", categories=["quant-ph", "cond-mat.mtrl-sci"]
    )
    kept, _ = apply_prefilter(cfg.prefilter, [paper], secondary_categories=SECONDARY)
    assert len(kept) == 1


def test_a_disabled_prefilter_keeps_everything_but_still_counts(cfg):
    """The measurement has to survive switching the filter off."""
    from dataclasses import replace

    disabled = replace(cfg.prefilter, enabled=False)
    paper = _paper(1, abstract="We prove a theorem.", categories=["quant-ph"])
    kept, report = apply_prefilter(disabled, [paper], secondary_categories=SECONDARY)
    assert len(kept) == 1
    assert report.dropped == 0
    # It still records what enabling it would have cost.
    phrases = next(p for p in report.proxies if p.name == "experimental_phrases")
    assert phrases.would_drop == 1


def test_the_journal_ref_proxy_is_off_by_default(cfg):
    """Measured recall was 3.3%. Switching this on by accident guts the digest."""
    assert cfg.prefilter.journal_ref is False


def test_journal_ref_is_still_measured_while_disabled(cfg):
    paper = _paper(1, comment="Accepted in Nature Physics")
    _, report = apply_prefilter(cfg.prefilter, [paper], secondary_categories=SECONDARY)
    journal = next(p for p in report.proxies if p.name == "journal_ref")
    assert journal.enabled is False
    assert journal.kept == 1


def test_combine_all_is_stricter_than_combine_any(cfg):
    from dataclasses import replace

    paper = _paper(
        1, abstract="We prove a theorem.", categories=["quant-ph", "cond-mat.mtrl-sci"]
    )
    any_kept, _ = apply_prefilter(cfg.prefilter, [paper], secondary_categories=SECONDARY)
    all_kept, _ = apply_prefilter(
        replace(cfg.prefilter, combine="all"), [paper], secondary_categories=SECONDARY
    )
    assert len(any_kept) == 1 and all_kept == []


def test_prefilter_of_an_empty_list_is_harmless(cfg):
    kept, report = apply_prefilter(cfg.prefilter, [], secondary_categories=SECONDARY)
    assert kept == [] and report.total == 0


# --------------------------------------------------------------------------
# Excluding replacements and revised versions
# --------------------------------------------------------------------------


def test_revisions_are_excluded_and_first_submissions_kept():
    papers = [_paper(1, version="v1"), _paper(2, version="v3"), _paper(3, version="")]
    kept, excluded = select_new_submissions(papers)
    assert [p.arxiv_id for p in kept] == ["2609.00001", "2609.00003"]
    assert excluded == 1


# --------------------------------------------------------------------------
# The budget guard
# --------------------------------------------------------------------------


def test_the_projection_grows_with_the_paper_count(cfg):
    assert project(cfg, 2000).total_usd > project(cfg, 1000).total_usd


def test_a_normal_week_is_under_the_target(cfg):
    """955 papers is the measured weekly volume for the configured allowlist."""
    projection = project(cfg, 955)
    assert projection.total_usd < cfg.budget.target_usd


def test_the_guard_does_nothing_when_the_projection_fits(cfg):
    papers = [_paper(n) for n in range(1, 6)]
    result = apply_guard(cfg, papers, {})
    assert result.kept == papers
    assert result.acted is False
    assert result.trimmed == []


def test_the_guard_trims_until_the_projection_fits(cfg):
    """A volume spike must cost the cap, not whatever it happens to cost."""
    papers = [_paper(n) for n in range(100000)]
    result = apply_guard(cfg, papers, {})
    assert result.acted is True
    assert len(result.kept) < len(papers)
    assert result.projection.total_usd <= cfg.budget.hard_cap_usd
    assert len(result.kept) + result.trimmed_count == len(papers)


def test_the_guard_sheds_the_weakest_keyword_evidence_first(cfg):
    """Trimming has to be by evidence strength, not by arrival order."""
    from scout.matcher import PaperMatch

    papers = [_paper(n) for n in range(60000)]
    strong = PaperMatch(arxiv_id="2609.00001", all_hits=["a", "b", "c"])
    strong.industries = {"gallium": ["a"]}
    strong._tiers = (1,)
    result = apply_guard(cfg, papers, {"2609.00001": strong})
    assert result.acted is True
    assert "2609.00001" in {p.arxiv_id for p in result.kept}


def test_the_guard_on_an_empty_list_is_harmless(cfg):
    result = apply_guard(cfg, [], {})
    assert result.kept == [] and result.acted is False
# --------------------------------------------------------------------------
# Concurrency helper
# --------------------------------------------------------------------------


async def test_gather_limited_respects_the_limit():
    in_flight = 0
    peak = 0

    async def worker():
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return True

    results = await gather_limited(3, [worker() for _ in range(12)])
    assert results == [True] * 12
    assert peak <= 3


async def test_gather_limited_returns_exceptions_instead_of_raising():
    async def ok():
        return 1

    async def boom():
        raise ValueError("nope")

    results = await gather_limited(2, [ok(), boom(), ok()])
    assert results[0] == 1
    assert isinstance(results[1], ValueError)
    assert results[2] == 1


def test_backoff_grows_and_is_capped():
    assert all(0 <= _sleep_for(1, 2.0, 45.0) <= 2.0 for _ in range(50))
    assert all(0 <= _sleep_for(3, 2.0, 45.0) <= 8.0 for _ in range(50))
    assert all(0 <= _sleep_for(20, 2.0, 45.0) <= 45.0 for _ in range(50))


# --------------------------------------------------------------------------
# Run lock
# --------------------------------------------------------------------------


def test_lock_blocks_a_second_holder(tmp_path):
    path = tmp_path / "scout.lock"
    with RunLock(path):
        with pytest.raises(LockBusy):
            with RunLock(path):
                pass


def test_lock_is_released_on_exit(tmp_path):
    path = tmp_path / "scout.lock"
    with RunLock(path):
        pass
    with RunLock(path):  # must not raise
        pass


def test_lock_is_released_even_when_the_run_raises(tmp_path):
    path = tmp_path / "scout.lock"
    with pytest.raises(RuntimeError):
        with RunLock(path):
            raise RuntimeError("run failed")
    with RunLock(path):
        pass


def test_lock_records_the_holding_pid(tmp_path):
    import os

    path = tmp_path / "scout.lock"
    with RunLock(path):
        assert path.read_text().strip() == str(os.getpid())


# --------------------------------------------------------------------------
# Usage / cost accounting
# --------------------------------------------------------------------------


def test_cost_is_summed_per_model_at_that_model_s_price(cfg):
    usage = db.Usage()
    usage.add("claude-haiku-4-5-20251001", 1_000_000, 200_000)  # $1.00 + $1.00
    usage.add("claude-sonnet-5", 500_000, 100_000)              # $1.00 + $1.00
    assert usage.input_tokens == 1_500_000
    assert usage.output_tokens == 300_000
    assert usage.cost_usd(cfg.price) == pytest.approx(4.0)


def test_an_unpriced_model_costs_zero_rather_than_crashing(cfg):
    usage = db.Usage()
    usage.add("some-future-model", 1_000_000, 1_000_000)
    assert usage.cost_usd(cfg.price) == 0.0


# --------------------------------------------------------------------------
# Failure-rate guard: a broken run must not look like a quiet week
# --------------------------------------------------------------------------


def test_a_quiet_week_with_nothing_attempted_is_not_a_failure():
    from scout.pipeline import check_failure_rate

    check_failure_rate("triage", attempted=0, succeeded=0, max_rate=0.5)  # must not raise


def test_total_failure_of_a_non_empty_stage_raises():
    from scout.pipeline import StageFailure, check_failure_rate

    with pytest.raises(StageFailure, match="110/110"):
        check_failure_rate("triage", attempted=110, succeeded=0, max_rate=0.5)


def test_failures_within_the_threshold_are_tolerated():
    from scout.pipeline import check_failure_rate

    check_failure_rate("triage", attempted=100, succeeded=60, max_rate=0.5)  # 40% failed


def test_the_threshold_boundary_is_inclusive():
    from scout.pipeline import StageFailure, check_failure_rate

    check_failure_rate("triage", attempted=100, succeeded=50, max_rate=0.5)  # exactly 50%
    with pytest.raises(StageFailure):
        check_failure_rate("triage", attempted=100, succeeded=49, max_rate=0.5)


def test_a_fully_successful_stage_never_raises():
    from scout.pipeline import check_failure_rate

    check_failure_rate("critic", attempted=8, succeeded=8, max_rate=0.0)


def test_stage_failure_names_the_stage_and_the_setting():
    from scout.pipeline import StageFailure, check_failure_rate

    with pytest.raises(StageFailure) as exc_info:
        check_failure_rate("analyze", attempted=10, succeeded=1, max_rate=0.5)
    message = str(exc_info.value)
    assert "analyze" in message
    assert "max_failure_rate" in message


# --------------------------------------------------------------------------
# Ingest: secondary categories and the quantum_core gate
# --------------------------------------------------------------------------


def _sec_paper(arxiv_id, cats, title="T", abstract="A"):
    return Paper(
        arxiv_id=arxiv_id, version="v1", title=title, abstract=abstract,
        authors=[], primary_category=cats[0], categories=cats,
        published_utc="", updated_utc="", abs_url="", pdf_url="", comment=None,
    )


def test_primary_category_papers_bypass_the_gate():
    from scout.ingest import classify_and_gate

    kept, gated = classify_and_gate(
        [_sec_paper("1", ["quant-ph"])], primary=("quant-ph",), gate=lambda text: False
    )
    assert [p.arxiv_id for p in kept] == ["1"]
    assert kept[0].source_stream == "primary"
    assert gated == []


def test_a_cross_listed_paper_counts_as_primary():
    from scout.ingest import classify_and_gate

    kept, gated = classify_and_gate(
        [_sec_paper("1", ["cond-mat.mtrl-sci", "quant-ph"])],
        primary=("quant-ph",), gate=lambda text: False,
    )
    assert kept[0].source_stream == "primary"
    assert gated == []


def test_secondary_only_papers_must_pass_the_gate():
    from scout.ingest import classify_and_gate

    kept, gated = classify_and_gate(
        [
            _sec_paper("1", ["cond-mat.mtrl-sci"], title="A qubit device"),
            _sec_paper("2", ["physics.app-ph"], title="Sintered alumina"),
        ],
        primary=("quant-ph",),
        gate=lambda text: "qubit" in text,
    )
    assert [p.arxiv_id for p in kept] == ["1"]
    assert kept[0].source_stream == "secondary"
    assert [p.arxiv_id for p in gated] == ["2"]
    assert gated[0].gate_passed is False


def test_gated_out_papers_are_still_recorded_so_dedupe_skips_them(cfg, conn):
    from scout.ingest import classify_and_gate

    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=False)
    kept, gated = classify_and_gate(
        [_sec_paper("2609.00001", ["cond-mat.mtrl-sci"])],
        primary=("quant-ph",), gate=lambda text: False,
    )
    assert kept == [] and len(gated) == 1

    db.insert_papers(conn, kept + gated, run)
    row = db.get_paper(conn, "2609.00001")
    assert row["source_stream"] == "secondary"
    assert row["gate_passed"] == 0
    # Already known, so a later run will not re-examine it.
    assert db.known_arxiv_ids(conn) == {"2609.00001"}


# --------------------------------------------------------------------------
# arXiv rate-limit backoff
# --------------------------------------------------------------------------


def test_a_429_is_retried_with_exponential_backoff(monkeypatch):
    """arxiv's own retries wait only delay_seconds apart, which a 429 outlasts."""
    import arxiv

    from scout import ingest

    slept: list[float] = []
    attempts = {"n": 0}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def results(self, search):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise arxiv.HTTPError("http://x", attempts["n"], 429)
            return iter(())

    monkeypatch.setattr(ingest.arxiv, "Client", FakeClient)
    papers = ingest.fetch_papers(
        categories=["quant-ph"],
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
        until=datetime(2026, 9, 15, tzinfo=timezone.utc),
        backoff_seconds=10.0,
        sleep=slept.append,
    )

    assert papers == []
    assert attempts["n"] == 3
    assert slept == [10.0, 20.0]  # doubling, not a flat 3s


def test_a_429_that_never_clears_eventually_raises(monkeypatch):
    import arxiv

    from scout import ingest

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def results(self, search):
            raise arxiv.HTTPError("http://x", 1, 429)

    monkeypatch.setattr(ingest.arxiv, "Client", FakeClient)
    with pytest.raises(arxiv.HTTPError):
        ingest.fetch_papers(
            categories=["quant-ph"],
            since=datetime(2026, 9, 1, tzinfo=timezone.utc),
            until=datetime(2026, 9, 15, tzinfo=timezone.utc),
            max_attempts=2,
            backoff_seconds=0.0,
            sleep=lambda _: None,
        )


def test_a_404_is_not_retried(monkeypatch):
    """Only rate limits and server errors are worth waiting for."""
    import arxiv

    from scout import ingest

    attempts = {"n": 0}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def results(self, search):
            attempts["n"] += 1
            raise arxiv.HTTPError("http://x", 1, 404)

    monkeypatch.setattr(ingest.arxiv, "Client", FakeClient)
    with pytest.raises(arxiv.HTTPError):
        ingest.fetch_papers(
            categories=["quant-ph"],
            since=datetime(2026, 9, 1, tzinfo=timezone.utc),
            until=datetime(2026, 9, 15, tzinfo=timezone.utc),
            sleep=lambda _: None,
        )
    assert attempts["n"] == 1


def test_industry_matches_record_each_targets_own_tier(
    cfg, conn, project_taxonomy, project_index
):
    """A paper can match a tier-1 and a tier-2 industry at once; each row must
    carry its own tier, not the paper's overall candidacy tier."""
    from scout.matcher import match_paper

    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=False)
    conn.execute(
        "INSERT INTO papers (arxiv_id, title, abstract, authors, categories, "
        "first_seen_utc) VALUES ('2609.00001','t','a','[]','[]','now')"
    )
    conn.commit()

    match = match_paper(
        project_taxonomy, project_index, "2609.00001",
        "A gallium arsenide device inspected by weld NDT",
        "We grew gallium arsenide by molecular beam epitaxy and performed weld "
        "inspection with an atomic magnetometer on a cobalt-bearing fixture.",
    )
    assert match.tier == 1  # tier-1 gallium dominates the paper's candidacy
    db.insert_industry_matches(conn, run, {"2609.00001": match}, project_taxonomy)

    tiers = {
        row["target_id"]: row["tier"]
        for row in conn.execute(
            "SELECT target_id, tier FROM industry_matches WHERE run_id = ?", (run,)
        )
    }
    assert tiers["gallium"] == 1
    assert tiers["welding"] == 2      # not 1
    # Tier-3 tagging was removed in v2: the entries stay in industries.yaml but
    # must not produce rows, or the thing that was removed is back. (`cobalt` was
    # promoted to tier 1 on 2026-09-30, so it is no longer the example here.)
    assert 3 not in tiers.values()


def test_insert_industry_matches_works_without_a_taxonomy(cfg, conn):
    """Falls back to the paper tier rather than crashing."""
    from scout.matcher import PaperMatch

    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=False)
    conn.execute(
        "INSERT INTO papers (arxiv_id, title, abstract, authors, categories, "
        "first_seen_utc) VALUES ('2609.00001','t','a','[]','[]','now')"
    )
    conn.commit()
    match = PaperMatch(arxiv_id="2609.00001", industries={"gallium": ["gaas"]})
    match._tiers = (1,)
    db.insert_industry_matches(conn, run, {"2609.00001": match})
    row = conn.execute("SELECT tier FROM industry_matches").fetchone()
    assert row["tier"] == 1


# --------------------------------------------------------------------------
# API key provenance: which key did this run spend on?
# --------------------------------------------------------------------------


def test_an_environment_variable_overrides_dotenv(tmp_path, monkeypatch):
    """The footgun: a stray exported key silently wins over .env. It must be
    reported as such, so spend can be attributed to the right console row."""
    from scout.config import load_config

    (tmp_path / "config.toml").write_text("[arxiv]\nwindow_days = 14\n")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-from-the-dotenv-file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-environment")

    cfg = load_config(tmp_path)
    assert cfg.api_key == "sk-ant-from-the-environment"
    assert cfg.api_key_source == "environment"


def test_dotenv_is_used_when_no_environment_variable_is_set(tmp_path, monkeypatch):
    from scout.config import load_config

    (tmp_path / "config.toml").write_text("[arxiv]\nwindow_days = 14\n")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-from-the-dotenv-file\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(tmp_path)
    assert cfg.api_key == "sk-ant-from-the-dotenv-file"
    assert cfg.api_key_source == ".env"


def test_a_missing_key_is_reported_as_absent(tmp_path, monkeypatch):
    from scout.config import load_config

    (tmp_path / "config.toml").write_text("[arxiv]\nwindow_days = 14\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(tmp_path, env_file=tmp_path / "nonexistent.env")
    assert cfg.api_key is None
    assert cfg.api_key_source == "absent"
    assert cfg.api_key_fingerprint == "none"


def test_the_fingerprint_identifies_a_key_without_revealing_it(tmp_path, monkeypatch):
    from scout.config import load_config

    (tmp_path / "config.toml").write_text("[arxiv]\nwindow_days = 14\n")
    secret = "sk-ant-api03-" + "z" * 95
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    cfg = load_config(tmp_path)
    fingerprint = cfg.api_key_fingerprint

    assert len(fingerprint) == 16
    assert secret not in fingerprint
    assert fingerprint not in secret
    # Deterministic, so two runs on the same key produce the same fingerprint...
    assert load_config(tmp_path).api_key_fingerprint == fingerprint
    # ...and a different key produces a different one.
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret[:-1] + "y")
    assert load_config(tmp_path).api_key_fingerprint != fingerprint


def test_the_run_row_records_which_key_was_used(cfg, conn):
    run = db.start_run(
        conn, since_utc="s", until_utc="u", dry_run=False,
        api_key_fingerprint="abc123def456", api_key_source=".env",
    )
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run,)).fetchone()
    assert row["api_key_fingerprint"] == "abc123def456"
    assert row["api_key_source"] == ".env"


# --------------------------------------------------------------------------
# Shortlist ranking: what survives the [final] max_papers cap
# --------------------------------------------------------------------------


def _match(arxiv_id, *, tier=1, industries=None, hits=("a",)):
    from scout.matcher import PaperMatch

    m = PaperMatch(arxiv_id=arxiv_id, all_hits=list(hits))
    m.industries = industries if industries is not None else {"gallium": ["a"]}
    m._tiers = (tier,)
    return m


def test_evidence_ranking_puts_tier1_above_an_unmatched_paper():
    """The ordering must be expressible as "higher sorts first" -- an earlier
    version negated the tier, which sorted tier 1 BELOW a paper with no match."""
    from scout.matcher import evidence_strength

    matches = {"strong": _match("strong", tier=1)}
    ranked = sorted(
        ["unmatched", "strong"],
        key=lambda i: evidence_strength(i, matches),
        reverse=True,
    )
    assert ranked[0] == "strong"


def test_evidence_ranking_prefers_tier1_then_more_hits():
    from scout.matcher import evidence_strength

    matches = {
        "t1": _match("t1", tier=1, hits=("a",)),
        "t2_many": _match("t2_many", tier=2, hits=("a", "b", "c", "d")),
        "t2_few": _match("t2_few", tier=2, hits=("a",)),
    }
    ranked = sorted(matches, key=lambda i: evidence_strength(i, matches), reverse=True)
    assert ranked == ["t1", "t2_many", "t2_few"]


def test_evidence_ranking_is_deterministic_for_identical_evidence():
    from scout.matcher import evidence_strength

    matches = {"zzz": _match("zzz"), "aaa": _match("aaa")}
    first = sorted(matches, key=lambda i: evidence_strength(i, matches), reverse=True)
    second = sorted(
        reversed(list(matches)), key=lambda i: evidence_strength(i, matches), reverse=True
    )
    assert first == second


def test_the_cap_cuts_the_weakest_evidence_not_the_alphabetically_unlucky(cfg):
    """Every shortlisted paper scores relevance == 3, so without this ranking the
    truncation would fall in arXiv-ID order -- which is to say, at random."""
    from scout.matcher import evidence_strength

    rows = [{"arxiv_id": "2609.00001"}, {"arxiv_id": "2609.99999"}]
    matches = {
        "2609.00001": _match("2609.00001", tier=2, hits=("a",)),
        "2609.99999": _match("2609.99999", tier=1, hits=("a", "b", "c")),
    }
    rows.sort(key=lambda r: evidence_strength(r["arxiv_id"], matches), reverse=True)
    # The tier-1 paper survives a cap of 1 despite sorting last by id.
    assert rows[:1] == [{"arxiv_id": "2609.99999"}]


def test_the_final_cap_leaves_room_above_the_selection_size(cfg):
    """Selection picks 10 with no repeated industry, so the pool feeding it has to
    be comfortably larger or the no-duplicate rule has nothing to choose between."""
    assert cfg.final.max_papers >= 4 * cfg.select.max_papers


def test_selections_record_the_readiness_and_pipeline(conn):
    """These columns exist so a trend analysis can read readiness back out. They
    were added to the schema before the INSERT was updated, so every row wrote
    NULL -- pin them."""
    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=True)
    db.insert_papers(conn, [_paper(1)], run)
    pair = _pair("2609.00001", "gallium", readiness=9, pipeline_id="photonics")
    db.insert_selections(conn, run, [(pair, True)])

    row = conn.execute(
        "SELECT readiness, pipeline_id, verified FROM selections WHERE run_id = ?",
        (run,),
    ).fetchone()
    assert row["readiness"] == 9
    assert row["pipeline_id"] == "photonics"
    assert row["verified"] == 1
