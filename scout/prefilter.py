"""Free engagement / quality pre-filter, applied before any model call.

The brief asked for a real engagement signal (citations, Altmetric) with cheap
proxies as a fallback. A real signal is not available for papers this new, and
the investigation is recorded here so it is not repeated:

* **Semantic Scholar** indexes arXiv preprints, but its unauthenticated endpoint
  is a shared, contended pool that returned 429 on every attempt made while
  building this -- including for a well-known 2017 paper, so the failure was the
  pool, not coverage. A free API key raises that to 1 request/second, and the
  `paper/batch` endpoint takes 500 ids per POST, so a week's papers would be ~3
  requests. That part is workable. What is not workable is the signal itself: a
  seven-day-old preprint has essentially no citations, so `citationCount` is 0
  for almost every paper and cannot rank or filter anything.
* **Altmetric** has the same problem in a worse form -- its coverage of
  un-published preprints is thin, and its free tier is not licensed for this.

So: proxies. Every one of them is configurable and every one reports how many
papers it would drop, whether or not it is enabled, because a filter that
silently discards good papers is worse than no filter. The numbers in
`config.toml` come from measuring each proxy against run 1's 998 papers, using
v1's own triage scores as ground truth.

The headline finding is that the journal-reference proxy must not be used as a
gate: it keeps 2.2% of papers and retains 3.3% of the good ones. It is off by
default for that reason, not by oversight.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .config import PrefilterConfig
from .ingest import Paper

log = logging.getLogger("scout.prefilter")

# "accepted", "accepted in/by", "published in", "to appear in".
_PUBLISHED_RE = re.compile(
    r"\b(accepted|published\s+in|to\s+appear\s+in)\b", re.IGNORECASE
)


@dataclass
class ProxyStats:
    """What one proxy did, whether or not it was enabled."""

    name: str
    enabled: bool
    kept: int = 0
    # Papers this proxy alone would reject. Recorded even when disabled, so the
    # cost of switching it on is visible before you switch it on.
    would_drop: int = 0


@dataclass
class PrefilterReport:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    combine: str = "any"
    enabled: bool = True
    proxies: list[ProxyStats] = field(default_factory=list)
    # Papers dropped by the combined filter that no enabled proxy liked.
    dropped_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.enabled:
            parts = ", ".join(
                f"{p.name} would drop {p.would_drop}" for p in self.proxies
            )
            return f"prefilter disabled: {self.total} papers kept ({parts})"
        parts = ", ".join(
            f"{p.name}{'' if p.enabled else ' [off]'} kept {p.kept}" for p in self.proxies
        )
        return (
            f"prefilter ({self.combine}): kept {self.kept}/{self.total}, "
            f"dropped {self.dropped} -- {parts}"
        )


def _has_journal_signal(paper: Paper) -> bool:
    journal_ref = (getattr(paper, "journal_ref", None) or "").strip()
    if journal_ref:
        return True
    return bool(_PUBLISHED_RE.search(paper.comment or ""))


def _has_experimental_phrase(paper: Paper, phrases: list[str]) -> bool:
    haystack = f"{paper.title}\n{paper.abstract}".lower()
    return any(phrase.lower() in haystack for phrase in phrases)


def _is_relevant_cross_list(paper: Paper, relevant: frozenset[str]) -> bool:
    """True if the paper sits in at least one allowlisted non-primary category.

    The categories come from industries.yaml, so this proxy needs no list of its
    own and cannot drift from the arXiv query.
    """
    return bool(relevant & set(paper.categories))


def apply_prefilter(
    cfg: PrefilterConfig,
    papers: list[Paper],
    *,
    secondary_categories: tuple[str, ...] = (),
) -> tuple[list[Paper], PrefilterReport]:
    """Return (surviving papers, report).

    When `cfg.enabled` is False every paper survives, but each proxy is still
    evaluated so the report says what enabling it would have cost.
    """
    relevant = frozenset(secondary_categories)

    checks: list[tuple[str, bool, object]] = [
        ("journal_ref", cfg.journal_ref, lambda p: _has_journal_signal(p)),
        (
            "experimental_phrases",
            cfg.experimental_phrases,
            lambda p: _has_experimental_phrase(p, list(cfg.phrases)),
        ),
        (
            "relevant_cross_list",
            cfg.relevant_cross_list and bool(relevant),
            lambda p: _is_relevant_cross_list(p, relevant),
        ),
    ]
    stats = {name: ProxyStats(name=name, enabled=on) for name, on, _ in checks}
    report = PrefilterReport(
        total=len(papers), combine=cfg.combine, enabled=cfg.enabled,
        proxies=[stats[name] for name, _, _ in checks],
    )
    if not papers:
        return [], report

    combine_all = cfg.combine == "all"
    kept: list[Paper] = []
    for paper in papers:
        verdicts: list[bool] = []
        for name, on, predicate in checks:
            hit = bool(predicate(paper))  # type: ignore[operator]
            if hit:
                stats[name].kept += 1
            else:
                stats[name].would_drop += 1
            if on:
                verdicts.append(hit)

        if not cfg.enabled or not verdicts:
            kept.append(paper)
            continue
        survives = all(verdicts) if combine_all else any(verdicts)
        if survives:
            kept.append(paper)
        else:
            report.dropped_ids.append(paper.arxiv_id)

    report.kept = len(kept)
    report.dropped = len(papers) - len(kept)
    log.info("%s", report.summary())
    if report.enabled and report.total and report.dropped / report.total > 0.8:
        log.warning(
            "prefilter dropped %.0f%% of papers -- check [prefilter] in config.toml "
            "before trusting a thin digest",
            100 * report.dropped / report.total,
        )
    return kept, report
