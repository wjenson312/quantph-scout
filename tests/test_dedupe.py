"""Version stripping and dedupe: the guarantee that the overlapping window is safe."""

import sqlite3
from datetime import datetime, timezone

import pytest

from scout import db
from scout.ingest import Paper, select_new, strip_version


@pytest.mark.parametrize(
    "raw,expected_id,expected_version",
    [
        ("2609.01234v2", "2609.01234", "v2"),
        ("2609.01234v1", "2609.01234", "v1"),
        ("2609.01234v13", "2609.01234", "v13"),
        ("2609.01234", "2609.01234", ""),
        ("quant-ph/0201082v1", "quant-ph/0201082", "v1"),
        ("quant-ph/0201082", "quant-ph/0201082", ""),
        ("  2609.01234v2  ", "2609.01234", "v2"),
    ],
)
def test_strip_version(raw, expected_id, expected_version):
    assert strip_version(raw) == (expected_id, expected_version)


def test_strip_version_does_not_eat_a_trailing_v_without_digits():
    assert strip_version("2609.01234v") == ("2609.01234v", "")


def test_versions_of_one_paper_collapse_to_one_id():
    ids = {strip_version(f"2609.01234v{n}")[0] for n in range(1, 6)}
    assert ids == {"2609.01234"}


def _paper(arxiv_id: str, version: str = "v1") -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        version=version,
        title=f"Title {arxiv_id}",
        abstract="We measured a thing.",
        authors=["A. Author"],
        primary_category="quant-ph",
        categories=["quant-ph"],
        published_utc=datetime(2026, 9, 20, tzinfo=timezone.utc).isoformat(),
        updated_utc=datetime(2026, 9, 20, tzinfo=timezone.utc).isoformat(),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        comment=None,
    )


def test_select_new_skips_known_ids():
    papers = [_paper("2609.00001"), _paper("2609.00002"), _paper("2609.00003")]
    new = select_new(papers, {"2609.00002"})
    assert [p.arxiv_id for p in new] == ["2609.00001", "2609.00003"]


def test_select_new_with_empty_db_keeps_everything():
    papers = [_paper("2609.00001"), _paper("2609.00002")]
    assert len(select_new(papers, set())) == 2


def test_select_new_matches_on_the_stripped_id_not_the_versioned_one():
    """A v2 resubmission of a known paper must not come back as new."""
    v2 = _paper(*strip_version("2609.00001v2"))
    assert select_new([v2], {"2609.00001"}) == []


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_insert_papers_is_idempotent(conn):
    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=True)
    papers = [_paper("2609.00001"), _paper("2609.00002")]

    assert db.insert_papers(conn, papers, run) == 2
    assert db.insert_papers(conn, papers, run) == 0  # second pass inserts nothing
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2


def test_reinserting_a_new_version_does_not_duplicate_or_overwrite(conn):
    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=True)
    db.insert_papers(conn, [_paper("2609.00001", "v1")], run)
    db.insert_papers(conn, [_paper("2609.00001", "v2")], run)

    rows = list(conn.execute("SELECT arxiv_id, latest_version FROM papers"))
    assert len(rows) == 1
    # INSERT OR IGNORE: first_seen data wins, so the paper is not re-triaged.
    assert rows[0]["latest_version"] == "v1"


def test_known_arxiv_ids_round_trips_through_the_db(conn):
    run = db.start_run(conn, since_utc="s", until_utc="u", dry_run=True)
    db.insert_papers(conn, [_paper("2609.00001"), _paper("quant-ph/0201082")], run)
    assert db.known_arxiv_ids(conn) == {"2609.00001", "quant-ph/0201082"}


def test_overlapping_windows_process_each_paper_once(conn):
    """The whole point of the 14-day window with a 7-day cadence."""
    run1 = db.start_run(conn, since_utc="s1", until_utc="u1", dry_run=True)
    week1 = [_paper(f"2609.0000{n}") for n in (1, 2, 3)]
    db.insert_papers(conn, select_new(week1, db.known_arxiv_ids(conn)), run1)

    # Next week's fetch overlaps: it re-returns week1 plus two genuinely new papers.
    run2 = db.start_run(conn, since_utc="s2", until_utc="u2", dry_run=True)
    week2 = week1 + [_paper("2609.00004"), _paper("2609.00005")]
    new = select_new(week2, db.known_arxiv_ids(conn))

    assert [p.arxiv_id for p in new] == ["2609.00004", "2609.00005"]
    db.insert_papers(conn, new, run2)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 5


