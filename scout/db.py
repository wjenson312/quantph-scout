"""SQLite storage. One file, data/scout.db, created on first use.

Tables:
  papers        one row per arXiv paper ever seen (version-stripped id = PK).
                Membership here is what dedupe checks -- the seen-papers store.
  triage        cheap-pass verdict, one row per (paper, run).
  assessments   final-pass output, one row per (paper, run).
  verifications independent check of a selected paper, one row per (paper, run).
  selections    what actually made the report, with its rank.
  runs          one row per pipeline invocation, with counts, tokens, cost, status.
  model_calls   every request/response pair, for debugging and cost forensics.

`analyses`, `critiques` and `industry_links` are v1 tables. Their DDL is retained
so an existing database still opens and run 1's history stays queryable with plain
SQL, but v2 never writes to them and the functions that used to are gone.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id         TEXT PRIMARY KEY,   -- version stripped, e.g. 2609.01234
    latest_version   TEXT,               -- e.g. v2, as first seen
    title            TEXT NOT NULL,
    abstract         TEXT NOT NULL,
    authors          TEXT NOT NULL,      -- JSON array
    primary_category TEXT,
    categories       TEXT NOT NULL,      -- JSON array
    published_utc    TEXT,
    updated_utc      TEXT,
    abs_url          TEXT,
    pdf_url          TEXT,
    comment          TEXT,
    -- arXiv exposes this; v2's engagement proxy reads it. Almost always NULL for a
    -- week-old preprint, which is the measured reason that proxy is off by default.
    journal_ref      TEXT,
    -- 'primary' if the paper is in an arxiv.primary category, else 'secondary'.
    source_stream    TEXT NOT NULL DEFAULT 'primary',
    -- 0 for a secondary-category paper that failed the secondary_requires gate. Such a
    -- paper is still recorded, so dedupe never re-examines it; widen the gate and
    -- DELETE these rows to reconsider them.
    gate_passed      INTEGER NOT NULL DEFAULT 1,
    first_seen_utc   TEXT NOT NULL,
    first_seen_run   INTEGER
);

CREATE TABLE IF NOT EXISTS triage (
    id                      INTEGER PRIMARY KEY,
    arxiv_id                TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id                  INTEGER NOT NULL,
    is_experimental         INTEGER NOT NULL,
    reports_measured_result INTEGER NOT NULL,
    -- 1-5 in v1, 0-3 from v2 on. Prefer `relevance` below for v2 data.
    applied_relevance       INTEGER NOT NULL,
    -- v2: the single chosen industry, and relevance on the 0-3 scale.
    best_industry           TEXT,
    relevance               INTEGER,
    reason                  TEXT NOT NULL,
    shortlisted             INTEGER NOT NULL,
    created_utc             TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_triage_run ON triage (run_id, shortlisted);

CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY,
    arxiv_id        TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id          INTEGER NOT NULL,
    payload         TEXT NOT NULL,      -- Analysis as JSON
    fulltext_source TEXT,               -- 'html' | 'pdf'
    fulltext_chars  INTEGER,
    truncated       INTEGER NOT NULL DEFAULT 0,
    created_utc     TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);

CREATE TABLE IF NOT EXISTS critiques (
    id                INTEGER PRIMARY KEY,
    arxiv_id          TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id            INTEGER NOT NULL,
    analysis_id       INTEGER NOT NULL REFERENCES analyses(id),
    payload           TEXT NOT NULL,    -- Critique as JSON (incl. revised_analysis)
    adjusted_relevance INTEGER NOT NULL,
    confidence        TEXT NOT NULL,    -- from revised_analysis
    score             REAL NOT NULL,    -- relevance * confidence weight
    created_utc       TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);

-- One row per (paper, matched target) from the keyword pre-filter. Written for
-- every candidate, whether or not it later scored well enough to be shown.
CREATE TABLE IF NOT EXISTS industry_matches (
    id               INTEGER PRIMARY KEY,
    arxiv_id         TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id           INTEGER NOT NULL,
    target_id        TEXT NOT NULL,     -- industry id, or 'group:<id>'
    target_kind      TEXT NOT NULL,     -- 'industry' | 'group'
    tier             INTEGER NOT NULL,
    is_candidate     INTEGER NOT NULL,  -- 0 for tier-3 context tags
    matched_keywords TEXT NOT NULL,     -- JSON array
    created_utc      TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_imatch_run ON industry_matches (run_id, tier);

-- Scored links. Every candidate the model saw is stored, including score-0 ones,
-- so a bad keyword can be judged on the model's own reasoning.
CREATE TABLE IF NOT EXISTS industry_links (
    id            INTEGER PRIMARY KEY,
    arxiv_id      TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id        INTEGER NOT NULL,
    target_id     TEXT NOT NULL,
    target_kind   TEXT NOT NULL,
    name          TEXT NOT NULL,
    tier          INTEGER NOT NULL,
    score         INTEGER NOT NULL,
    justification TEXT NOT NULL,
    kept          INTEGER NOT NULL,     -- 1 if it reaches the digest
    created_utc   TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_ilink_run ON industry_links (run_id, kept, score);

-- Per-keyword hit counts for a run, so noisy terms can be trimmed from the YAML.
CREATE TABLE IF NOT EXISTS keyword_hits (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL,
    keyword     TEXT NOT NULL,
    paper_count INTEGER NOT NULL,
    owners      TEXT NOT NULL,          -- JSON array of industry/group ids it can tag
    created_utc TEXT NOT NULL,
    UNIQUE (run_id, keyword)
);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY,
    started_utc        TEXT NOT NULL,
    finished_utc       TEXT,
    status             TEXT NOT NULL,   -- running | ok | failed | locked
    stage_reached      TEXT,
    dry_run            INTEGER NOT NULL DEFAULT 0,
    since_utc          TEXT,
    until_utc          TEXT,
    n_fetched          INTEGER DEFAULT 0,
    n_new              INTEGER DEFAULT 0,
    n_triaged          INTEGER DEFAULT 0,
    n_shortlisted      INTEGER DEFAULT 0,
    n_analyzed         INTEGER DEFAULT 0,
    n_critiqued        INTEGER DEFAULT 0,
    n_gated_out        INTEGER DEFAULT 0,
    n_candidates_t1    INTEGER DEFAULT 0,
    n_candidates_t2    INTEGER DEFAULT 0,
    n_industry_scored  INTEGER DEFAULT 0,
    n_industry_links   INTEGER DEFAULT 0,
    input_tokens       INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    digest_path        TEXT,
    error              TEXT,
    -- Which API key this run used: a SHA-256 prefix, never the key itself, plus
    -- whether it came from .env or from an overriding environment variable.
    api_key_fingerprint TEXT,
    api_key_source      TEXT
);

CREATE TABLE IF NOT EXISTS model_calls (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL,
    arxiv_id      TEXT,
    stage         TEXT NOT NULL,
    model         TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    system_prompt TEXT,
    user_prompt   TEXT,
    response      TEXT,
    stop_reason   TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    -- Recorded so prompt-caching effectiveness can be measured rather than
    -- assumed. Caching is off by default; a nonzero cache_read here is the only
    -- evidence that turning it on did anything.
    cache_read_input_tokens     INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    latency_ms    INTEGER,
    error         TEXT,
    created_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_run ON model_calls (run_id, stage);

-- v2: the final pass. One row per paper that got the expensive call, whether or
-- not it was later selected, so a paper that just missed the cut is still on
-- record with its reasoning.
CREATE TABLE IF NOT EXISTS assessments (
    id              INTEGER PRIMARY KEY,
    arxiv_id        TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id          INTEGER NOT NULL,
    best_industry   TEXT NOT NULL,
    disruption      INTEGER NOT NULL,
    payload         TEXT NOT NULL,      -- FinalAssessment as JSON
    used_fulltext   INTEGER NOT NULL DEFAULT 0,
    fulltext_source TEXT,               -- 'html' | 'pdf', null when abstract-only
    fulltext_chars  INTEGER,
    created_utc     TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_assess_run ON assessments (run_id, disruption);

-- v2: the verification pass over the selected papers.
CREATE TABLE IF NOT EXISTS verifications (
    id                  INTEGER PRIMARY KEY,
    arxiv_id            TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id              INTEGER NOT NULL,
    verdict             TEXT NOT NULL,   -- ok | corrected | drop
    industry_fits       INTEGER NOT NULL,
    n_checks            INTEGER NOT NULL,
    n_unsupported       INTEGER NOT NULL,
    adjusted_disruption INTEGER NOT NULL,
    payload             TEXT NOT NULL,   -- Verification as JSON
    created_utc         TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);

-- Pipeline layer, Stage A. One row per (paper, run): the route by which the
-- result could reach a product. `dropped` records a 'none' tag rather than
-- deleting the row, so the self-improvement pass can see what was discarded.
CREATE TABLE IF NOT EXISTS pipeline_tags (
    id                 INTEGER PRIMARY KEY,
    arxiv_id           TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id             INTEGER NOT NULL,
    primary_pipeline   TEXT NOT NULL,
    secondary_pipeline TEXT,
    confidence         TEXT NOT NULL,
    signal             TEXT NOT NULL,
    dropped            INTEGER NOT NULL DEFAULT 0,
    created_utc        TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_tags_run ON pipeline_tags (run_id, primary_pipeline);

-- Pipeline layer, Stage B. The total plus the per-check breakdown as JSON, so a
-- check that has gone inert can be found later without re-scoring anything.
CREATE TABLE IF NOT EXISTS readiness_scores (
    id            INTEGER PRIMARY KEY,
    arxiv_id      TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id        INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    max_score     INTEGER NOT NULL,
    most_limiting TEXT,
    concreteness  INTEGER,
    shortlisted   INTEGER NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL,     -- ReadinessScore as JSON
    created_utc   TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_readiness_run
    ON readiness_scores (run_id, total DESC);

-- v2: what reached the report, in order. One row per selected (paper, industry).
CREATE TABLE IF NOT EXISTS selections (
    id          INTEGER PRIMARY KEY,
    arxiv_id    TEXT NOT NULL REFERENCES papers(arxiv_id),
    run_id      INTEGER NOT NULL,
    industry_id TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    relevance   INTEGER NOT NULL,
    disruption  INTEGER NOT NULL,
    readiness   INTEGER,
    pipeline_id TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    created_utc TEXT NOT NULL,
    UNIQUE (arxiv_id, run_id),
    -- The no-duplicate-industry rule, enforced by the database as well as by
    -- select.py. If a refactor ever breaks the greedy loop, this fails loudly
    -- instead of quietly shipping a report with one industry twice.
    UNIQUE (run_id, industry_id)
);
"""


