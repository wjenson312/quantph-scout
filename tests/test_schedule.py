"""The retry ladder's invariants.

The gate itself is shell inside the workflow, so what is tested here is the
thing that actually rots: the cron lines. Every rung is written in UTC but
judged in local time, which means a rung can drift into the wrong local day
without anyone noticing -- and the failure mode is a silent week, not a red
build. These tests pin the ladder in both halves of the daylight-saving year.

The gate's decision is reimplemented in `_gate` below rather than shelled out
to, because the point is to assert the *schedule* satisfies the gate, not to
re-test `date`. `_gate` must stay a faithful transcription of the `case "$dow"`
block in .github/workflows/weekly.yml.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "weekly.yml"

# A Monday comfortably inside each half of the year, so the rungs are read
# once under CDT (UTC-5) and once under CST (UTC-6).
SUMMER_MONDAY = date(2026, 7, 6)
WINTER_MONDAY = date(2026, 11, 16)


def _workflow() -> dict:
    # `on` is a YAML 1.1 boolean, so the key parses as True. GitHub's own
    # parser handles this; ours has to know about it.
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _env() -> dict[str, str]:
    return {k: str(v) for k, v in _workflow()["env"].items()}


def _crons() -> list[str]:
    return [entry["cron"] for entry in _workflow()[True]["schedule"]]


def _parse(cron: str) -> tuple[int, int]:
    """(hour UTC, ISO weekday UTC) for the fixed-point crons this file uses."""
    minute, hour, dom, month, dow = re.split(r"\s+", cron.strip())
    assert (minute, dom, month) == ("0", "*", "*"), f"unexpected cron shape: {cron}"
    return int(hour), int(dow)


def _fire(reference_monday: date, cron: str) -> datetime:
    """When this cron fires, in the local timezone, for a given UTC Monday."""
    hour, dow = _parse(cron)
    day = reference_monday + timedelta(days=dow - 1)
    fired = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return fired.astimezone(ZoneInfo(_env()["TARGET_TZ"]))


def _gate(now_local: datetime, sent: str, target_hour: int) -> str:
    """The gate's verdict: "send", "catchup", or a reason to skip."""
    today = now_local.date().isoformat()
    yesterday = (now_local.date() - timedelta(days=1)).isoformat()
    if sent == today:
        return "skip: already sent today"
    if now_local.isoweekday() == 1:
        if now_local.hour < target_hour:
            return "skip: too early"
        return "send"
    if now_local.isoweekday() == 2:
        if sent == yesterday:
            return "skip: monday covered"
        return "catchup"
    return "skip: wrong day"


ALL_MONDAYS = pytest.mark.parametrize("reference", [SUMMER_MONDAY, WINTER_MONDAY],
                                      ids=["cdt", "cst"])


@ALL_MONDAYS
def test_every_rung_but_the_last_is_monday_local(reference: date) -> None:
    """The two late rungs are Tuesday in UTC and must still be Monday locally."""
    *rungs, catchup = _crons()
    for cron in rungs:
        assert _fire(reference, cron).isoweekday() == 1, f"{cron} is not Monday locally"
    assert _fire(reference, catchup).isoweekday() == 2, "the catch-up must be Tuesday"


@ALL_MONDAYS
def test_the_digest_goes_out_once_at_the_target_hour(reference: date) -> None:
    """Happy path: the first rung to pass the gate lands on TARGET_HOUR, and
    every later rung stands down."""
    env = _env()
    target_hour = int(env["TARGET_HOUR"])
    sent = "none"
    sends: list[datetime] = []
    for cron in _crons():
        fired = _fire(reference, cron)
        if _gate(fired, sent, target_hour) in {"send", "catchup"}:
            sends.append(fired)
            sent = fired.date().isoformat()
    assert len(sends) == 1, f"expected exactly one send, got {sends}"
    assert sends[0].hour == target_hour
    assert sends[0].isoweekday() == 1


@ALL_MONDAYS
def test_a_late_delivery_still_sends(reference: date) -> None:
    """The bug this ladder replaced: a rung delivered an hour or three late was
    read as the wrong side of daylight saving and exited green, costing a week.
    Lateness must never be a reason to skip."""
    target_hour = int(_env()["TARGET_HOUR"])
    on_time = next(
        _fire(reference, cron) for cron in _crons()
        if _gate(_fire(reference, cron), "none", target_hour) == "send"
    )
    for delay in (1, 2, 3, 5):
        late = on_time + timedelta(hours=delay)
        if late.isoweekday() != 1:
            continue  # pushed out of Monday entirely; the catch-up covers that
        assert _gate(late, "none", target_hour) == "send", f"{delay}h late was skipped"


@ALL_MONDAYS
def test_the_catchup_fires_only_when_monday_produced_nothing(reference: date) -> None:
    catchup = _fire(reference, _crons()[-1])
    monday = (catchup.date() - timedelta(days=1)).isoformat()
    target_hour = int(_env()["TARGET_HOUR"])
    assert _gate(catchup, monday, target_hour) == "skip: monday covered"
    assert _gate(catchup, "none", target_hour) == "catchup"


@ALL_MONDAYS
def test_every_rung_is_tried_when_each_one_fails(reference: date) -> None:
    """With no marker ever written -- the pipeline failing every time -- the
    ladder must offer every rung rather than standing down after the first."""
    target_hour = int(_env()["TARGET_HOUR"])
    attempts = [
        cron for cron in _crons()
        if _gate(_fire(reference, cron), "none", target_hour) in {"send", "catchup"}
    ]
    # One rung stands down in winter: 15:00 UTC is 09:00 CST, before the target.
    expected = len(_crons()) if reference is SUMMER_MONDAY else len(_crons()) - 1
    assert len(attempts) == expected


def test_the_list_id_the_mailer_reads_is_actually_set() -> None:
    """scout/config.py reads EMAIL_LIST_ID and omits the List-Id header when it
    is empty, so an unset variable silently undoes a deliverability fix."""
    assert _env().get("EMAIL_LIST_ID"), "EMAIL_LIST_ID must be set in the workflow env"


def _step(job: str, name_fragment: str) -> dict:
    for step in _workflow()["jobs"][job]["steps"]:
        if name_fragment in step.get("name", ""):
            return step
    raise AssertionError(f"no step matching {name_fragment!r} in job {job!r}")


def test_only_a_real_send_records_a_send() -> None:
    """A `preview` writes no marker, and a `resend` -- the free deliverability
    test -- must not suppress that Monday's real digest either."""
    condition = _step("digest", "Record that the digest went out")["if"]
    assert "needs.schedule-gate.outputs.mode == 'send'" in condition
    for mode in ("preview", "resend", "collect", "seed", "dry-run"):
        assert mode not in condition


def test_the_marker_refuses_to_lie_about_an_unconfigured_send() -> None:
    """send_digest() returns False without raising when mail is unconfigured,
    so the marker step has to check the recipient list itself."""
    step = _step("digest", "Record that the digest went out")
    assert "SMTP_TO" in step["env"]
    assert 'if [ -z "${SMTP_TO:-}" ]' in step["run"]
    assert "exit 1" in step["run"]


def test_the_alarm_only_fires_for_scheduled_failures() -> None:
    """A failed manual `collect` is a human watching a terminal, not a silent
    week; it must not mail anyone."""
    condition = _workflow()["jobs"]["alarm"]["if"]
    assert "always()" in condition
    assert "github.event_name == 'schedule'" in condition
    assert "needs.digest.result == 'failure'" in condition
