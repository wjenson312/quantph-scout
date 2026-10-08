"""The final pass: confirm the industry, score disruption, write the report prose.

This one stage replaces v1's `analyze` + `critic` pair. The big change is that it
reads **abstracts, not full text**, which is where almost all of v2's saving
comes from: run 1's full texts averaged 87k characters (~22k tokens) per paper,
against ~340 tokens for a title and abstract.

Full text is still reachable, but only per-paper and only on demand: the model is
asked to set `abstract_too_vague` when it cannot explain the finding from the
abstract, and only those papers get a second call with the paper text attached.
On run 1's corpus that should be rare, and each occurrence is logged so the rate
can be watched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import requests

from .config import Config
from .fulltext import FullText, FullTextError, fetch_fulltext
from .industries import Taxonomy
from .llm import ModelClient, gather_limited
from .prompts import get_prompt
from .schemas import FinalAssessment
from .match import eligible_industries, render_industry_list

log = logging.getLogger("scout.final")

TRUNCATION_NOTE = (
    " (truncated -- this is the first part of the paper only, the tail is missing)"
)


@dataclass
class FinalOutcome:
    arxiv_id: str
    title: str
    assessment: FinalAssessment
    # Set only when the abstract was judged too vague and full text was pulled.
    fulltext: FullText | None = None

    @property
    def used_fulltext(self) -> bool:
        return self.fulltext is not None


def _industry_name(taxonomy: Taxonomy, industry_id: str) -> str:
    industry = taxonomy.industries.get(industry_id)
    return industry.name if industry else industry_id


async def _ask(
    client: ModelClient,
    cfg: Config,
    taxonomy: Taxonomy,
    *,
    arxiv_id: str,
    title: str,
    abstract: str,
    categories: str,
    triage_industry: str,
    triage_reason: str,
    fulltext: FullText | None,
) -> FinalAssessment:
    system, template = get_prompt(cfg, "final")
    system = system.format(industry_list=render_industry_list(taxonomy))
    if fulltext is None:
        source_block = (
            "You have the abstract only. If it genuinely does not say enough to "
            "explain the finding, set abstract_too_vague and the full text will be "
            "fetched for a second look."
        )
    else:
        source_block = (
            "Full paper text follows, because the abstract was judged too vague. "
            "Set abstract_too_vague to false now and answer from this text."
            f"{TRUNCATION_NOTE if fulltext.truncated else ''}\n\n"
            f"{fulltext.text}"
        )

    return await client.structured(
        stage="final",
        model=cfg.models.final,
        system=system,
        user=template.format(
            arxiv_id=arxiv_id,
            title=title,
            categories=categories,
            abstract=abstract,
            triage_industry=triage_industry,
            triage_industry_name=_industry_name(taxonomy, triage_industry),
            triage_reason=triage_reason,
            source_block=source_block,
        ),
        output_format=FinalAssessment,
        max_tokens=cfg.final.max_tokens,
        arxiv_id=arxiv_id,
        thinking={"type": "adaptive"},
        effort=cfg.final.effort,
    )


async def assess_paper(
    client: ModelClient,
    cfg: Config,
    taxonomy: Taxonomy,
    row: dict,
    session: requests.Session,
    valid_ids: frozenset[str],
) -> FinalOutcome:
    arxiv_id = row["arxiv_id"]
    common = dict(
        arxiv_id=arxiv_id,
        title=row["title"],
        abstract=row["abstract"],
        categories=row.get("categories") or "",
        triage_industry=row.get("best_industry") or "",
        triage_reason=row.get("reason") or "",
    )

    assessment = await _ask(client, cfg, taxonomy, fulltext=None, **common)
    fulltext: FullText | None = None

    if assessment.abstract_too_vague and cfg.final.allow_fulltext_fallback:
        log.info("%s: abstract judged too vague, fetching full text", arxiv_id)
        try:
            # requests + pypdf are blocking, so keep them off the event loop.
            fulltext = await asyncio.to_thread(
                fetch_fulltext,
                arxiv_id,
                row.get("pdf_url"),
                max_chars=cfg.final.max_fulltext_chars,
                timeout=cfg.fulltext.timeout,
                user_agent=cfg.fulltext.user_agent,
                session=session,
            )
        except FullTextError as exc:
            # Not fatal: the abstract-only answer already exists, so report it
            # rather than losing the paper over an unavailable PDF.
            log.warning(
                "%s: full text unavailable (%s); keeping the abstract-only reading",
                arxiv_id, exc,
            )
        else:
            log.info(
                "%s: full text from %s (%d chars%s)",
                arxiv_id, fulltext.source, fulltext.chars,
                ", truncated" if fulltext.truncated else "",
            )
            assessment = await _ask(
                client, cfg, taxonomy, fulltext=fulltext, **common
            )

    if assessment.best_industry not in valid_ids:
        log.warning(
            "%s: final pass returned unknown industry %r; keeping triage's %r",
            arxiv_id, assessment.best_industry, common["triage_industry"],
        )
        assessment = assessment.model_copy(
            update={"best_industry": common["triage_industry"]}
        )

    return FinalOutcome(
        arxiv_id=arxiv_id,
        title=row["title"],
        assessment=assessment,
        fulltext=fulltext,
    )


async def assess_papers(
    client: ModelClient, cfg: Config, taxonomy: Taxonomy, rows: list[dict]
) -> tuple[list[FinalOutcome], list[tuple[str, Exception]]]:
    """Run the final pass over the shortlist. `rows` come from db.shortlisted_for_run."""
    if not rows:
        return [], []

    capped = rows[: cfg.final.max_papers]
    if len(rows) > len(capped):
        log.warning(
            "shortlist has %d papers; assessing the top %d ([final].max_papers)",
            len(rows), len(capped),
        )
    log.info(
        "final pass over %d papers with %s (concurrency %d, abstracts only)",
        len(capped), cfg.models.final, cfg.final.concurrency,
    )

    valid_ids = frozenset(i.id for i in eligible_industries(taxonomy))
    session = requests.Session()
    try:
        results = await gather_limited(
            cfg.final.concurrency,
            [
                assess_paper(client, cfg, taxonomy, row, session, valid_ids)
                for row in capped
            ],
        )
    finally:
        session.close()

    outcomes: list[FinalOutcome] = []
    failures: list[tuple[str, Exception]] = []
    for row, result in zip(capped, results):
        if isinstance(result, BaseException):
            failures.append((row["arxiv_id"], result))  # type: ignore[arg-type]
            log.error("final pass failed for %s: %s", row["arxiv_id"], result)
        else:
            outcomes.append(result)

    pulled = sum(1 for o in outcomes if o.used_fulltext)
    log.info(
        "final pass done: %d assessed, %d needed full text, %d failed",
        len(outcomes), pulled, len(failures),
    )
    return outcomes, failures
