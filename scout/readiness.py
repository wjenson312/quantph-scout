"""Readiness scoring: how close each result is to real use, and the top-N shortlist.

Runs *after* industry matching, not before it. The addendum specifies the reverse,
and that ordering produced an empty report: readiness rewards work with no physical
barriers, which means software, and a crystal-structure-prediction tool has no
industry. `design_tools` papers averaged 7.5/10 and took 9 of the first run's 20
shortlist slots, after which matching dropped 18 of 20 for having no industry at all.
Scoring only industry-matched papers fixes that and makes this stage cheaper.

Five checks, 0-2 each, out of 10 (the denominator is derived from `pipelines.yaml`,
not hardcoded). This score does two jobs: it is the shortlist filter, and it is
what the report is ordered by.

Two deliberate deviations from a naive reading of the rubric, both documented in
`prompts/readiness.md`:

* **`operating_conditions` is judged against the pipeline's incumbent**, not against
  room temperature absolutely. Scored literally, every superconducting and quantum
  paper scores 0 on it by definition and can never rank -- which would silently
  delete whole industries from the report. A 77 K tape competing with a 4 K magnet
  is an improvement, not a barrier.
* **"not stated" scores 1**, per the brief. That makes `reproducibility` nearly
  constant, since single preprints rarely report independent replication; see
  `describe_distribution` for how to check whether a check has gone inert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .batching import align, run_batched
from .config import Config
from .llm import ModelClient
from .pipelines import PipelineSet, render_readiness_checks
from .prompts import get_prompt
from .match import MatchOutcome
from .schemas import ReadinessBatch, ReadinessScore

log = logging.getLogger("scout.readiness")

NOT_STATED = "not stated"


@dataclass
class ScoredPaper:
    matched: MatchOutcome
    score: ReadinessScore

    @property
    def tagged(self):
        return self.matched.tagged

    @property
    def arxiv_id(self) -> str:
        return self.matched.arxiv_id

    @property
    def total(self) -> int:
        return sum(item.score for item in self.score.checks)

    @property
    def by_check(self) -> dict[str, int]:
        return {item.check_id: item.score for item in self.score.checks}

    def weakest(self) -> str:
        """The limiting check, trusting the model's own choice when it is coherent.

        The model is asked for `most_limiting` directly because it can weigh which
        of several tied-low checks actually blocks use. If it names a check that is
        not the lowest, fall back to the lowest -- a stated barrier beats a guess.
        """
        scores = self.by_check
        if not scores:
            return ""
        floor = min(scores.values())
        named = self.score.most_limiting
        if named in scores and scores[named] == floor:
            return named
        lowest = [cid for cid, s in scores.items() if s == floor]
        # Prefer a check the paper actually addressed over one that was silent.
        stated = [
            item.check_id
            for item in self.score.checks
            if item.check_id in lowest and NOT_STATED not in item.note.lower()
        ]
        return (stated or lowest)[0]

    def note_for(self, check_id: str) -> str:
        for item in self.score.checks:
            if item.check_id == check_id:
                return item.note
        return ""


@dataclass
class ReadinessReport:
    scored: list[ScoredPaper] = field(default_factory=list)
    shortlist: list[ScoredPaper] = field(default_factory=list)
    max_score: int = 10

    def describe_distribution(self) -> str:
        """Per-check mean, so an inert check is visible rather than assumed.

        A check whose mean sits at exactly 1.0 is telling you every paper answered
        "not stated" -- it is contributing a constant and discriminating nothing.
        """
        if not self.scored:
            return "no papers scored"
        totals: dict[str, list[int]] = {}
        for paper in self.scored:
            for item in paper.score.checks:
                totals.setdefault(item.check_id, []).append(item.score)
        parts = [
            f"{cid} {sum(v) / len(v):.2f}" for cid, v in totals.items() if v
        ]
        return "mean per check: " + ", ".join(parts)


def _sort_key(paper: ScoredPaper) -> tuple:
    """Rank by total, then tag confidence, then how concrete the measurement is.

    Exactly the tie-break order the brief asks for. `arxiv_id` last so the order is
    total and two runs over the same input rank identically.
    """
    confidence_rank = {"high": 2, "medium": 1, "low": 0}
    return (
        -paper.total,
        -confidence_rank.get(paper.tagged.tag.confidence, 0),
        -paper.score.concreteness,
        paper.arxiv_id,
    )


def render_batch(papers: list[MatchOutcome], pipelines: PipelineSet) -> str:
    """Each paper carries its pipeline, so operating conditions can be judged
    against that pipeline's incumbent rather than against room temperature."""
    blocks = []
    for n, matched in enumerate(papers, start=1):
        tagged = matched.tagged
        pipeline = pipelines.pipelines.get(tagged.tag.primary_pipeline)
        name = pipeline.name if pipeline else tagged.tag.primary_pipeline
        incumbents = ", ".join(pipeline.becomes) if pipeline and pipeline.becomes else "-"
        blocks.append(
            f"### Paper {n}\n"
            f"Pipeline: {tagged.tag.primary_pipeline} ({name})\n"
            f"Competes with: {incumbents}\n"
            f"Filed under industry: {matched.result.best_industry}\n"
            f"Tagged on signal: {tagged.tag.signal}\n"
            f"Title: {tagged.paper.title}\n"
            f"Abstract: {tagged.paper.abstract}"
        )
    return "\n\n".join(blocks)


