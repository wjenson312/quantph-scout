"""Date-window logic. The window must be computed at runtime, never hard-coded."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.ingest import build_query, compute_window


def test_default_window_is_window_days_back_from_now():
    now = datetime(2026, 9, 29, 15, 30, tzinfo=timezone.utc)
    since, until = compute_window(now=now, window_days=14)
    assert until == now
    assert since == datetime(2026, 9, 15, 15, 30, tzinfo=timezone.utc)
    assert until - since == timedelta(days=14)


def test_window_days_is_configurable():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    since, _ = compute_window(now=now, window_days=7)
    assert since == datetime(2026, 9, 22, tzinfo=timezone.utc)


def test_window_tracks_the_clock_not_a_constant():
    """Two calls a day apart must produce windows a day apart."""
    base = datetime(2026, 9, 29, tzinfo=timezone.utc)
    first = compute_window(now=base, window_days=14)
    second = compute_window(now=base + timedelta(days=1), window_days=14)
    assert second[0] - first[0] == timedelta(days=1)
    assert second[1] - first[1] == timedelta(days=1)


def test_no_now_argument_uses_the_real_clock():
    before = datetime.now(timezone.utc)
    _, until = compute_window(window_days=14)
    after = datetime.now(timezone.utc)
    assert before <= until <= after


def test_since_overrides_the_lower_bound_only():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    since, until = compute_window(
        now=now, window_days=14, since=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    assert since == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert until == now  # --since widens the window, it does not shift it


def test_naive_since_is_treated_as_utc():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    since, _ = compute_window(now=now, since=datetime(2026, 9, 1))
    assert since == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_since_in_a_local_timezone_is_converted():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    chicago = timezone(timedelta(hours=-5))
    since, _ = compute_window(now=now, since=datetime(2026, 9, 1, 20, 0, tzinfo=chicago))
    assert since == datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)


def test_empty_window_is_rejected():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="empty date window"):
        compute_window(now=now, since=datetime(2026, 10, 5, tzinfo=timezone.utc))


def test_zero_window_days_is_rejected():
    with pytest.raises(ValueError, match="window_days"):
        compute_window(now=datetime(2026, 9, 29, tzinfo=timezone.utc), window_days=0)


def test_query_includes_cross_lists_and_a_utc_date_range():
    since = datetime(2026, 9, 15, 15, 30, tzinfo=timezone.utc)
    until = datetime(2026, 9, 29, 15, 30, tzinfo=timezone.utc)
    query = build_query(["quant-ph"], since, until)
    # `cat:` matches primary category AND cross-lists, so no extra clause is needed.
    assert query == "(cat:quant-ph) AND submittedDate:[202609151530 TO 202609291530]"


def test_query_handles_multiple_categories():
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    until = datetime(2026, 9, 2, tzinfo=timezone.utc)
    query = build_query(["quant-ph", "cond-mat.mes-hall"], since, until)
    assert query.startswith("(cat:quant-ph OR cat:cond-mat.mes-hall)")


def test_query_converts_non_utc_bounds_to_utc():
    chicago = timezone(timedelta(hours=-5))
    since = datetime(2026, 9, 15, 19, 0, tzinfo=chicago)  # = 2026-09-16 00:00 UTC
    until = datetime(2026, 9, 16, 19, 0, tzinfo=chicago)
    assert "submittedDate:[202609160000 TO 202609170000]" in build_query(
        ["quant-ph"], since, until
    )


def test_empty_category_list_is_rejected():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="category"):
        build_query([], now - timedelta(days=1), now)
