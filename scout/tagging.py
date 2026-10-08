"""Stage A: tag every paper with the pipeline its result could travel.

A pipeline is the *mechanism* by which a physics result becomes a product. Tagging
first does two things that matter:

* it drops foundational work cheaply, before anything expensive touches it;
* it narrows the industry candidates from 78 to a handful, so the industry-matching
  stage is choosing between plausible options rather than scanning a long menu.

The tagger is deliberately generous: told to choose a real pipeline with `low`
confidence rather than `none` when it is torn, because Stage B filters on evidence
and a paper dropped here is gone for good.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .batching import align, run_batched
from .config import Config
from .ingest import Paper
from .llm import ModelClient
from .pipelines import NONE_ID, PipelineSet, render_pipeline_list
from .prompts import get_prompt
from .schemas import PipelineTag, PipelineTagBatch

log = logging.getLogger("scout.tagging")


@dataclass
class TagOutcome:
    paper: Paper
    tag: PipelineTag

    @property
    def dropped(self) -> bool:
        return self.tag.primary_pipeline == NONE_ID or not self.tag.primary_pipeline

    @property
    def pipeline_ids(self) -> tuple[str, ...]:
        ids = [self.tag.primary_pipeline]
        if self.tag.secondary_pipeline and self.tag.secondary_pipeline != ids[0]:
            ids.append(self.tag.secondary_pipeline)
        return tuple(i for i in ids if i and i != NONE_ID)


def render_batch(papers: list[Paper]) -> str:
    return "\n\n".join(
        f"### Paper {n}\n"
        f"Title: {paper.title}\n"
        f"Categories: {', '.join(paper.categories)}\n"
        f"Abstract: {paper.abstract}"
        for n, paper in enumerate(papers, start=1)
    )


def _validate(tag: PipelineTag, valid: frozenset[str]) -> PipelineTag:
    """Refuse invented pipeline ids rather than letting them reach the next stage."""
    updates: dict[str, str] = {}
    if tag.primary_pipeline not in valid:
        log.warning(
            "tagging returned unknown pipeline %r; treating the paper as unmatched",
            tag.primary_pipeline,
        )
        updates["primary_pipeline"] = NONE_ID
        updates["secondary_pipeline"] = ""
    elif tag.secondary_pipeline and tag.secondary_pipeline not in valid:
        log.warning(
            "tagging returned unknown secondary pipeline %r; dropping it",
            tag.secondary_pipeline,
        )
        updates["secondary_pipeline"] = ""
    # A secondary that duplicates the primary carries no information.
    if not updates and tag.secondary_pipeline == tag.primary_pipeline:
        updates["secondary_pipeline"] = ""
    return tag.model_copy(update=updates) if updates else tag


async def tag_papers(
    client: ModelClient, cfg: Config, pipelines: PipelineSet, papers: list[Paper]
) -> tuple[list[TagOutcome], list[tuple[Paper, Exception]]]:
    """Tag every paper. Returns (outcomes, failures) -- `none` tags included.

    Dropping is the caller's job, so the run can report how many were dropped and
    the self-improvement pass can see low-confidence tags it might learn from.
    """
    if not papers:
        return [], []

    valid = frozenset(pipelines.pipelines)
    system, template = get_prompt(cfg, "tag")
    system = system.format(pipeline_list=render_pipeline_list(pipelines))

    log.info(
        "tagging %d papers against %d pipelines with %s (%d per request)",
        len(papers), len(pipelines.scorable), cfg.models.tag, cfg.tag.papers_per_request,
    )

    async def call(batch: list[Paper]) -> tuple[list[TagOutcome], list[Paper]]:
        result = await client.structured(
            stage="tag",
            model=cfg.models.tag,
            system=system,
            user=template.format(n=len(batch), papers=render_batch(batch)),
            output_format=PipelineTagBatch,
            max_tokens=cfg.tag.max_tokens,
            arxiv_id=f"batch:{batch[0].arxiv_id}+{len(batch) - 1}",
        )
        paired, unanswered = align(batch, result.results, label="tagging batch")
        return (
            [TagOutcome(paper=p, tag=_validate(t, valid)) for p, t in paired],
            unanswered,
        )

    outcomes, failures = await run_batched(
        papers,
        batch_size=cfg.tag.papers_per_request,
        concurrency=cfg.tag.concurrency,
        call=call,
        stage="tag",
    )

    kept = [o for o in outcomes if not o.dropped]
    low = sum(1 for o in kept if o.tag.confidence == "low")
    log.info(
        "tagging done: %d tagged, %d dropped as '%s', %d kept (%d low confidence), "
        "%d failed",
        len(outcomes), len(outcomes) - len(kept), NONE_ID, len(kept), low, len(failures),
    )
    return outcomes, failures
