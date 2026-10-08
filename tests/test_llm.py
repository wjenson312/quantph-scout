"""ModelClient: retries, call logging, usage accounting, and param shedding.

The Anthropic client is stubbed. These paths (a 400 rejecting `thinking`, a 401
that must not be retried, a truncated response) are hard to trigger on purpose
against the real API, which is exactly why they are tested here.
"""

import httpx2
import pytest
from anthropic import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from scout import db
from scout.config import load_config
from scout.llm import ModelCallError, ModelClient
from scout.schemas import TriageBatch, TriageItem

VALID = TriageBatch(
    results=[
        TriageItem(
            ref=1, best_industry="gallium", relevance=3, experimental=True, reason="ok"
        )
    ]
)


class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=100):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, parsed=VALID, stop_reason="end_turn", text='{"ok": true}'):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.usage = FakeUsage()
        self.content = [type("Block", (), {"type": "text", "text": text})()]


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _http_response(status):
    return httpx2.Response(status, request=_request())


def api_error(cls, status, message):
    return cls(message, response=_http_response(status), body=None)


class FakeMessages:
    """Replays a queued script of responses/exceptions, recording each call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, script):
        self.messages = FakeMessages(script)

    async def close(self):
        pass


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def ctx(tmp_path, cfg):
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn, since_utc="s", until_utc="u", dry_run=True)
    usage = db.Usage()
    yield conn, run_id, usage
    conn.close()


def make_client(cfg, ctx, script):
    conn, run_id, usage = ctx
    fake = FakeAnthropic(script)
    client = ModelClient(cfg, conn, run_id, usage, client=fake)
    return client, fake


def calls_logged(conn):
    return list(conn.execute("SELECT * FROM model_calls ORDER BY id"))


async def _call(client, **overrides):
    kwargs = dict(
        stage="triage",
        model="claude-haiku-4-5-20251001",
        system="sys",
        user="usr",
        output_format=TriageBatch,
        max_tokens=1024,
        arxiv_id="2609.00001",
    )
    kwargs.update(overrides)
    return await client.structured(**kwargs)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


async def test_a_successful_call_returns_the_parsed_object(cfg, ctx):
    client, _ = make_client(cfg, ctx, [FakeResponse()])
    assert await _call(client) == VALID


async def test_a_successful_call_is_logged_with_tokens_and_usage(cfg, ctx):
    conn, _, usage = ctx
    client, _ = make_client(cfg, ctx, [FakeResponse()])
    await _call(client)

    rows = calls_logged(conn)
    assert len(rows) == 1
    assert rows[0]["stage"] == "triage"
    assert rows[0]["arxiv_id"] == "2609.00001"
    assert rows[0]["attempt"] == 1
    assert rows[0]["error"] is None
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (1000, 100)
    assert rows[0]["system_prompt"] == "sys"
    assert usage.input_tokens == 1000


async def test_thinking_and_effort_are_forwarded_when_given(cfg, ctx):
    client, fake = make_client(cfg, ctx, [FakeResponse()])
    await _call(client, thinking={"type": "adaptive"}, effort="high")

    call = fake.messages.calls[0]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}


async def test_optional_params_are_omitted_when_not_given(cfg, ctx):
    client, fake = make_client(cfg, ctx, [FakeResponse()])
    await _call(client)

    call = fake.messages.calls[0]
    assert "thinking" not in call
    assert "output_config" not in call


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        api_error(RateLimitError, 429, "slow down"),
        api_error(InternalServerError, 500, "oops"),
        APIConnectionError(request=_request()),
    ],
)
async def test_transient_errors_are_retried_then_succeed(cfg, ctx, error, monkeypatch):
    monkeypatch.setattr("scout.llm._sleep_for", lambda *a: 0.0)
    conn, _, _ = ctx
    client, _ = make_client(cfg, ctx, [error, FakeResponse()])

    assert await _call(client) == VALID
    rows = calls_logged(conn)
    assert len(rows) == 2                 # the failure is logged too
    assert rows[0]["error"] is not None
    assert rows[1]["error"] is None
    assert [r["attempt"] for r in rows] == [1, 2]


async def test_a_schema_validation_failure_is_retried(cfg, ctx, monkeypatch):
    monkeypatch.setattr("scout.llm._sleep_for", lambda *a: 0.0)
    client, _ = make_client(cfg, ctx, [FakeResponse(parsed=None), FakeResponse()])
    assert await _call(client) == VALID


async def test_a_truncated_response_is_retried_not_returned(cfg, ctx, monkeypatch):
    monkeypatch.setattr("scout.llm._sleep_for", lambda *a: 0.0)
    conn, _, _ = ctx
    client, _ = make_client(
        cfg, ctx, [FakeResponse(stop_reason="max_tokens"), FakeResponse()]
    )
    assert await _call(client) == VALID
    assert "max_tokens" in calls_logged(conn)[0]["error"]


async def test_retries_give_up_after_max_attempts(cfg, ctx, monkeypatch):
    monkeypatch.setattr("scout.llm._sleep_for", lambda *a: 0.0)
    conn, _, _ = ctx
    attempts = cfg.retry.max_attempts
    client, _ = make_client(
        cfg, ctx, [api_error(InternalServerError, 500, "oops")] * attempts
    )

    with pytest.raises(ModelCallError, match=f"after {attempts} attempts"):
        await _call(client)
    assert len(calls_logged(conn)) == attempts


async def test_failed_attempts_still_count_toward_the_cost_estimate(cfg, ctx, monkeypatch):
    """A retried call was paid for; the run's cost must say so."""
    monkeypatch.setattr("scout.llm._sleep_for", lambda *a: 0.0)
    _, _, usage = ctx
    client, _ = make_client(
        cfg, ctx, [FakeResponse(stop_reason="max_tokens"), FakeResponse()]
    )
    await _call(client)
    assert usage.input_tokens == 2000  # both attempts


