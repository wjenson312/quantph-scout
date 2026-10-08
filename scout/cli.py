"""CLI: `python -m scout run [flags]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import ConfigError, load_config
from .lock import LockBusy, RunLock
from .logging_setup import setup_logging
from .pipeline import STAGES, StageFailure, run_pipeline


def _parse_since(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be YYYY-MM-DD, got {value!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scout",
        description="Weekly industry-impact digest of new arXiv papers "
                    "(quant-ph plus allowlisted cond-mat and physics subcategories).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the pipeline")
    run.add_argument(
        "--until",
        choices=STAGES,
        default="digest",
        help="stop after this stage (default: digest)",
    )
    run.add_argument(
        "--since",
        type=_parse_since,
        metavar="YYYY-MM-DD",
        help="override the rolling date window; for backfills and testing",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="stop after readiness scoring: no final pass, no verification, no email",
    )
    run.add_argument(
        "--no-email", action="store_true", help="render the digest but do not send it"
    )
    run.add_argument(
        "--email-preview",
        action="store_true",
        help="also write the HTML email to digests/<date>.email.html",
    )
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    report = sub.add_parser("report", help="print the triage table for a stored run")
    report.add_argument(
        "--picks",
        action="store_true",
        help="show the selected papers instead of the triage table",
    )
    report.add_argument(
        "--pipelines",
        action="store_true",
        help="show the pipeline tags and readiness scores for a run",
    )
    report.add_argument(
        "--keywords",
        action="store_true",
        help="show the noisiest keywords, as candidates to trim from industries.yaml",
    )
    report.add_argument(
        "--run", type=int, default=None, help="run id (default: the most recent run)"
    )
    report.add_argument(
        "--all",
        action="store_true",
        help="show every triaged paper, not just the shortlist",
    )
    report.add_argument("-v", "--verbose", action="store_true")

    mail = sub.add_parser(
        "email",
        help="render or send an existing digest as email, without running the pipeline",
    )
    mail.add_argument(
        "path",
        nargs="?",
        help="digest Markdown file (default: the newest in digests/)",
    )
    mail.add_argument(
        "--send",
        action="store_true",
        help="actually send it; without this, only the HTML preview is written",
    )
    mail.add_argument(
        "--out",
        default=None,
        help="where to write the HTML preview (default: alongside the digest)",
    )
    mail.add_argument("-v", "--verbose", action="store_true")

    return parser


def _get(row, key, default=0):
    """Read a column that may not exist on a row written by an older version."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _cmd_report(cfg, args) -> int:
    conn = db.connect(cfg.db_path)
    run_id = args.run or db.latest_run_id(conn)
    if run_id is None:
        print("no runs in the database yet", file=sys.stderr)
        return 1

    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        print(f"no such run: {run_id}", file=sys.stderr)
        return 1

    print(
        f"run {row['id']}  status={row['status']}  stage={row['stage_reached']}  "
        f"dry_run={bool(row['dry_run'])}"
    )
    print(f"window {row['since_utc']} -> {row['until_utc']}")
    print(
        f"fetched={row['n_fetched']} new={row['n_new']} triaged={row['n_triaged']} "
        f"tagged={_get(row, 'n_tagged')} shortlisted={row['n_shortlisted']} "
        f"assessed={_get(row, 'n_assessed')} verified={_get(row, 'n_verified')} "
        f"selected={_get(row, 'n_selected')}"
    )
    print(
        f"tag-dropped={_get(row, 'n_tag_dropped')} "
        f"readiness-scored={_get(row, 'n_readiness_scored')} "
        f"match-dropped={_get(row, 'n_match_dropped')} "
        f"proposals={_get(row, 'n_proposals')} "
        f"auto-applied={_get(row, 'n_auto_applied')}"
    )
    print(
        f"tokens in={row['input_tokens']} out={row['output_tokens']} "
        f"est. cost=${row['estimated_cost_usd'] or 0:.4f} "
        f"(projected ${_get(row, 'projected_cost_usd') or 0:.2f})"
    )
    if row["status"] == "running":
        # Totals are written at finish, so an in-flight run reads as zero rather
        # than as free.
        print("NOTE: this run has not finished; token and cost totals are not yet written")
    print(
        f"api key: fingerprint {row['api_key_fingerprint'] or 'unrecorded'} "
        f"(from {row['api_key_source'] or 'unrecorded'})"
    )
    if row["error"]:
        print(f"error: {row['error']}")
    print()

    print(
        f"gated out={row['n_gated_out']} revisions excluded="
        f"{_get(row, 'n_excluded_revisions')} prefiltered out="
        f"{_get(row, 'n_prefiltered_out')} budget trimmed="
        f"{_get(row, 'n_budget_trimmed')}"
    )
    print(
        f"candidates t1={row['n_candidates_t1']} t2={row['n_candidates_t2']} "
        f"verify-dropped={_get(row, 'n_verify_dropped')}"
    )
    print()

    if args.picks:
        picks = db.selections_for_run(conn, run_id)
        print(f"{len(picks)} selected paper(s), in report order:")
        for pick in picks:
            mark = " " if pick["verified"] else "?"
            print(
                f"  [{mark}] #{pick['rank']:2} d={pick['disruption']} "
                f"r={pick['relevance']} {pick['arxiv_id']}  {pick['industry_id']}"
            )
            print(f"          {pick['title'][:88]}")
        if any(not pick["verified"] for pick in picks):
            print("  ('?' = not checked by the verification pass)")
        conn.close()
        return 0

    if args.pipelines:
        scores = db.readiness_for_run(conn, run_id)
        if not scores:
            print("no readiness scores recorded for this run")
        else:
            print(f"{len(scores)} scored paper(s), best first "
                  f"('*' = reached the shortlist):")
            for row in scores:
                mark = "*" if row["shortlisted"] else " "
                print(
                    f"  [{mark}] {row['total']:2}/{row['max_score']} "
                    f"{row['arxiv_id']}  {(row['primary_pipeline'] or '-'):22} "
                    f"weakest={row['most_limiting'] or '-'}"
                )
                print(f"          {row['title'][:86]}")
        conn.close()
        return 0

    if args.keywords:
        noisy = db.noisiest_keywords(conn, run_id, limit=25)
        if not noisy:
            print("no keyword hits recorded for this run")
        else:
            print("noisiest keywords (candidates to trim from industries.yaml):")
            for hit in noisy:
                owners = ", ".join(json.loads(hit["owners"]))[:58]
                print(f"  {hit['paper_count']:5}  {hit['keyword']:30} -> {owners}")
        conn.close()
        return 0

    rows = (
        db.triaged_for_run(conn, run_id)
        if args.all
        else db.shortlisted_for_run(conn, run_id)
    )
    label = "triaged" if args.all else "shortlisted"
    print(f"{len(rows)} {label} paper(s):")
    for paper in rows:
        flags = "E" if paper["is_experimental"] else "-"
        flags += "*" if paper["shortlisted"] else " "
        industry = paper["best_industry"] or "-"
        print(
            f"  [{flags}] r={paper['relevance'] if paper['relevance'] is not None else paper['applied_relevance']}"
            f" {paper['arxiv_id']}  {industry[:28]:28} {paper['title'][:52]}"
        )
        print(f"          {paper['reason'][:160]}")
    conn.close()
    return 0