def utcnow() -> str:
    """Timestamps are stored as ISO-8601 UTC strings, so they sort lexically."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does nothing
# to a table that already exists, so a database created by an earlier version needs
# these added explicitly -- otherwise the first query naming one fails with
# "No item with that key" on a machine that has been running for weeks.
#
# Append-only: never remove or retype an entry here, add a new one. SQLite's ADD
# COLUMN requires a constant default, which is why they are all nullable or 0.
MIGRATIONS: dict[str, dict[str, str]] = {
    "papers": {
        "source_stream": "TEXT NOT NULL DEFAULT 'primary'",
        "gate_passed": "INTEGER NOT NULL DEFAULT 1",
        # v2: the engagement proxy reads this. arXiv exposed it all along; v1
        # simply never stored it.
        "journal_ref": "TEXT",
    },
    "runs": {
        "n_gated_out": "INTEGER DEFAULT 0",
        "n_candidates_t1": "INTEGER DEFAULT 0",
        "n_candidates_t2": "INTEGER DEFAULT 0",
        "n_industry_scored": "INTEGER DEFAULT 0",
        "n_industry_links": "INTEGER DEFAULT 0",
        "api_key_fingerprint": "TEXT",
        "api_key_source": "TEXT",
        # v2 counts.
        "n_excluded_revisions": "INTEGER DEFAULT 0",
        "n_prefiltered_out": "INTEGER DEFAULT 0",
        "n_assessed": "INTEGER DEFAULT 0",
        "n_verified": "INTEGER DEFAULT 0",
        "n_verify_dropped": "INTEGER DEFAULT 0",
        "n_selected": "INTEGER DEFAULT 0",
        "n_budget_trimmed": "INTEGER DEFAULT 0",
        # Pipeline layer counts.
        "n_tagged": "INTEGER DEFAULT 0",
        "n_tag_dropped": "INTEGER DEFAULT 0",
        "n_readiness_scored": "INTEGER DEFAULT 0",
        "n_match_dropped": "INTEGER DEFAULT 0",
        "n_proposals": "INTEGER DEFAULT 0",
        "n_auto_applied": "INTEGER DEFAULT 0",
        "projected_cost_usd": "REAL DEFAULT 0.0",
        "digest_json_path": "TEXT",
    },
    # v2's triage output shape. The v1 columns are kept and still written, so
    # run 1 stays queryable, but `relevance` and `best_industry` are the v2
    # columns to query.
    #
    # WATCH THE SCALE: `applied_relevance` is 1-5 for run 1 and 0-3 from v2
    # onward, because v2 changed the scale. Any query spanning both must filter on
    # run_id, or join `runs`. `relevance` only ever holds the 0-3 scale, which is
    # why it exists as its own column rather than reusing the old one.
    "triage": {
        "best_industry": "TEXT",
        "relevance": "INTEGER",
    },
    "selections": {
        "readiness": "INTEGER",
        "pipeline_id": "TEXT",
    },
    # v2 measures prompt-cache effectiveness rather than assuming it. Caching is
    # off by default (Haiku 4.5 needs a 4,096-token prefix and v2's is ~1,300),
    # but the columns exist so `cache_read_input_tokens` can be checked rather
    # than guessed at if that ever changes.
    "model_calls": {
        "cache_read_input_tokens": "INTEGER DEFAULT 0",
        "cache_creation_input_tokens": "INTEGER DEFAULT 0",
    },
}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any columns missing from an existing database. Returns what was added."""
    added: list[str] = []
    for table, columns in MIGRATIONS.items():
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            continue  # table doesn't exist yet; SCHEMA just created it correctly
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                added.append(f"{table}.{column}")
    if added:
        conn.commit()
    return added


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    added = _migrate(conn)
    if added:
        logging.getLogger("scout.db").info(
            "migrated %s: added %s", path.name, ", ".join(added)
        )
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Token / cost accounting
# --------------------------------------------------------------------------