# --------------------------------------------------------------------------
# Non-retryable errors
# --------------------------------------------------------------------------


async def test_an_auth_error_is_not_retried(cfg, ctx):
    conn, _, _ = ctx
    client, fake = make_client(cfg, ctx, [api_error(AuthenticationError, 401, "bad key")])

    with pytest.raises(ModelCallError, match="after 1 attempt"):
        await _call(client)
    assert len(fake.messages.calls) == 1
    assert len(calls_logged(conn)) == 1


async def test_a_refusal_is_not_retried(cfg, ctx):
    client, fake = make_client(cfg, ctx, [FakeResponse(stop_reason="refusal")])
    with pytest.raises(ModelCallError, match="refused"):
        await _call(client)
    assert len(fake.messages.calls) == 1


# --------------------------------------------------------------------------
# Param shedding on a 400
# --------------------------------------------------------------------------


async def test_a_400_naming_thinking_retries_without_the_optional_params(cfg, ctx):
    client, fake = make_client(
        cfg,
        ctx,
        [
            api_error(BadRequestError, 400, "thinking: not supported for this model"),
            FakeResponse(),
        ],
    )
    assert await _call(client, thinking={"type": "adaptive"}, effort="high") == VALID

    first, second = fake.messages.calls
    assert first["thinking"] == {"type": "adaptive"}
    assert "thinking" not in second        # shed on the retry
    assert "output_config" not in second
    assert second["output_format"] is TriageBatch  # structured output is kept


async def test_param_shedding_happens_only_once(cfg, ctx):
    """If it still 400s without the optional params, that is a real error."""
    client, fake = make_client(
        cfg,
        ctx,
        [
            api_error(BadRequestError, 400, "effort: not supported"),
            api_error(BadRequestError, 400, "something else entirely"),
        ],
    )
    with pytest.raises(ModelCallError, match="rejected by"):
        await _call(client, effort="high")
    assert len(fake.messages.calls) == 2


async def test_an_unrelated_400_fails_immediately(cfg, ctx):
    client, fake = make_client(
        cfg, ctx, [api_error(BadRequestError, 400, "prompt is too long")]
    )
    with pytest.raises(ModelCallError, match="rejected by"):
        await _call(client, thinking={"type": "adaptive"})
    assert len(fake.messages.calls) == 1


async def test_a_400_with_no_optional_params_to_shed_fails_immediately(cfg, ctx):
    client, fake = make_client(
        cfg, ctx, [api_error(BadRequestError, 400, "thinking not supported")]
    )
    with pytest.raises(ModelCallError):
        await _call(client)  # no thinking/effort passed, so nothing to shed
    assert len(fake.messages.calls) == 1