def _cmd_email(cfg, args) -> int:
    """Render (and optionally send) a digest that already exists on disk.

    Separate from `run` because the two failure modes are completely different:
    a pipeline run costs money and takes minutes, whereas an SMTP or rendering
    problem is worth iterating on in seconds. This is also the only safe way to
    re-send an issue after fixing a mail problem -- nothing is recomputed.
    """
    from .digest import compose_subject
    from .mailer import render_html, send_digest

    if args.path:
        path = Path(args.path)
    else:
        candidates = sorted(cfg.digest_dir.glob("[0-9]*.md"))
        if not candidates:
            print(f"no digests found in {cfg.digest_dir}", file=sys.stderr)
            return 1
        path = candidates[-1]
    if not path.exists():
        print(f"no such digest: {path}", file=sys.stderr)
        return 1

    markdown = path.read_text(encoding="utf-8")
    # The subject normally comes from render_subject() over the structured
    # entries. Those are gone once we have only Markdown, so recover the same
    # parts from the first item and feed them through the same composer, rather
    # than silently sending a dated stub.
    industry = title = ""
    for line in markdown.split("\n"):
        if not industry and line.startswith("## ") and ". " in line:
            industry = line.split(". ", 1)[1].strip()
        if not title and line.startswith("**Paper:** ["):
            title = line.split("[", 1)[1].split("](", 1)[0]
        if industry and title:
            break
    n_more = max(0, markdown.count("\n**Paper:** ") - 1)
    subject = (
        compose_subject(cfg.email.subject_prefix, industry, title, n_more)
        if title
        else f"{cfg.email.subject_prefix} {path.stem}".strip()
    )

    out = Path(args.out) if args.out else path.with_suffix(".email.html")
    out.write_text(render_html(cfg.email, markdown), encoding="utf-8")
    print(f"subject: {subject}")
    print(f"preview: {out}")

    if not args.send:
        print("not sent (pass --send to deliver it)")
        return 0
    if not cfg.email.enabled:
        print(
            "email is not configured: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD "
            "and SMTP_TO",
            file=sys.stderr,
        )
        return 2
    send_digest(cfg.email, subject, markdown, path)
    print(f"sent to {len(cfg.email.recipients)} recipient(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    log = setup_logging(
        cfg.log_path,
        level=cfg.logging.level,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
        verbose=args.verbose,
    )

    if args.command == "report":
        return _cmd_report(cfg, args)
    if args.command == "email":
        return _cmd_email(cfg, args)

    try:
        # The lock is held for the whole run, so an overrunning Monday job can
        # never be joined by the next one.
        with RunLock(cfg.lock_path):
            report = asyncio.run(
                run_pipeline(
                    cfg,
                    until=args.until,
                    since=args.since,
                    dry_run=args.dry_run,
                    no_email=args.no_email or args.dry_run,
                    email_preview=args.email_preview,
                )
            )
    except LockBusy as exc:
        log.warning("%s", exc)
        return 0  # not a failure: the other run is doing the work
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except StageFailure as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    except Exception:
        log.error("run failed; see the traceback above and the model_calls table")
        return 1

    counts = report.counts
    log.info(
        "run %d finished at stage %s: %s; est. cost $%.4f%s",
        report.run_id,
        report.stage_reached,
        ", ".join(f"{k}={v}" for k, v in counts.items()),
        report.cost_usd,
        f"; digest {report.digest_path}" if report.digest_path else "",
    )
    failed = sum(report.failures.values())
    if failed:
        log.warning(
            "%d paper(s) failed along the way: %s",
            failed,
            ", ".join(f"{k}={v}" for k, v in report.failures.items() if v),
        )
    if report.stage_reached == "triage":
        log.info("review the triage output with: python -m scout report --run %d", report.run_id)
    return 0