def _validate(score: ReadinessScore, check_ids: tuple[str, ...]) -> ReadinessScore | None:
    """Drop unknown checks and require the expected set.

    A partial answer is rejected rather than scored, because a missing check would
    silently lower a paper's total and push it down the ranking for no real reason.
    """
    known = [item for item in score.checks if item.check_id in check_ids]
    seen = {item.check_id for item in known}
    if seen != set(check_ids):
        missing = sorted(set(check_ids) - seen)
        log.warning(
            "readiness answer covered %d/%d checks (missing %s); retrying this paper",
            len(seen), len(check_ids), ", ".join(missing),
        )
        return None
    return score.model_copy(update={"checks": known})


async def score_papers(
    client: ModelClient,
    cfg: Config,
    pipelines: PipelineSet,
    matched: list[MatchOutcome],
) -> tuple[ReadinessReport, list[tuple[MatchOutcome, Exception]]]:
    """Score every industry-matched paper; return the top `shortlist_size`."""
    report = ReadinessReport(max_score=pipelines.max_score)
    if not matched:
        return report, []

    check_ids = pipelines.check_ids()
    system, template = get_prompt(cfg, "readiness")
    system = system.format(readiness_checks=render_readiness_checks(pipelines))

    log.info(
        "readiness scoring %d industry-matched papers on %d checks (max %d) with %s",
        len(matched), len(check_ids), pipelines.max_score, cfg.models.readiness,
    )

    async def call(
        batch: list[MatchOutcome],
    ) -> tuple[list[ScoredPaper], list[MatchOutcome]]:
        result = await client.structured(
            stage="readiness",
            model=cfg.models.readiness,
            system=system,
            user=template.format(
                n=len(batch), papers=render_batch(batch, pipelines)
            ),
            output_format=ReadinessBatch,
            max_tokens=cfg.readiness.max_tokens,
            arxiv_id=f"batch:{batch[0].arxiv_id}+{len(batch) - 1}",
        )
        paired, unanswered = align(batch, result.results, label="readiness batch")
        produced: list[ScoredPaper] = []
        for item, raw in paired:
            validated = _validate(raw, check_ids)
            if validated is None:
                unanswered.append(item)
            else:
                produced.append(ScoredPaper(matched=item, score=validated))
        return produced, unanswered

    scored, failures = await run_batched(
        matched,
        batch_size=cfg.readiness.papers_per_request,
        concurrency=cfg.readiness.concurrency,
        call=call,
        stage="readiness",
    )

    report.scored = sorted(scored, key=_sort_key)
    report.shortlist = report.scored[: cfg.readiness.shortlist_size]
    if report.scored:
        log.info(
            "readiness done: %d scored, %d failed; shortlist %d (scores %d-%d of %d)",
            len(scored), len(failures), len(report.shortlist),
            report.shortlist[-1].total if report.shortlist else 0,
            report.shortlist[0].total if report.shortlist else 0,
            pipelines.max_score,
        )
        log.info("readiness %s", report.describe_distribution())
    return report, failures