@dataclass
class Usage:
    """Accumulates token spend across a run, per model."""

    per_model: dict[str, list[int]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.per_model is None:
            self.per_model = {}

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        entry = self.per_model.setdefault(model, [0, 0])
        entry[0] += input_tokens
        entry[1] += output_tokens

    @property
    def input_tokens(self) -> int:
        return sum(v[0] for v in self.per_model.values())

    @property
    def output_tokens(self) -> int:
        return sum(v[1] for v in self.per_model.values())

    def cost_usd(self, price_for: Any) -> float:
        total = 0.0
        for model, (inp, out) in self.per_model.items():
            price = price_for(model)
            total += inp / 1_000_000 * price.input + out / 1_000_000 * price.output
        return round(total, 4)


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def start_run(
    conn: sqlite3.Connection,
    *,
    since_utc: str,
    until_utc: str,
    dry_run: bool,
    api_key_fingerprint: str | None = None,
    api_key_source: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_utc, status, dry_run, since_utc, until_utc, "
        "api_key_fingerprint, api_key_source) VALUES (?, 'running', ?, ?, ?, ?, ?)",
        (
            utcnow(), int(dry_run), since_utc, until_utc,
            api_key_fingerprint, api_key_source,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id)
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    stage_reached: str | None = None,
    error: str | None = None,
    **fields: Any,
) -> None:
    update_run(
        conn,
        run_id,
        finished_utc=utcnow(),
        status=status,
        stage_reached=stage_reached,
        error=error,
        **fields,
    )


# --------------------------------------------------------------------------
# Papers
# --------------------------------------------------------------------------


def known_arxiv_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT arxiv_id FROM papers")}


def insert_papers(conn: sqlite3.Connection, papers: Iterable[Any], run_id: int) -> int:
    """Insert new papers; existing ids are left untouched. Returns rows inserted."""
    now = utcnow()
    rows = [
        (
            p.arxiv_id,
            p.version,
            p.title,
            p.abstract,
            json.dumps(p.authors),
            p.primary_category,
            json.dumps(p.categories),
            p.published_utc,
            p.updated_utc,
            p.abs_url,
            p.pdf_url,
            p.comment,
            getattr(p, "journal_ref", None),
            getattr(p, "source_stream", "primary"),
            int(getattr(p, "gate_passed", True)),
            now,
            run_id,
        )
        for p in papers
    ]
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO papers (arxiv_id, latest_version, title, abstract, "
        "authors, primary_category, categories, published_utc, updated_utc, abs_url, "
        "pdf_url, comment, journal_ref, source_stream, gate_passed, first_seen_utc, "
        "first_seen_run) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def get_paper(conn: sqlite3.Connection, arxiv_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,)
    ).fetchone()


# --------------------------------------------------------------------------
# Triage / analyses / critiques
# --------------------------------------------------------------------------


def insert_triage(
    conn: sqlite3.Connection, arxiv_id: str, run_id: int, result: Any, shortlisted: bool
) -> None:
    """Store one triage verdict.

    v2 collapsed v1's two booleans (is_experimental, reports_measured_result) into
    one `experimental`, because in practice they almost never disagreed -- on run
    1's 998 papers they differed on 6. Both legacy columns are written from that
    single value so the table stays NOT NULL-satisfiable and old queries still run.
    """
    experimental = int(result.experimental)
    conn.execute(
        "INSERT OR REPLACE INTO triage (arxiv_id, run_id, is_experimental, "
        "reports_measured_result, applied_relevance, best_industry, relevance, "
        "reason, shortlisted, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            arxiv_id,
            run_id,
            experimental,
            experimental,
            int(result.relevance),
            result.best_industry,
            int(result.relevance),
            result.reason,
            int(shortlisted),
            utcnow(),
        ),
    )
    conn.commit()


