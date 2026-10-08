"""Cost projection and the budget guard.

The guard runs after the free pre-filter and before the first model call, which
is the only point where the paper count is known and nothing has been spent yet.
If the projection exceeds the hard cap, the triage input is trimmed to the
highest keyword-scoring papers until it fits, and what was trimmed is recorded.

Trimming by keyword score rather than at random is deliberate: a paper that hit a
tier-1 industry keyword is likelier to matter than one that hit a group-level
term, so the guard sheds the weakest evidence first.

The estimates are per-token and come from measurement, not from guesswork -- see
`[budget]` in config.toml. They are estimates all the same, so the run still
records what it actually spent; this only decides what to attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .matcher import evidence_strength

log = logging.getLogger("scout.budget")


@dataclass
class Projection:
    n_papers: int
    tag_usd: float
    readiness_usd: float
    match_usd: float
    final_usd: float
    verify_usd: float

    @property
    def total_usd(self) -> float:
        return (
            self.tag_usd + self.readiness_usd + self.match_usd
            + self.final_usd + self.verify_usd
        )

    def describe(self) -> str:
        return (
            f"${self.total_usd:.2f} projected for {self.n_papers} papers "
            f"(tag ${self.tag_usd:.2f} + readiness ${self.readiness_usd:.2f} + "
            f"match ${self.match_usd:.2f} + final ${self.final_usd:.2f} + "
            f"verify ${self.verify_usd:.2f})"
        )


@dataclass
class GuardResult:
    projection: Projection
    kept: list
    trimmed: list = field(default_factory=list)
    # None when the guard did not need to act.
    original: Projection | None = None

    @property
    def trimmed_count(self) -> int:
        return len(self.trimmed)

    @property
    def acted(self) -> bool:
        return bool(self.trimmed)

    def describe(self) -> str:
        if not self.acted:
            return f"budget guard: {self.projection.describe()}, within cap"
        assert self.original is not None
        return (
            f"budget guard TRIMMED {self.trimmed_count} papers to fit the cap: "
            f"{self.original.describe()} -> {self.projection.describe()}"
        )


def _batched(
    price, n: int, per_request: int, prefix: int, per_paper: int, out_per_paper: int
) -> float:
    """Cost of a batched stage over `n` papers."""
    if n <= 0:
        return 0.0
    requests = -(-n // max(1, per_request))
    tokens_in = requests * prefix + n * per_paper
    tokens_out = n * out_per_paper
    return (tokens_in * price.input + tokens_out * price.output) / 1e6


def project(cfg: Config, n_papers: int) -> Projection:
    """Project the whole run's cost from the paper count entering Stage A.

    Only the two full-corpus stages scale with volume. Matching, the final pass and
    verification are bounded by their own caps, so they are projected AT those caps
    -- the pessimistic case, which is what a guard wants.
    """
    b = cfg.budget

    # Stage A: every paper.
    tag = _batched(
        cfg.price(cfg.models.tag), n_papers, cfg.tag.papers_per_request,
        b.est_tag_prefix_tokens, b.est_tokens_per_paper, b.est_tag_output_per_paper,
    )

    # Industry matching: every tagged paper. Projected at the full count, since the
    # tagging survival rate is not known until Stage A has run.
    match = _batched(
        cfg.price(cfg.models.match), n_papers, cfg.match.papers_per_request,
        b.est_triage_prefix_tokens, b.est_tokens_per_paper,
        b.est_triage_output_per_paper,
    )

    # Readiness: only papers that got an industry. Projected from the measured
    # match survival share -- if that drifts the projection drifts with it, which
    # is why `report` prints actual against projected every run.
    n_readiness = int(n_papers * b.est_match_survival_share)
    readiness = _batched(
        cfg.price(cfg.models.readiness), n_readiness,
        cfg.readiness.papers_per_request, b.est_readiness_prefix_tokens,
        b.est_tokens_per_paper, b.est_readiness_output_per_paper,
    )
    n_match = min(cfg.readiness.shortlist_size, max(1, n_readiness))

    final_price = cfg.price(cfg.models.final)
    verify_price = cfg.price(cfg.models.verify)
    n_final = min(cfg.final.max_papers, n_match)
    final = (
        n_final * b.est_final_input_per_paper * final_price.input
        + n_final * b.est_final_output_per_paper * final_price.output
    ) / 1e6

    n_verify = min(cfg.select.max_papers, n_final) if cfg.verify.enabled else 0
    verify = (
        n_verify * b.est_verify_input_per_paper * verify_price.input
        + n_verify * b.est_verify_output_per_paper * verify_price.output
    ) / 1e6

    return Projection(
        n_papers=n_papers, tag_usd=tag, readiness_usd=readiness, match_usd=match,
        final_usd=final, verify_usd=verify,
    )


def apply_guard(cfg: Config, papers: list, matches: dict) -> GuardResult:
    """Trim `papers` until the projection fits `hard_cap_usd`.

    `matches` maps arxiv_id -> PaperMatch, used only to decide what to shed first.
    """
    projection = project(cfg, len(papers))
    cap = cfg.budget.hard_cap_usd

    if projection.total_usd <= cap or not papers:
        if projection.total_usd > cfg.budget.target_usd:
            log.warning(
                "%s -- over the $%.2f target but under the $%.2f cap, proceeding",
                projection.describe(), cfg.budget.target_usd, cap,
            )
        else:
            log.info("%s", GuardResult(projection=projection, kept=papers).describe())
        return GuardResult(projection=projection, kept=list(papers))

    log.warning(
        "%s exceeds the $%.2f hard cap; trimming to the highest keyword-scoring papers",
        projection.describe(), cap,
    )
    ranked = sorted(
        papers, key=lambda p: evidence_strength(p.arxiv_id, matches), reverse=True
    )

    # Binary search the largest prefix that fits, rather than dropping one paper
    # at a time: with thousands of papers the loop would otherwise dominate.
    lo, hi = 0, len(ranked)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if project(cfg, mid).total_usd <= cap:
            lo = mid
        else:
            hi = mid - 1

    kept, trimmed = ranked[:lo], ranked[lo:]
    result = GuardResult(
        projection=project(cfg, len(kept)),
        kept=kept,
        trimmed=trimmed,
        original=projection,
    )
    log.warning("%s", result.describe())
    if not kept:
        log.error(
            "budget guard trimmed every paper: the fixed cost of the final and "
            "verify stages alone exceeds the $%.2f cap. Raise [budget].hard_cap_usd "
            "or lower [final].max_papers.", cap,
        )
    return result
