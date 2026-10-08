"""Greedy selection of the papers that make the report.

Kept deliberately free of I/O, config objects, and model calls: the selection
rule is the part of v2 most likely to be argued about, so it is a pure function
over plain data that a test can drive directly.

The rule: rank every (paper, industry) pair by relevance, then disruption. Take
the best pair, remove that paper *and* that industry from the pool, repeat.
No industry may appear twice, and fewer than `limit` papers is a valid result --
if only four pairs qualify, the report has four entries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("scout.select")


@dataclass(frozen=True)
class Pair:
    """One (paper, industry) candidate for the report."""

    arxiv_id: str
    industry_id: str
    relevance: int
    disruption: int
    # Readiness total out of pipelines.max_score. Leads the ranking.
    readiness: int = 0
    # Tag confidence, ranked high > medium > low.
    confidence: str = "medium"
    pipeline_id: str = ""


@dataclass
class Selection:
    selected: list[Pair]
    # Pairs that lost because their industry was already taken, which is the one
    # rejection reason worth reporting: the paper was good enough but something
    # better already held its slot.
    crowded_out: list[Pair]

    def __len__(self) -> int:
        return len(self.selected)


_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def _sort_key(pair: Pair) -> tuple:
    # Readiness first, then tag confidence, then disruption, then relevance -- all
    # descending. arxiv_id last so the order is total and therefore deterministic:
    # two papers with identical scores must not swap places between runs.
    return (
        -pair.readiness,
        -_CONFIDENCE_RANK.get(pair.confidence, 0),
        -pair.disruption,
        -pair.relevance,
        pair.arxiv_id,
    )


def select(pairs: list[Pair], *, limit: int = 10) -> Selection:
    """Greedily pick at most `limit` pairs, at most one per paper and per industry."""
    if limit < 0:
        raise ValueError("limit must be >= 0")

    selected: list[Pair] = []
    crowded_out: list[Pair] = []
    used_papers: set[str] = set()
    used_industries: set[str] = set()

    for pair in sorted(pairs, key=_sort_key):
        if len(selected) >= limit:
            break
        if pair.arxiv_id in used_papers:
            continue
        if not pair.industry_id:
            continue
        if pair.industry_id in used_industries:
            crowded_out.append(pair)
            continue
        selected.append(pair)
        used_papers.add(pair.arxiv_id)
        used_industries.add(pair.industry_id)

    log.info(
        "selection: %d papers from %d candidate pairs (%d crowded out by a "
        "duplicate industry)",
        len(selected), len(pairs), len(crowded_out),
    )
    return Selection(selected=selected, crowded_out=crowded_out)
