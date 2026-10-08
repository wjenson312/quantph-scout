"""Industry matching: choose the one industry each shortlisted paper bears on.

This was `triage.py` before the pipeline layer. It runs between tagging and
readiness scoring, and that order was chosen the hard way.

The addendum puts readiness scoring first and passes only its top 20 on to industry
matching. On real data that produces an **empty report**, because readiness and
industry relevance are orthogonal: the readiness rubric rewards work with no
physical barriers, which means software. In the first run under that order,
`design_tools` papers averaged 7.5/10 and took 9 of the top 20 slots -- and a crystal
structure prediction tool has no industry in the taxonomy. 18 of 20 shortlisted
papers were dropped here for having no industry, and verification dropped the other
two. So matching now runs first, over every tagged paper, and readiness scores only
what actually has an industry.

The pipeline tag still earns its place: rather than offering 78 industries, the
candidate list is narrowed to those plausibly reachable from the paper's pipeline,
and papers are batched by pipeline so that narrowing is real rather than diluted
into a union. `typical_domains` are hints only -- the industry list decides the
final answer, and a paper whose best fit is weak is dropped rather than forced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .batching import align, run_batched
from .config import Config
from .industries import Industry, Taxonomy
from .llm import ModelClient
from .pipelines import PipelineSet
from .prompts import get_prompt
from .schemas import TriageBatch, TriageItem
from .tagging import TagOutcome

log = logging.getLogger("scout.match")


@dataclass
class MatchOutcome:
    tagged: TagOutcome
    result: TriageItem
    kept: bool

    @property
    def arxiv_id(self) -> str:
        return self.tagged.paper.arxiv_id


def eligible_industries(taxonomy: Taxonomy) -> list[Industry]:
    """Tier-1 and tier-2 industries, in a stable order.

    Tier 3 is excluded here and nowhere else needs to know: it is simply never
    offered to the model, so it cannot be chosen.
    """
    return sorted(
        (i for i in taxonomy.industries.values() if i.tier in (1, 2)),
        key=lambda i: (i.tier, i.id),
    )


def render_industry_list(taxonomy: Taxonomy, subset: list[Industry] | None = None) -> str:
    industries = subset if subset is not None else eligible_industries(taxonomy)
    return "\n".join(
        f"- {i.id} (tier {i.tier}) — {i.name}: {(i.description or '').strip()}"
        for i in industries
    )


def render_tier_definitions(taxonomy: Taxonomy) -> str:
    # Tier 3 is deliberately not described: it is not an eligible match, so
    # telling the model it exists only invites it to reach for one.
    return "\n".join(
        f"- Tier {tier}: {taxonomy.tier_definition(tier)}" for tier in (1, 2)
    )


def is_kept(result: TriageItem, min_relevance: int) -> bool:
    """Keep only a real industry fit.

    This honours the instruction not to force a fit -- a paper whose best available
    industry is a stretch is dropped rather than wedged in. It is also what keeps
    readiness scoring cheap, since only what survives here gets scored.
    """
    return result.relevance >= min_relevance and bool(result.best_industry)


def candidate_industries(
    taxonomy: Taxonomy,
    pipelines: PipelineSet,
    pipeline_ids: tuple[str, ...],
    *,
    min_candidates: int,
) -> list[Industry]:
    """Narrow the industry list using the paper's pipeline(s).

    Matching is on the pipeline's `becomes` and `typical_domains` vocabulary against
    each industry's name and description. These are hints, not rules, so the result
    is only ever a *narrowing*: if it leaves too few candidates to choose sensibly,
    the full eligible list is returned rather than trusting a thin overlap.
    """
    everything = eligible_industries(taxonomy)
    if not pipeline_ids:
        return everything

    vocabulary: set[str] = set()
    for pid in pipeline_ids:
        pipeline = pipelines.pipelines.get(pid)
        if pipeline is None:
            continue
        for phrase in (*pipeline.becomes, *pipeline.typical_domains):
            vocabulary.update(
                word for word in phrase.lower().replace("-", " ").split() if len(word) > 3
            )
    if not vocabulary:
        return everything

    narrowed = [
        industry
        for industry in everything
        if vocabulary & set(
            f"{industry.name} {industry.description}".lower().replace("-", " ").split()
        )
    ]
    if len(narrowed) < min_candidates:
        # A thin overlap means the vocabulary did not describe this corner of the
        # taxonomy, not that only two industries are plausible. Widen rather than
        # force the model to pick from a list that may exclude the right answer.
        return everything
    return narrowed


def group_by_pipeline(tagged: list[TagOutcome]) -> list[tuple[str, list[TagOutcome]]]:
    """Group papers by primary pipeline, so each batch shares a narrowed list.

    Without this the candidate list for a batch is the union of what every paper in
    it allows, which for a mixed batch is most of the taxonomy -- and the narrowing
    stops narrowing anything.
    """
    groups: dict[str, list[TagOutcome]] = {}
    for paper in tagged:
        groups.setdefault(paper.tag.primary_pipeline, []).append(paper)
    return sorted(groups.items())


async def match_papers(
    client: ModelClient,
    cfg: Config,
    taxonomy: Taxonomy,
    pipelines: PipelineSet,
    tagged: list[TagOutcome],
) -> tuple[list[MatchOutcome], list[tuple[TagOutcome, Exception]]]:
    """Assign one industry per tagged paper. Returns (outcomes, failures)."""
    if not tagged:
        return [], []

    valid_ids = frozenset(i.id for i in eligible_industries(taxonomy))
    system_template, user_template = get_prompt(cfg, "match")

    log.info(
        "industry matching %d tagged papers across %d pipelines with %s",
        len(tagged), len(group_by_pipeline(tagged)), cfg.models.match,
    )

    async def call(
        batch: list[TagOutcome],
    ) -> tuple[list[MatchOutcome], list[TagOutcome]]:
        union: dict[str, Industry] = {}
        for paper in batch:
            for industry in candidate_industries(
                taxonomy, pipelines, paper.pipeline_ids,
                min_candidates=cfg.match.min_candidates,
            ):
                union[industry.id] = industry
        candidates = sorted(union.values(), key=lambda i: (i.tier, i.id))

        system = system_template.format(
            tier_definitions=render_tier_definitions(taxonomy),
            industry_list=render_industry_list(taxonomy, candidates),
        )
        blocks = []
        for n, paper in enumerate(batch, start=1):
            pipeline = pipelines.pipelines.get(paper.tag.primary_pipeline)
            blocks.append(
                f"### Paper {n}\n"
                f"Pipeline: {paper.tag.primary_pipeline}"
                f" ({pipeline.name if pipeline else '-'})\n"
                f"Tagged on signal: {paper.tag.signal}\n"
                f"Title: {paper.paper.title}\n"
                f"Categories: {', '.join(paper.paper.categories)}\n"
                f"Abstract: {paper.paper.abstract}"
            )
        result = await client.structured(
            stage="match",
            model=cfg.models.match,
            system=system,
            user=user_template.format(n=len(batch), papers="\n\n".join(blocks)),
            output_format=TriageBatch,
            max_tokens=cfg.match.max_tokens,
            arxiv_id=f"batch:{batch[0].paper.arxiv_id}+{len(batch) - 1}",
        )
        paired, unanswered = align(batch, result.results, label="match batch")
        produced: list[MatchOutcome] = []
        for paper, item in paired:
            if item.best_industry and item.best_industry not in valid_ids:
                log.warning(
                    "%s: match returned unknown industry %r; treating as no match",
                    paper.paper.arxiv_id, item.best_industry,
                )
                item = item.model_copy(update={"best_industry": "", "relevance": 0})
            produced.append(
                MatchOutcome(
                    tagged=paper,
                    result=item,
                    kept=is_kept(item, cfg.match.min_relevance),
                )
            )
        return produced, unanswered

    # Batched per pipeline group, so the narrowed candidate list stays narrow.
    outcomes: list[MatchOutcome] = []
    failures: list[tuple[TagOutcome, Exception]] = []
    for _pipeline_id, group in group_by_pipeline(tagged):
        produced, failed = await run_batched(
            group,
            batch_size=cfg.match.papers_per_request,
            concurrency=cfg.match.concurrency,
            call=call,
            stage="match",
        )
        outcomes.extend(produced)
        failures.extend(failed)

    kept = [o for o in outcomes if o.kept]
    log.info(
        "matching done: %d of %d kept, %d dropped rather than forced into a weak fit, "
        "%d failed",
        len(kept), len(outcomes), len(outcomes) - len(kept), len(failures),
    )
    return outcomes, failures
