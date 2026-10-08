"""The one place that talks to the Anthropic API.

Every call goes through `ModelClient.structured`, which:
  * asks for a strict JSON schema derived from a Pydantic model
    (`messages.parse(output_format=...)`) and returns a validated instance;
  * retries with exponential backoff + jitter on transport errors, rate limits,
    5xx, and on a response that fails schema validation;
  * writes one row to model_calls per attempt -- success or failure -- so a bad
    run can be debugged after the fact without re-spending money;
  * accumulates token usage for the run's cost estimate.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, TypeVar

import anthropic
import pydantic
from anthropic import AsyncAnthropic

from .config import Config
from .db import Usage, log_model_call

log = logging.getLogger("scout.llm")

T = TypeVar("T", bound=pydantic.BaseModel)

# Errors worth trying again: transient transport problems, rate limits, and
# server-side failures. 400/404 are not here on purpose -- retrying a malformed
# request just burns time.
_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class ModelCallError(RuntimeError):
    """A model call that did not produce a valid result after every retry."""


# Substrings that mark a 400 as "this model won't take these optional params
# alongside structured output". Param support varies by model -- Haiku 4.5 rejects
# `effort`, for instance -- so rather than encode a compatibility matrix that goes
# stale, drop the optional params once and retry. The warning tells you to fix
# config.toml so the retry isn't paid for every week.
_PARAM_REJECTION_MARKERS = (
    "thinking",
    "output_config",
    "effort",
    "output_format",
    "not supported",
)


def _sleep_for(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter, so concurrent workers don't sync up."""
    return random.uniform(0.0, min(cap, base * (2 ** (attempt - 1))))


class ModelClient:
    def __init__(
        self,
        cfg: Config,
        conn: Any,
        run_id: int,
        usage: Usage,
        *,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.run_id = run_id
        self.usage = usage
        self._client = client or AsyncAnthropic(
            api_key=cfg.require_api_key(),
            # The SDK retries 429/5xx itself; keep that low and let our own loop,
            # which also retries validation failures, own the backoff policy.
            max_retries=1,
        )

    async def close(self) -> None:
        await self._client.close()

    async def structured(
        self,
        *,
        stage: str,
        model: str,
        system: str,
        user: str,
        output_format: type[T],
        max_tokens: int,
        arxiv_id: str | None = None,
        thinking: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> T:
        """One structured call, retried. Raises ModelCallError if it never lands."""
        retry = self.cfg.retry
        last_error: Exception | None = None
        attempt = 0
        # Flipped to True if the model rejects the optional tuning params.
        drop_optional_params = False

        for attempt in range(1, retry.max_attempts + 1):
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "output_format": output_format,
            }
            if thinking and not drop_optional_params:
                kwargs["thinking"] = thinking
            if effort and not drop_optional_params:
                kwargs["output_config"] = {"effort": effort}

            response = None
            text = None
            try:
                response = await self._client.messages.parse(**kwargs)
                text = "".join(
                    block.text for block in response.content if block.type == "text"
                )

                if response.stop_reason == "refusal":
                    raise ModelCallError(
                        f"model refused the request (stop_reason=refusal) for {arxiv_id or stage}"
                    )
                if response.stop_reason == "max_tokens":
                    raise ValueError(
                        f"response hit max_tokens={max_tokens} and is truncated; "
                        "raise max_tokens for this stage"
                    )

                parsed = response.parsed_output
                if parsed is None:
                    raise ValueError("response contained no parsable JSON output")

                self._record(
                    stage=stage,
                    model=model,
                    arxiv_id=arxiv_id,
                    attempt=attempt,
                    system=system,
                    user=user,
                    response=text,
                    stop_reason=response.stop_reason,
                    started=started,
                    resp=response,
                    error=None,
                )
                return parsed

            except ModelCallError as exc:
                # A refusal will not change on retry.
                self._record(
                    stage=stage, model=model, arxiv_id=arxiv_id, attempt=attempt,
                    system=system, user=user, response=text, resp=response,
                    stop_reason=getattr(response, "stop_reason", None),
                    started=started, error=str(exc),
                )
                raise

            except anthropic.BadRequestError as exc:
                message = str(exc).lower()
                can_shed = (
                    (thinking or effort)
                    and not drop_optional_params
                    and any(marker in message for marker in _PARAM_REJECTION_MARKERS)
                )
                self._record(
                    stage=stage, model=model, arxiv_id=arxiv_id, attempt=attempt,
                    system=system, user=user, response=None, resp=None,
                    stop_reason=None, started=started,
                    error=f"BadRequestError: {exc}",
                )
                if not can_shed:
                    raise ModelCallError(
                        f"{stage} request rejected by {model}: {exc}"
                    ) from exc
                log.warning(
                    "%s: %s rejected thinking/effort (%s); retrying without them. "
                    "Set [%s].effort = \"\" in config.toml to stop paying for this retry.",
                    stage, model, exc, stage,
                )
                drop_optional_params = True
                last_error = exc
                continue

            except (
                *_RETRYABLE,
                pydantic.ValidationError,
                ValueError,
                anthropic.APIStatusError,
            ) as exc:
                last_error = exc
                retryable = isinstance(
                    exc, (*_RETRYABLE, pydantic.ValidationError, ValueError)
                ) or (
                    isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500
                )
                self._record(
                    stage=stage, model=model, arxiv_id=arxiv_id, attempt=attempt,
                    system=system, user=user, response=text, resp=response,
                    stop_reason=getattr(response, "stop_reason", None),
                    started=started, error=f"{type(exc).__name__}: {exc}",
                )
                if not retryable or attempt == retry.max_attempts:
                    break
                delay = _sleep_for(attempt, retry.base_delay, retry.max_delay)
                log.warning(
                    "%s %s attempt %d/%d failed (%s); retrying in %.1fs",
                    stage, arxiv_id or "-", attempt, retry.max_attempts,
                    type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)

        plural = "attempt" if attempt == 1 else "attempts"
        raise ModelCallError(
            f"{stage} failed for {arxiv_id or '-'} after {attempt} {plural}: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    def _record(
        self,
        *,
        stage: str,
        model: str,
        arxiv_id: str | None,
        attempt: int,
        system: str,
        user: str,
        response: str | None,
        stop_reason: str | None,
        started: float,
        resp: Any,
        error: str | None,
    ) -> None:
        input_tokens = output_tokens = 0
        cache_read = cache_write = 0
        if resp is not None and getattr(resp, "usage", None) is not None:
            input_tokens = int(getattr(resp.usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(resp.usage, "output_tokens", 0) or 0)
            # Recorded so prompt caching can be judged on evidence. These are 0
            # unless caching is enabled, which it is not by default -- Haiku 4.5
            # needs a 4,096-token prefix and triage's is ~1,300.
            cache_read = int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
            cache_write = int(getattr(resp.usage, "cache_creation_input_tokens", 0) or 0)
            # Failed attempts still cost money, so they count toward the run total.
            self.usage.add(model, input_tokens, output_tokens)
        log_model_call(
            self.conn,
            run_id=self.run_id,
            arxiv_id=arxiv_id,
            stage=stage,
            model=model,
            attempt=attempt,
            system_prompt=system,
            user_prompt=user,
            response=response,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=error,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )


async def gather_limited(limit: int, coros: list[Any]) -> list[Any]:
    """Run coroutines with at most `limit` in flight; exceptions come back as values."""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def guarded(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(guarded(c) for c in coros), return_exceptions=True)
