"""Shared machinery for the batched, ref-matched stages.

Three stages now send many papers per request (pipeline tagging, readiness scoring,
industry matching). Each one has to solve the same problem: a batched response can
come back short, long, or reordered, and matching results to papers by *position*
would silently hand paper 1's verdict to paper 3. That is the failure mode that
makes batching dangerous, so the alignment logic lives here once rather than being
reimplemented per stage.

The contract every batched stage follows:
  * papers are numbered 1..n in the prompt and the model echoes the number as `ref`;
  * results are matched by `ref`, never by position;
  * anything unanswered is retried in a smaller batch before being called a failure.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from .llm import gather_limited

log = logging.getLogger("scout.batching")

T = TypeVar("T")


def chunk(items: Sequence[T], size: int) -> list[list[T]]:
    """Split into batches of at most `size`. A nonsense size yields singletons."""
    size = max(1, size)
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def align(
    items: Sequence[T], results: Sequence[Any], *, label: str = "batch"
) -> tuple[list[tuple[T, Any]], list[T]]:
    """Match results to items by echoed 1-based `ref`. Returns (paired, unanswered).

    A ref outside the range, or a duplicate, is discarded rather than guessed at:
    the first answer for a ref wins and the rest are dropped, because a model that
    answered twice for one paper has told us nothing about which answer it meant.
    """
    by_ref: dict[int, Any] = {}
    for result in results:
        ref = getattr(result, "ref", None)
        if isinstance(ref, int) and 1 <= ref <= len(items) and ref not in by_ref:
            by_ref[ref] = result

    paired: list[tuple[T, Any]] = []
    unanswered: list[T] = []
    for n, item in enumerate(items, start=1):
        found = by_ref.get(n)
        if found is None:
            unanswered.append(item)
        else:
            paired.append((item, found))
    if unanswered:
        log.warning(
            "%s answered %d/%d; %d will be retried individually",
            label, len(paired), len(items), len(unanswered),
        )
    return paired, unanswered


async def run_batched(
    items: Sequence[T],
    *,
    batch_size: int,
    concurrency: int,
    call: Callable[[list[T]], Awaitable[tuple[list[Any], list[T]]]],
    stage: str,
) -> tuple[list[Any], list[tuple[T, Exception]]]:
    """Run `call` over batches, then retry anything unanswered one item at a time.

    `call` takes a batch and returns (outcomes, unanswered). One uncooperative
    response then costs a few cents of retries rather than a whole batch of papers.
    """
    if not items:
        return [], []

    outcomes: list[Any] = []
    failures: list[tuple[T, Exception]] = []

    async def round_of(groups: list[list[T]]) -> list[T]:
        results = await gather_limited(concurrency, [call(g) for g in groups])
        retry: list[T] = []
        for group, result in zip(groups, results):
            if isinstance(result, BaseException):
                log.error("%s batch of %d failed: %s", stage, len(group), result)
                # Every paper in a failed batch is reported, not just one standing
                # in for the group -- otherwise the failure-rate guard undercounts.
                failures.extend((item, result) for item in group)  # type: ignore[misc]
                continue
            produced, unanswered = result
            outcomes.extend(produced)
            retry.extend(unanswered)
        return retry

    leftover = await round_of(chunk(items, batch_size))
    if leftover:
        log.info("%s: retrying %d unanswered items individually", stage, len(leftover))
        still_missing = await round_of([[item] for item in leftover])
        failures.extend(
            (item, RuntimeError(f"{stage} returned no result")) for item in still_missing
        )
    return outcomes, failures
