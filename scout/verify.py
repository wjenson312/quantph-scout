"""Independent verification of the selected papers.

This is v2's replacement for v1's critic, and it exists because dropping the
critic outright would have been a capability loss rather than a simplification.
In run 1 the critic measurably took claims away: it downgraded confidence
high->medium and medium->low and removed applications that ignored operating
constraints. Nothing else in the pipeline does that, because the model that
writes the prose is not a good judge of whether its own prose overreached.

Two things make it affordable here where v1's critic was not:

* it runs on the **selected** papers only (at most 10), not the whole shortlist;
* it reads the abstract, not 120k characters of full text.

The trade is honest and worth stating: checking prose against an abstract is a
weaker check than checking it against the full paper. It catches a number that
was never in the source, a claim the source does not support, and an industry
that does not fit. It cannot catch a number that the abstract states correctly
and the body contradicts. Where the final pass already pulled full text, that
text is passed through and the check is as strong as v1's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .final import FinalOutcome
from .industries import Taxonomy
from .llm import ModelClient, gather_limited
from .prompts import get_prompt
from .schemas import Verification

log = logging.getLogger("scout.verify")


@dataclass
class VerifyOutcome:
    arxiv_id: str
    verification: Verification

    @property
    def dropped(self) -> bool:
        return self.verification.verdict == "drop"


async def verify_one(
    client: ModelClient,
    cfg: Config,
    taxonomy: Taxonomy,
    outcome: FinalOutcome,
    *,
    abstract: str,
) -> VerifyOutcome:
    system, template = get_prompt(cfg, "verify")
    assessment = outcome.assessment
    industry = taxonomy.industries.get(assessment.best_industry)

    if outcome.fulltext is not None:
        source = f"Full paper text:\n{outcome.fulltext.text}"
    else:
        source = f"Abstract (the only source used to write the text):\n{abstract}"

    verification = await client.structured(
        stage="verify",
        model=cfg.models.verify,
        system=system,
        user=template.format(
            arxiv_id=outcome.arxiv_id,
            title=outcome.title,
            industry_id=assessment.best_industry,
            industry_name=industry.name if industry else assessment.best_industry,
            industry_description=(industry.description or "").strip() if industry else "",
            disruption=assessment.disruption,
            what_they_found=assessment.what_they_found,
            why_it_matters=assessment.why_it_matters,
            the_catch=assessment.the_catch,
            source=source,
        ),
        output_format=Verification,
        max_tokens=cfg.verify.max_tokens,
        arxiv_id=outcome.arxiv_id,
        thinking={"type": "adaptive"},
        effort=cfg.verify.effort,
    )
    return VerifyOutcome(arxiv_id=outcome.arxiv_id, verification=verification)


async def verify_papers(
    client: ModelClient,
    cfg: Config,
    taxonomy: Taxonomy,
    outcomes: list[FinalOutcome],
    abstracts: dict[str, str],
) -> tuple[list[VerifyOutcome], list[tuple[str, Exception]]]:
    """Verify each selected paper. Returns (verifications, failures).

    A verification failure is not fatal: the unverified entry stays in the report
    and is marked as unverified, which is more useful than dropping a good paper
    because a checking call timed out.
    """
    if not outcomes or not cfg.verify.enabled:
        return [], []

    log.info(
        "verifying %d selected papers with %s (concurrency %d)",
        len(outcomes), cfg.models.verify, cfg.verify.concurrency,
    )
    results = await gather_limited(
        cfg.verify.concurrency,
        [
            verify_one(
                client, cfg, taxonomy, o, abstract=abstracts.get(o.arxiv_id, "")
            )
            for o in outcomes
        ],
    )

    verified: list[VerifyOutcome] = []
    failures: list[tuple[str, Exception]] = []
    for outcome, result in zip(outcomes, results):
        if isinstance(result, BaseException):
            failures.append((outcome.arxiv_id, result))  # type: ignore[arg-type]
            log.error("verification failed for %s: %s", outcome.arxiv_id, result)
        else:
            verified.append(result)

    corrected = sum(1 for v in verified if v.verification.verdict == "corrected")
    dropped = sum(1 for v in verified if v.dropped)
    log.info(
        "verification done: %d checked, %d corrected, %d dropped, %d failed",
        len(verified), corrected, dropped, len(failures),
    )
    if dropped:
        log.warning(
            "%d selected papers were dropped by verification -- their claims did "
            "not hold up against the source", dropped,
        )
    return verified, failures


def apply_verifications(
    outcomes: list[FinalOutcome], verified: list[VerifyOutcome]
) -> tuple[list[FinalOutcome], list[str], set[str]]:
    """Fold corrections back into the assessments.

    Returns (surviving outcomes, dropped ids, ids that were not verified at all).
    """
    by_id = {v.arxiv_id: v.verification for v in verified}
    surviving: list[FinalOutcome] = []
    dropped: list[str] = []
    unverified: set[str] = set()

    for outcome in outcomes:
        verification = by_id.get(outcome.arxiv_id)
        if verification is None:
            unverified.add(outcome.arxiv_id)
            surviving.append(outcome)
            continue
        if verification.verdict == "drop":
            dropped.append(outcome.arxiv_id)
            continue
        outcome.assessment = outcome.assessment.model_copy(
            update={
                "what_they_found": verification.corrected_what_they_found,
                "why_it_matters": verification.corrected_why_it_matters,
                "the_catch": verification.corrected_the_catch,
                "disruption": verification.adjusted_disruption,
            }
        )
        surviving.append(outcome)

    return surviving, dropped, unverified
