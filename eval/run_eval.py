#!/usr/bin/env python3
"""Measure the triage stage against hand labels.

Fill in eval/labels.csv with your own judgements (arxiv_id, should_flag, notes),
then:

    python eval/run_eval.py

It runs the real triage prompt and model over the labelled papers and reports
precision, recall, and F1 against your labels, plus every disagreement so you can
see whether the prompt or the label is wrong.

Note it exercises the real BATCHED prompt, so with more labels than
[triage].papers_per_request it also tests that batching does not change verdicts --
which is worth knowing, because batching is the one thing in v2 that could quietly
make triage worse.

Papers already in data/scout.db are read from there; the rest are fetched from
arXiv by id. Nothing here writes to the papers/triage tables -- eval runs are
recorded in runs and model_calls only, so they can't pollute a real digest.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import arxiv  # noqa: E402

from scout import db  # noqa: E402
from scout.config import ConfigError, load_config  # noqa: E402
from scout.ingest import Paper, strip_version, _to_paper  # noqa: E402
from scout.llm import ModelClient  # noqa: E402
from scout.logging_setup import setup_logging  # noqa: E402
from scout.industries import load_taxonomy  # noqa: E402
from scout.triage import triage_papers  # noqa: E402

TRUE_VALUES = {"yes", "y", "true", "1", "flag"}
FALSE_VALUES = {"no", "n", "false", "0", "skip"}


@dataclass
class Label:
    arxiv_id: str
    should_flag: bool
    notes: str


def read_labels(path: Path) -> list[Label]:
    if not path.exists():
        raise SystemExit(f"no label file at {path}")
    labels: list[Label] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            raw_id = (row.get("arxiv_id") or "").strip()
            if not raw_id or raw_id.startswith("#"):
                continue
            verdict = (row.get("should_flag") or "").strip().lower()
            if verdict in TRUE_VALUES:
                should_flag = True
            elif verdict in FALSE_VALUES:
                should_flag = False
            else:
                raise SystemExit(
                    f"{path}:{lineno}: should_flag must be yes/no, got {verdict!r}"
                )
            labels.append(
                Label(strip_version(raw_id)[0], should_flag, (row.get("notes") or "").strip())
            )
    if not labels:
        raise SystemExit(f"{path} has no labelled rows yet -- fill it in first")
    return labels


def load_papers(cfg, labels: list[Label]) -> list[Paper]:
    """Prefer the local database; fetch anything missing from arXiv by id."""
    wanted = [label.arxiv_id for label in labels]
    found: dict[str, Paper] = {}

    if cfg.db_path.exists():
        conn = db.connect(cfg.db_path)
        try:
            for arxiv_id in wanted:
                row = db.get_paper(conn, arxiv_id)
                if row is None:
                    continue
                import json

                found[arxiv_id] = Paper(
                    arxiv_id=row["arxiv_id"],
                    version=row["latest_version"] or "",
                    title=row["title"],
                    abstract=row["abstract"],
                    authors=json.loads(row["authors"]),
                    primary_category=row["primary_category"] or "",
                    categories=json.loads(row["categories"]),
                    published_utc=row["published_utc"] or "",
                    updated_utc=row["updated_utc"] or "",
                    abs_url=row["abs_url"] or "",
                    pdf_url=row["pdf_url"] or "",
                    comment=row["comment"],
                    journal_ref=(
                        row["journal_ref"] if "journal_ref" in row.keys() else None
                    ),
                )
        finally:
            conn.close()

    missing = [arxiv_id for arxiv_id in wanted if arxiv_id not in found]
    if missing:
        print(f"fetching {len(missing)} paper(s) from arXiv...", file=sys.stderr)
        client = arxiv.Client(page_size=100, delay_seconds=cfg.arxiv.delay_seconds)
        for result in client.results(arxiv.Search(id_list=missing)):
            paper = _to_paper(result)
            found[paper.arxiv_id] = paper

    still_missing = [arxiv_id for arxiv_id in wanted if arxiv_id not in found]
    if still_missing:
        print(f"WARNING: could not retrieve: {', '.join(still_missing)}", file=sys.stderr)
    return [found[arxiv_id] for arxiv_id in wanted if arxiv_id in found]


def report(labels: list[Label], outcomes: list, failures: list) -> int:
    by_id = {label.arxiv_id: label for label in labels}
    tp = fp = fn = tn = 0
    rows = []

    for outcome in outcomes:
        label = by_id.get(outcome.paper.arxiv_id)
        if label is None:
            continue
        predicted, actual = outcome.shortlisted, label.should_flag
        if predicted and actual:
            tp += 1; verdict = "TP"
        elif predicted and not actual:
            fp += 1; verdict = "FP"
        elif not predicted and actual:
            fn += 1; verdict = "FN"
        else:
            tn += 1; verdict = "TN"
        rows.append((verdict, outcome, label))

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    scored = tp + fp + fn + tn
    accuracy = (tp + tn) / scored if scored else 0.0

    print()
    print(f"scored {scored} labelled paper(s); {len(failures)} triage call(s) failed")
    print(f"  TP {tp}   FP {fp}   FN {fn}   TN {tn}")
    print(f"  precision {precision:.3f}   recall {recall:.3f}   F1 {f1:.3f}   accuracy {accuracy:.3f}")

    disagreements = [row for row in rows if row[0] in ("FP", "FN")]
    if disagreements:
        print()
        print("disagreements:")
        for verdict, outcome, label in disagreements:
            result = outcome.result
            print(f"  [{verdict}] {outcome.paper.arxiv_id}  {outcome.paper.title[:80]}")
            print(
                f"        model: experimental={result.experimental} "
                f"industry={result.best_industry or '(none)'} "
                f"relevance={result.relevance}/3"
            )
            print(f"        model reason: {result.reason}")
            print(f"        your label:   {'flag' if label.should_flag else 'skip'} -- {label.notes}")
    else:
        print()
        print("no disagreements.")

    if failures:
        print()
        print("failed calls:")
        for paper, error in failures:
            print(f"  {paper.arxiv_id}: {error}")

    return 0 if not failures else 1


async def main_async(args) -> int:
    cfg = load_config()
    setup_logging(
        cfg.log_path, level=cfg.logging.level,
        max_bytes=cfg.logging.max_bytes, backup_count=cfg.logging.backup_count,
        verbose=args.verbose,
    )
    labels = read_labels(Path(args.labels))
    papers = load_papers(cfg, labels)
    if not papers:
        raise SystemExit("no papers could be loaded for the labelled ids")

    taxonomy = load_taxonomy(cfg.taxonomy_path)
    print(
        f"triaging {len(papers)} paper(s) with {cfg.models.triage} in batches of "
        f"{cfg.triage.papers_per_request} "
        f"(threshold: relevance >= {cfg.triage.min_relevance} and experimental)",
        file=sys.stderr,
    )

    cfg.require_api_key()  # fail early, before a run row is created

    conn = db.connect(cfg.db_path)
    usage = db.Usage()
    run_id = db.start_run(conn, since_utc="eval", until_utc="eval", dry_run=True)
    client = ModelClient(cfg, conn, run_id, usage)
    try:
        outcomes, failures = await triage_papers(client, cfg, taxonomy, papers)
    finally:
        await client.close()
        db.finish_run(
            conn, run_id, status="ok", stage_reached="eval",
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            estimated_cost_usd=usage.cost_usd(cfg.price),
        )
        conn.close()

    exit_code = report(labels, outcomes, failures)
    print(f"\nest. cost of this eval: ${usage.cost_usd(cfg.price):.4f} (run {run_id})")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels", default=str(ROOT / "eval" / "labels.csv"), help="path to labels.csv"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    try:
        return asyncio.run(main_async(parser.parse_args()))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
