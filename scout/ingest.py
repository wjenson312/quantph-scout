"""Stage 1: fetch new quant-ph papers from arXiv.

Two things carry the weight here and are covered by tests:

* `compute_window` -- the date window is always computed at runtime, never
  hard-coded. The default window (14 days) is deliberately wider than the weekly
  run cadence because arXiv announces papers after a delay, so a paper submitted
  shortly before a run may not be retrievable until the next one.
* `strip_version` -- dedupe keys on the version-stripped id, so 2609.01234v2 and
  2609.01234v1 are the same paper. Combined with the overlapping window, nothing
  is missed and nothing is paid for twice.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import arxiv
import requests

log = logging.getLogger("scout.ingest")


class _TimeoutSession(requests.Session):
    """A requests Session that applies a default timeout to every request.

    `arxiv` 4.0.1 calls `self._session.get(url, headers=...)` with no timeout and
    exposes no way to pass one, so a hung TCP connection blocks forever. For a
    weekly cron job that is worse than a crash: the run holds an exclusive flock
    for its whole duration, so one hang also stops every later week from running.
    Observed in practice -- a fetch sat for 25 minutes with no progress and no
    error after a transient DNS failure.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def request(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", self._timeout)
        return super().request(*args, **kwargs)


def _bound_request_time(client: arxiv.Client, timeout: float) -> None:
    """Give an arxiv.Client a request timeout, tolerating a future rename.

    Reaches into a private attribute on purpose: an unbounded hang in a cron job
    is a worse problem than depending on `_session`. If a future arxiv version
    renames it, this logs and carries on rather than failing the run -- but the
    hang risk comes back, so the warning names the consequence.
    """
    if not hasattr(client, "_session"):
        log.warning(
            "arxiv.Client has no _session attribute, so no request timeout could "
            "be applied; a hung connection will block this run indefinitely"
        )
        return
    client._session = _TimeoutSession(timeout)

# Matches a trailing version suffix on both modern ids (2609.01234v2) and the
# pre-2007 scheme (quant-ph/0201082v1).
_VERSION_RE = re.compile(r"v(\d+)$")

# arXiv's submittedDate filter wants UTC as YYYYMMDDHHMM.
_ARXIV_TS = "%Y%m%d%H%M"


@dataclass(frozen=True)
class Paper:
    arxiv_id: str  # version stripped
    version: str  # e.g. "v2", or "" when the id carried none
    title: str
    abstract: str
    authors: list[str]
    primary_category: str
    categories: list[str]
    published_utc: str
    updated_utc: str
    abs_url: str
    pdf_url: str
    comment: str | None
    # arXiv exposes this and v2's engagement proxy reads it, so it is stored.
    # Nearly always None for a week-old preprint -- that is the finding, not a bug.
    journal_ref: str | None = None
    # 'primary' if the paper sits in an arxiv.primary category, else 'secondary'.
    source_stream: str = "primary"
    # False for a secondary-category paper that failed the secondary_requires gate.
    gate_passed: bool = True


def strip_version(raw_id: str) -> tuple[str, str]:
    """Split an arXiv id into (base_id, version).

    >>> strip_version("2609.01234v2")
    ('2609.01234', 'v2')
    >>> strip_version("2609.01234")
    ('2609.01234', '')
    >>> strip_version("quant-ph/0201082v1")
    ('quant-ph/0201082', 'v1')
    """
    raw_id = raw_id.strip()
    match = _VERSION_RE.search(raw_id)
    if not match:
        return raw_id, ""
    return raw_id[: match.start()], match.group(0)


def compute_window(
    *,
    now: datetime | None = None,
    window_days: int = 7,
    lookback_days: int = 0,
    since: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the (inclusive) UTC window to query.

    `since` overrides the rolling window -- used by --since for backfills. The
    upper bound is always "now", so a --since in the past widens the window
    rather than shifting it.

    `lookback_days` extends the lower bound only. v1 used a 14-day window for a
    7-day cadence on purpose: arXiv announces papers after a delay, so one
    submitted shortly before a run may not be retrievable until the next one, and
    the overlap plus id dedupe meant nothing was missed and nothing was paid for
    twice. v2 reports on 7 days, which on its own would silently drop every
    late-announced paper. Fetching further back and letting `select_new` discard
    what is already known restores that property at no cost: the extra papers are
    dropped before any model sees them.
    """
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    if lookback_days < 0:
        raise ValueError("lookback_days must be >= 0")
    until = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if since is not None:
        start = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        start = until - timedelta(days=window_days + lookback_days)
    if start >= until:
        raise ValueError(f"empty date window: since={start.isoformat()} >= until={until.isoformat()}")
    return start, until


def build_query(categories: list[str], since: datetime, until: datetime) -> str:
    """Build the arXiv API query string.

    `cat:quant-ph` matches the primary category *and* cross-lists, so no extra
    clause is needed to pick up papers cross-listed into quant-ph.
    """
    if not categories:
        raise ValueError("at least one category is required")
    cats = " OR ".join(f"cat:{c}" for c in categories)
    lo = since.astimezone(timezone.utc).strftime(_ARXIV_TS)
    hi = until.astimezone(timezone.utc).strftime(_ARXIV_TS)
    return f"({cats}) AND submittedDate:[{lo} TO {hi}]"


def _to_paper(result: arxiv.Result) -> Paper:
    base_id, version = strip_version(result.get_short_id())
    return Paper(
        arxiv_id=base_id,
        version=version,
        title=" ".join(result.title.split()),
        abstract=" ".join(result.summary.split()),
        authors=[a.name for a in result.authors],
        primary_category=result.primary_category,
        categories=list(result.categories),
        published_utc=result.published.isoformat() if result.published else "",
        updated_utc=result.updated.isoformat() if result.updated else "",
        abs_url=f"https://arxiv.org/abs/{base_id}",
        pdf_url=result.pdf_url or f"https://arxiv.org/pdf/{base_id}",
        comment=result.comment,
        journal_ref=getattr(result, "journal_ref", None),
    )


# arXiv returns 429 when it decides you have been too eager. The `arxiv` package
# does retry, but only ever waits its fixed `delay_seconds` (3s) between attempts,
# so five retries are exhausted in fifteen seconds and the run dies. A weekly job
# should not lose its digest to a transient rate limit, so retry the whole search
# with real backoff on top.
_RATE_LIMIT_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_retryable_http(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    return isinstance(exc, arxiv.HTTPError) and (
        status is None or status in _RATE_LIMIT_STATUSES
    )


def fetch_papers(
    *,
    categories: list[str],
    since: datetime,
    until: datetime,
    delay_seconds: float = 3.0,
    request_timeout: float = 60.0,
    page_size: int = 100,
    max_results: int = 4000,
    max_attempts: int = 4,
    backoff_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Paper]:
    """Query arXiv for the window and return papers, de-duplicated within the batch.

    `delay_seconds` is passed to arxiv.Client, which sleeps between pages to
    respect arXiv's rate limit. Keep it at >= 3.

    On a 429 or a 5xx the whole search is retried with exponential backoff
    (`backoff_seconds`, doubling). Paging restarts from the beginning, which is
    fine: the query is deterministic and dedupe makes repeats free.
    """
    query = build_query(categories, since, until)
    log.info("arxiv query: %s", query)

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        client = arxiv.Client(
            page_size=page_size, delay_seconds=delay_seconds, num_retries=3
        )
        _bound_request_time(client, request_timeout)
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        seen: set[str] = set()
        papers: list[Paper] = []
        try:
            for result in client.results(search):
                paper = _to_paper(result)
                if paper.arxiv_id in seen:
                    continue
                seen.add(paper.arxiv_id)
                papers.append(paper)
        except Exception as exc:
            if not _is_retryable_http(exc) or attempt == max_attempts:
                raise
            last_error = exc
            wait = backoff_seconds * (2 ** (attempt - 1))
            log.warning(
                "arxiv fetch failed on attempt %d/%d (%s) after %d papers; "
                "waiting %.0fs before retrying",
                attempt, max_attempts, exc, len(papers), wait,
            )
            sleep(wait)
            continue

        log.info("arxiv returned %d unique papers for the window", len(papers))
        if len(papers) >= max_results:
            log.warning(
                "hit max_results=%d -- the window may be truncated; raise "
                "[arxiv].max_results",
                max_results,
            )
        return papers

    raise RuntimeError(f"arxiv fetch failed after {max_attempts} attempts") from last_error


def classify_and_gate(
    papers: list[Paper],
    *,
    primary: tuple[str, ...] | list[str],
    gate: Callable[[str], bool] | None = None,
) -> tuple[list[Paper], list[Paper]]:
    """Split papers into (kept, gated_out).

    A paper in any `primary` category is kept unconditionally. A paper that reached
    us only through a secondary category has to pass `gate` -- the keyword test
    named by `arxiv.secondary_requires` in industries.yaml -- or eight whole
    cond-mat/physics subcategories would flood the pipeline. That gate asked "is
    this quantum" until 2026-09-30 and now asks "is there a measurement".

    Both lists are returned because gated-out papers still get recorded, so dedupe
    never pays to look at them again.
    """
    primary_set = set(primary)
    kept: list[Paper] = []
    gated_out: list[Paper] = []
    for paper in papers:
        if primary_set & set(paper.categories):
            kept.append(replace(paper, source_stream="primary", gate_passed=True))
            continue
        if gate is None or gate(f"{paper.title}\n{paper.abstract}"):
            kept.append(replace(paper, source_stream="secondary", gate_passed=True))
        else:
            gated_out.append(replace(paper, source_stream="secondary", gate_passed=False))
    if gated_out or any(p.source_stream == "secondary" for p in kept):
        log.info(
            "secondary categories: %d kept by the gate, %d rejected",
            sum(1 for p in kept if p.source_stream == "secondary"), len(gated_out),
        )
    return kept, gated_out


def select_new_submissions(papers: list[Paper]) -> tuple[list[Paper], int]:
    """Drop replacements and revised versions, keeping only first submissions.

    Returns (kept, n_excluded). In practice this removes very little -- 4.5% of
    the measured corpus -- because arXiv's `submittedDate` filter already keys on
    the ORIGINAL submission date, so a revision of an older paper never enters
    the window in the first place. It is here for report quality (a re-announced
    old paper should not be presented as this week's news), not for cost.
    """
    kept = [p for p in papers if p.version in ("", "v1")]
    excluded = len(papers) - len(kept)
    if excluded:
        log.info("excluded %d replacements/revised versions (kept v1 only)", excluded)
    return kept, excluded


def select_new(papers: list[Paper], known_ids: set[str]) -> list[Paper]:
    """Drop papers whose (version-stripped) id is already in the database."""
    new = [p for p in papers if p.arxiv_id not in known_ids]
    log.info("%d/%d papers are new (%d already known)", len(new), len(papers), len(papers) - len(new))
    return new