def shortlisted_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Shortlisted papers for the final pass, best first.

    `categories` is decoded here rather than in the pipeline so the final pass
    receives a display string and does not have to know the storage format.
    """
    rows = list(
        conn.execute(
            "SELECT t.*, p.title, p.abstract, p.abs_url, p.pdf_url, p.categories, "
            "p.authors, p.latest_version "
            "FROM triage t JOIN papers p USING (arxiv_id) "
            "WHERE t.run_id = ? AND t.shortlisted = 1 "
            "ORDER BY t.relevance DESC, t.arxiv_id",
            (run_id,),
        )
    )
    out = []
    for row in rows:
        item = dict(row)
        item["categories"] = ", ".join(_json_list(item.get("categories")))
        out.append(item)
    return out


def _json_list(raw: Any) -> list[str]:
    """Decode a stored JSON array, tolerating null and malformed values."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


# --------------------------------------------------------------------------
# v2: assessments, verifications, selections
# --------------------------------------------------------------------------


def insert_assessment(
    conn: sqlite3.Connection, arxiv_id: str, run_id: int, outcome: Any
) -> int:
    assessment = outcome.assessment
    fulltext = outcome.fulltext
    cur = conn.execute(
        "INSERT OR REPLACE INTO assessments (arxiv_id, run_id, best_industry, "
        "disruption, payload, used_fulltext, fulltext_source, fulltext_chars, "
        "created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            arxiv_id,
            run_id,
            assessment.best_industry,
            int(assessment.disruption),
            assessment.model_dump_json(),
            int(fulltext is not None),
            fulltext.source if fulltext is not None else None,
            fulltext.chars if fulltext is not None else None,
            utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_verification(
    conn: sqlite3.Connection, arxiv_id: str, run_id: int, verification: Any
) -> int:
    checks = list(verification.number_checks)
    cur = conn.execute(
        "INSERT OR REPLACE INTO verifications (arxiv_id, run_id, verdict, "
        "industry_fits, n_checks, n_unsupported, adjusted_disruption, payload, "
        "created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            arxiv_id,
            run_id,
            verification.verdict,
            int(verification.industry_fits),
            len(checks),
            sum(1 for c in checks if not c.supported),
            int(verification.adjusted_disruption),
            verification.model_dump_json(),
            utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_selections(conn: sqlite3.Connection, run_id: int, pairs: list) -> int:
    """Record the selected (paper, industry) pairs in report order.

    The table's UNIQUE (run_id, industry_id) is what makes the no-duplicate rule
    a database invariant rather than a promise made by select.py.
    """
    now = utcnow()
    rows = [
        (
            pair.arxiv_id, run_id, pair.industry_id, rank,
            int(pair.relevance), int(pair.disruption),
            int(getattr(pair, "readiness", 0) or 0),
            getattr(pair, "pipeline_id", "") or None,
            int(verified), now,
        )
        for rank, (pair, verified) in enumerate(pairs, start=1)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO selections (arxiv_id, run_id, industry_id, rank, "
        "relevance, disruption, readiness, pipeline_id, verified, created_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)
def log_model_call(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    arxiv_id: str | None,
    stage: str,
    model: str,
    attempt: int,
    system_prompt: str | None,
    user_prompt: str | None,
    response: str | None,
    stop_reason: str | None,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    error: str | None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO model_calls (run_id, arxiv_id, stage, model, attempt, "
        "system_prompt, user_prompt, response, stop_reason, input_tokens, "
        "output_tokens, cache_read_input_tokens, cache_creation_input_tokens, "
        "latency_ms, error, created_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            arxiv_id,
            stage,
            model,
            attempt,
            system_prompt,
            user_prompt,
            response,
            stop_reason,
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
            latency_ms,
            error,
            utcnow(),
        ),
    )
    conn.commit()


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    return int(row[0]) if row and row[0] is not None else None


# --------------------------------------------------------------------------
# Industry taxonomy: keyword matches, scored links, keyword noise
# --------------------------------------------------------------------------
def noisiest_keywords(
    conn: sqlite3.Connection, run_id: int, limit: int = 20
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT keyword, paper_count, owners FROM keyword_hits "
            "WHERE run_id = ? AND owners != '[]' ORDER BY paper_count DESC LIMIT ?",
            (run_id, limit),
        )
    )


def triaged_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Every triaged paper for a run, best first -- the `report --all` view."""
    rows = conn.execute(
        "SELECT t.*, p.title, p.abstract, p.abs_url, p.pdf_url, p.categories, "
        "p.authors, p.latest_version "
        "FROM triage t JOIN papers p USING (arxiv_id) "
        "WHERE t.run_id = ? "
        "ORDER BY t.relevance DESC, t.arxiv_id",
        (run_id,),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["categories"] = ", ".join(_json_list(item.get("categories")))
        out.append(item)
    return out


def assessments_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT a.*, p.title, p.abs_url, p.authors, p.published_utc, p.comment "
            "FROM assessments a JOIN papers p USING (arxiv_id) "
            "WHERE a.run_id = ? ORDER BY a.disruption DESC, a.arxiv_id",
            (run_id,),
        )
    )


def selections_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT s.*, p.title, p.abs_url, p.authors, a.payload "
            "FROM selections s JOIN papers p USING (arxiv_id) "
            "LEFT JOIN assessments a ON a.arxiv_id = s.arxiv_id AND a.run_id = s.run_id "
            "WHERE s.run_id = ? ORDER BY s.rank",
            (run_id,),
        )
    )


def insert_industry_matches(
    conn: sqlite3.Connection, run_id: int, matches: dict, taxonomy: Any = None
) -> None:
    """Record what the keyword pre-filter concluded, one row per (paper, target).

    Each row carries the *target's own* tier, not the paper's overall candidacy
    tier: a paper can match a tier-1 industry and a tier-2 industry at once, and
    recording both as tier 1 would make the stored evidence useless for judging
    which keyword earned its place.

    Tier-3 tags are deliberately NOT stored. v2 removed tier-3 tagging; the
    entries remain in industries.yaml but are inert, so writing rows for them
    would reintroduce the thing that was removed.

    `taxonomy` is optional so this still works with the taxonomy layer disabled,
    in which case a target's tier falls back to the paper's.
    """
    now = utcnow()
    rows = []
    for arxiv_id, match in matches.items():
        paper_tier = match.tier or 0

        def tier_of(industry_id: str) -> int:
            if taxonomy is None:
                return paper_tier
            industry = taxonomy.industries.get(industry_id)
            return industry.tier if industry is not None else paper_tier

        for industry_id, keywords in match.industries.items():
            rows.append(
                (arxiv_id, run_id, industry_id, "industry", tier_of(industry_id),
                 1, json.dumps(keywords), now)
            )
        for group_id, keywords in match.groups.items():
            rows.append(
                (arxiv_id, run_id, f"group:{group_id}", "group", paper_tier,
                 1, json.dumps(keywords), now)
            )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO industry_matches (arxiv_id, run_id, target_id, "
            "target_kind, tier, is_candidate, matched_keywords, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()


def insert_keyword_hits(
    conn: sqlite3.Connection, run_id: int, counts: dict, owners: dict, min_count: int = 1
) -> None:
    now = utcnow()
    rows = [
        (run_id, keyword, count, json.dumps(sorted(owners.get(keyword, ()))), now)
        for keyword, count in counts.items()
        if count >= min_count
    ]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO keyword_hits (run_id, keyword, paper_count, "
            "owners, created_utc) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()


# --------------------------------------------------------------------------
# Pipeline layer: tags and readiness scores
# --------------------------------------------------------------------------


def insert_pipeline_tags(conn: sqlite3.Connection, run_id: int, outcomes: list) -> int:
    """Store every tag, including `none` ones.

    Dropped papers are recorded rather than omitted: the self-improvement pass
    looks for papers with an obvious use that were tagged `none`, and it cannot
    find them if they were never written down.
    """
    now = utcnow()
    rows = [
        (
            o.paper.arxiv_id, run_id, o.tag.primary_pipeline,
            o.tag.secondary_pipeline or None, o.tag.confidence, o.tag.signal,
            int(o.dropped), now,
        )
        for o in outcomes
    ]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO pipeline_tags (arxiv_id, run_id, primary_pipeline, "
            "secondary_pipeline, confidence, signal, dropped, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows)


def insert_readiness_scores(
    conn: sqlite3.Connection, run_id: int, scored: list, shortlisted: set[str]
) -> int:
    now = utcnow()
    rows: list[tuple] = []
    for paper in scored:
        rows.append(
            (
                paper.arxiv_id, run_id, paper.total, _max_score_hint(paper),
                paper.weakest(), paper.score.concreteness,
                int(paper.arxiv_id in shortlisted),
                paper.score.model_dump_json(), now,
            )
        )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO readiness_scores (arxiv_id, run_id, total, "
            "max_score, most_limiting, concreteness, shortlisted, payload, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows)


def _max_score_hint(paper: Any) -> int:
    """Two points per check, derived from the answer itself."""
    return 2 * len(paper.score.checks)


def pipeline_tags_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT t.*, p.title FROM pipeline_tags t JOIN papers p USING (arxiv_id) "
            "WHERE t.run_id = ? ORDER BY t.primary_pipeline, t.arxiv_id",
            (run_id,),
        )
    )


def readiness_for_run(
    conn: sqlite3.Connection, run_id: int, *, shortlisted_only: bool = False
) -> list[sqlite3.Row]:
    clause = " AND r.shortlisted = 1" if shortlisted_only else ""
    return list(
        conn.execute(
            f"SELECT r.*, p.title, t.primary_pipeline FROM readiness_scores r "
            "JOIN papers p USING (arxiv_id) "
            "LEFT JOIN pipeline_tags t ON t.arxiv_id = r.arxiv_id AND t.run_id = r.run_id "
            f"WHERE r.run_id = ?{clause} ORDER BY r.total DESC, r.arxiv_id",
            (run_id,),
        )
    )