def test_papers_missing_from_a_late_window_are_still_caught_later(conn):
    """A paper announced after run 1's window closed is picked up by run 2."""
    run1 = db.start_run(conn, since_utc="s1", until_utc="u1", dry_run=True)
    db.insert_papers(conn, select_new([_paper("2609.00001")], set()), run1)

    # arXiv only announced 2609.00002 after run 1 finished; run 2's window still covers it.
    run2 = db.start_run(conn, since_utc="s2", until_utc="u2", dry_run=True)
    late = select_new([_paper("2609.00001"), _paper("2609.00002")], db.known_arxiv_ids(conn))
    assert [p.arxiv_id for p in late] == ["2609.00002"]
    db.insert_papers(conn, late, run2)
    assert db.known_arxiv_ids(conn) == {"2609.00001", "2609.00002"}


# --------------------------------------------------------------------------
# Schema migration: a database created by an earlier version must keep working
# --------------------------------------------------------------------------


def test_missing_columns_are_added_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to an existing table, so a long-lived
    database on the Pi needs columns added explicitly."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, started_utc TEXT NOT NULL, "
        "status TEXT NOT NULL, dry_run INTEGER DEFAULT 0)"
    )
    old.execute(
        "INSERT INTO runs (started_utc, status) VALUES ('2026-09-01T00:00:00+00:00', 'ok')"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for expected in db.MIGRATIONS["runs"]:
        assert expected in columns, f"{expected} was not migrated in"

    # The pre-existing row survives, with NULL/0 for the new columns.
    row = conn.execute("SELECT * FROM runs WHERE id = 1").fetchone()
    assert row["status"] == "ok"
    assert row["api_key_fingerprint"] is None
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    db.connect(path).close()
    conn = db.connect(path)          # second open must not try to re-add columns
    assert db._migrate(conn) == []
    conn.close()


def test_a_fresh_database_needs_no_migration(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    assert db._migrate(conn) == []
    conn.close()


# --------------------------------------------------------------------------
# The arXiv request timeout
# --------------------------------------------------------------------------


def test_the_arxiv_client_gets_a_request_timeout():
    """arxiv 4.0.1 sets no timeout and exposes no way to pass one.

    Without this the fetch can hang forever, and because the run holds an
    exclusive flock for its whole duration, one hang also stops every later week.
    That was observed: a fetch sat for 25 minutes with no progress and no error.
    """
    import arxiv

    from scout.ingest import _bound_request_time

    client = arxiv.Client()
    _bound_request_time(client, 12.5)
    assert client._session._timeout == 12.5


def test_the_bound_session_applies_the_timeout_by_default():
    from scout.ingest import _TimeoutSession

    session = _TimeoutSession(7.0)
    seen = {}

    class Probe(_TimeoutSession):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            seen.update(kwargs)
            return "sent"

    assert Probe(7.0).request("GET", "https://example.invalid") == "sent"
    assert seen["timeout"] == 7.0


def test_an_explicit_timeout_is_not_overridden():
    """setdefault, not assignment -- a caller that passes one keeps it."""
    from scout.ingest import _TimeoutSession

    seen = {}

    class Probe(_TimeoutSession):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            seen.update(kwargs)

    Probe(7.0).request("GET", "https://example.invalid", timeout=1.0)
    assert seen["timeout"] == 1.0


def test_a_missing_session_attribute_warns_instead_of_crashing(caplog):
    """If a future arxiv version renames _session, the run continues -- but the
    hang risk returns, so the warning has to say so."""
    import logging

    from scout.ingest import _bound_request_time

    class Stub:
        pass

    with caplog.at_level(logging.WARNING):
        _bound_request_time(Stub(), 60.0)
    assert "timeout" in caplog.text.lower()
    assert "indefinitely" in caplog.text.lower()
