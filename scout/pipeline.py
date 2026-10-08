"""Pipeline orchestration, cheapest stage first.

    ingest -> prefilter -> [budget guard]
           -> tag -> match -> readiness
           -> final -> select -> verify -> digest
           -> self-improvement

Tagging assigns the *route* by which a result could reach a product. Matching then
assigns one industry, and readiness scores only what has one.

**That order is deliberately not the addendum's.** The addendum scores readiness
first and passes its top 20 to matching; on real data that produced an empty report.
Readiness rewards work with no physical barriers, which means software: in the first
run under that order `design_tools` papers averaged 7.5/10 and took 9 of the 20
shortlist slots, matching then dropped 18 of 20 for having no industry, and
verification dropped the last two. Matching first fixes the root cause, because
readiness and industry relevance are orthogonal and only one of them decides whether
a paper is reportable at all.

Stage boundaries are explicit so `--until` can stop cleanly after any of them and
`--dry-run` can stop after triage without making an expensive call.

Everything before `triage` is free: fetch, dedupe, the keyword pre-filter, the
engagement proxies, and the cost projection all run without an API key. That is
what makes `--until triage --dry-run` a useful way to test taxonomy changes, and
it is why the budget guard sits exactly where it does -- the last point at which
the paper count is known and nothing has been spent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import budget as budget_mod
from . import db
from .config import Config
from .digest import (
    DigestData,
    build_entries,
    render_digest,
    render_json,
    render_subject,
    write_digest,
    write_json,
)
from .final import assess_papers
from .improve import ImprovementResult, run_improvement
from .industries import Taxonomy, load_taxonomy
from .ingest import (
    classify_and_gate,
    compute_window,
    fetch_papers,
    select_new,
    select_new_submissions,
)
from .llm import ModelClient
from .mailer import render_html, send_digest
from .matcher import (
    KeywordIndex,
    MatchReport,
    build_index,
    match_papers as match_keywords,
    passes_secondary_gate,
)
from .pipelines import PipelineSet, load_pipelines
from .prefilter import PrefilterReport, apply_prefilter
from .select import Pair, select
from .verify import apply_verifications, verify_papers

log = logging.getLogger("scout.pipeline")

STAGES = ("ingest", "tag", "match", "readiness", "final", "verify", "digest")


class StageFailure(RuntimeError):
    """A stage failed on too large a share of its input for the run to count."""


def check_failure_rate(
    stage: str, attempted: int, succeeded: int, max_rate: float
) -> None:
    """Raise if a stage lost more than max_rate of its papers.

    Nothing attempted is not a failure -- a genuinely quiet week has no new
    papers, and that must still exit 0.
    """
    if attempted == 0:
        return
    failed = attempted - succeeded
    rate = failed / attempted
    if rate > max_rate:
        raise StageFailure(
            f"{stage}: {failed}/{attempted} papers failed ({rate:.0%}), over the "
            f"{max_rate:.0%} threshold in [run].max_failure_rate -- treating the "
            f"run as failed rather than reporting a thin digest"
        )
    if failed:
        log.warning("%s: %d/%d papers failed, within threshold", stage, failed, attempted)


@dataclass
class RunReport:
    run_id: int
    stage_reached: str
    counts: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    projected_cost_usd: float = 0.0
    digest_path: str | None = None
    digest_json_path: str | None = None
    email_preview_path: str | None = None
    failures: dict[str, int] = field(default_factory=dict)
    emailed: bool = False
    match_report: MatchReport | None = None
    prefilter_report: PrefilterReport | None = None
    budget: budget_mod.GuardResult | None = None
    # Pipeline-layer outputs, kept so the digest and the improvement pass can read
    # them without going back to the database.
    tagged: list = field(default_factory=list)
    readiness: Any = None
    matched: list = field(default_factory=list)
    improvement: ImprovementResult | None = None


def _stage_index(stage: str) -> int:
    return STAGES.index(stage)


async def run_pipeline(
    cfg: Config,
    *,
    until: str = "digest",
    since: datetime | None = None,
    dry_run: bool = False,
    no_email: bool = False,
    email_preview: bool = False,
) -> RunReport:
    if until not in STAGES:
        raise ValueError(f"--until must be one of {', '.join(STAGES)}")
    if dry_run and _stage_index(until) > _stage_index("readiness"):
        # Everything up to and including readiness uses the cheap model; the final
        # pass and verification are where Sonnet money starts.
        log.info("--dry-run: stopping after readiness")
        until = "readiness"
    stop_after = _stage_index(until)

    # Load and validate the taxonomy before anything else: a malformed
    # industries.yaml should fail immediately, not after 90s of arXiv paging.
    taxonomy: Taxonomy | None = None
    index: KeywordIndex | None = None
    if cfg.industry.enabled:
        taxonomy = load_taxonomy(cfg.taxonomy_path)
        index = build_index(taxonomy)
    # Validated here for the same reason as the taxonomy: a typo in pipelines.yaml
    # should fail in the first second, not after 90s of arXiv paging.
    pipeline_set: PipelineSet = load_pipelines(cfg.pipelines_path)

    window_since, window_until = compute_window(
        window_days=cfg.arxiv.window_days,
        lookback_days=cfg.arxiv.lookback_days,
        since=since,
    )
    conn = db.connect(cfg.db_path)
    usage = db.Usage()
    run_id = db.start_run(
        conn,
        since_utc=window_since.isoformat(),
        until_utc=window_until.isoformat(),
        dry_run=dry_run,
        api_key_fingerprint=cfg.api_key_fingerprint,
        api_key_source=cfg.api_key_source,
    )
    log.info(
        "API key: fingerprint %s, from %s", cfg.api_key_fingerprint, cfg.api_key_source
    )
    if cfg.api_key_source == "environment":
        log.warning(
            "ANTHROPIC_API_KEY is set in the environment, which OVERRIDES .env -- "
            "this run's spend will appear against that key, not the one in .env"
        )
    report = RunReport(run_id=run_id, stage_reached="ingest")
    log.info(
        "run %d: window %s -> %s (%dd report + %dd dedupe lookback), until=%s, dry_run=%s",
        run_id, window_since.isoformat(), window_until.isoformat(),
        cfg.arxiv.window_days, cfg.arxiv.lookback_days, until, dry_run,
    )

    client: ModelClient | None = None
    try:
        # -- Stage 1: ingest (free) ------------------------------------------
        if taxonomy is not None:
            categories = list(taxonomy.primary) + list(taxonomy.secondary)
            primary = taxonomy.primary
            secondary = taxonomy.secondary
        else:
            categories = list(cfg.arxiv.categories)
            primary = tuple(categories)
            secondary = ()

        papers = fetch_papers(
            categories=categories,
            since=window_since,
            until=window_until,
            delay_seconds=cfg.arxiv.delay_seconds,
            request_timeout=cfg.arxiv.request_timeout,
            page_size=cfg.arxiv.page_size,
            max_results=cfg.arxiv.max_results,
            max_attempts=cfg.arxiv.max_attempts,
            backoff_seconds=cfg.arxiv.backoff_seconds,
        )
        n_excluded_revisions = 0
        if cfg.arxiv.new_submissions_only:
            papers, n_excluded_revisions = select_new_submissions(papers)

        # The seen-papers store: dedupe against every paper ever recorded, which
        # is what makes the lookback window free.
        candidates_for_db = select_new(papers, db.known_arxiv_ids(conn))
        gate = (
            (lambda text: passes_secondary_gate(taxonomy, index, text))
            if taxonomy is not None and index is not None
            else None
        )
        new_papers, gated_out = classify_and_gate(
            candidates_for_db, primary=primary, gate=gate
        )
        db.insert_papers(conn, new_papers + gated_out, run_id)
        report.counts.update(
            n_fetched=len(papers),
            n_new=len(new_papers),
            n_gated_out=len(gated_out),
            n_excluded_revisions=n_excluded_revisions,
        )
        db.update_run(
            conn, run_id, n_fetched=len(papers), n_new=len(new_papers),
            n_gated_out=len(gated_out), n_excluded_revisions=n_excluded_revisions,
        )

        # Keyword pre-filter: free, so it runs over every new paper.
        matches: dict = {}
        if taxonomy is not None and index is not None:
            report.match_report = match_keywords(taxonomy, index, new_papers)
            matches = {m.arxiv_id: m for m in report.match_report.candidates}
            db.insert_industry_matches(conn, run_id, matches, taxonomy)
            db.insert_keyword_hits(
                conn, run_id,
                report.match_report.keyword_counts,
                report.match_report.keyword_owners,
            )
            report.counts.update(
                n_candidates_t1=report.match_report.tier_counts.get(1, 0),
                n_candidates_t2=report.match_report.tier_counts.get(2, 0),
            )
            db.update_run(
                conn, run_id,
                n_candidates_t1=report.match_report.tier_counts.get(1, 0),
                n_candidates_t2=report.match_report.tier_counts.get(2, 0),
            )

        # Engagement proxies: also free, and also before the budget guard.
        survivors, report.prefilter_report = apply_prefilter(
            cfg.prefilter, new_papers, secondary_categories=secondary
        )
        report.counts["n_prefiltered_out"] = report.prefilter_report.dropped
        db.update_run(
            conn, run_id, n_prefiltered_out=report.prefilter_report.dropped
        )

        # Budget guard: the last free moment, so the projection happens here.
        report.budget = budget_mod.apply_guard(cfg, survivors, matches)
        survivors = report.budget.kept
        report.projected_cost_usd = report.budget.projection.total_usd
        report.counts["n_budget_trimmed"] = report.budget.trimmed_count
        report.counts["n_to_triage"] = len(survivors)
        db.update_run(
            conn, run_id,
            n_budget_trimmed=report.budget.trimmed_count,
            projected_cost_usd=report.projected_cost_usd,
        )

        if stop_after == _stage_index("ingest"):
            return _finish(conn, cfg, report, usage, status="ok")

        if taxonomy is None:
            raise RuntimeError(
                "industry matching needs the taxonomy: set [industry] enabled = true"
            )

        # -- Stage A: pipeline tagging (batched, cheap model) ----------------
        report.stage_reached = "tag"
        client = ModelClient(cfg, conn, run_id, usage)
        # Imported late so `--until ingest` needs no API key.
        from .match import match_papers
        from .readiness import score_papers
        from .tagging import tag_papers

        tagged, tag_failures = await tag_papers(client, cfg, pipeline_set, survivors)
        db.insert_pipeline_tags(conn, run_id, tagged)
        kept_tags = [t for t in tagged if not t.dropped]
        report.tagged = tagged
        report.counts.update(
            n_tagged=len(tagged),
            n_tag_dropped=len(tagged) - len(kept_tags),
            n_after_tag=len(kept_tags),
        )
        report.failures["tag"] = len(tag_failures)
        db.update_run(
            conn, run_id, n_tagged=len(tagged),
            n_tag_dropped=len(tagged) - len(kept_tags),
        )
        check_failure_rate(
            "tag", len(survivors), len(tagged), cfg.run.max_failure_rate
        )

        if stop_after == _stage_index("tag"):
            return _finish(conn, cfg, report, usage, status="ok")

        # -- Industry matching (every tagged paper) ---------------------------
        report.stage_reached = "match"
        matched, match_failures = await match_papers(
            client, cfg, taxonomy, pipeline_set, kept_tags
        )
        for outcome in matched:
            db.insert_triage(
                conn, outcome.arxiv_id, run_id, outcome.result, outcome.kept
            )
        kept_matches = [m for m in matched if m.kept]
        report.matched = matched
        report.counts.update(
            n_matched=len(kept_matches),
            n_match_dropped=len(matched) - len(kept_matches),
        )
        report.failures["match"] = len(match_failures)
        db.update_run(
            conn, run_id, n_triaged=len(matched),
            n_match_dropped=len(matched) - len(kept_matches),
        )
        check_failure_rate(
            "match", len(kept_tags), len(matched), cfg.run.max_failure_rate
        )

        if stop_after == _stage_index("match"):
            return _finish(conn, cfg, report, usage, status="ok")

        # -- Readiness scoring and the shortlist (matched papers only) -------
        report.stage_reached = "readiness"
        readiness, readiness_failures = await score_papers(
            client, cfg, pipeline_set, kept_matches
        )
        shortlist_ids = {p.arxiv_id for p in readiness.shortlist}
        db.insert_readiness_scores(conn, run_id, readiness.scored, shortlist_ids)
        report.readiness = readiness
        report.counts.update(
            n_readiness_scored=len(readiness.scored),
            n_shortlisted=len(readiness.shortlist),
        )
        report.failures["readiness"] = len(readiness_failures)
        db.update_run(
            conn, run_id, n_readiness_scored=len(readiness.scored),
            n_shortlisted=len(readiness.shortlist),
        )
        check_failure_rate(
            "readiness", len(kept_matches), len(readiness.scored),
            cfg.run.max_failure_rate,
        )

        if stop_after == _stage_index("readiness"):
            return _finish(conn, cfg, report, usage, status="ok")

        # -- Final pass (prose, abstracts only) ------------------------------
        report.stage_reached = "final"
        rows = _final_rows(conn, readiness.shortlist)
        assessed, final_failures = await assess_papers(client, cfg, taxonomy, rows)
        for outcome in assessed:
            db.insert_assessment(conn, outcome.arxiv_id, run_id, outcome)
        report.counts["n_assessed"] = len(assessed)
        report.failures["final"] = len(final_failures)
        db.update_run(conn, run_id, n_assessed=len(assessed))
        check_failure_rate(
            "final",
            min(len(rows), cfg.final.max_papers),
            len(assessed),
            cfg.run.max_failure_rate,
        )

        if stop_after == _stage_index("final"):
            return _finish(conn, cfg, report, usage, status="ok")

        # -- Selection, then verification of what was selected --------------
        # Selection runs before verification on purpose: verifying 20 papers to
        # report 10 would double that stage's cost for no benefit.
        report.stage_reached = "verify"
        by_id = {o.arxiv_id: o for o in assessed}
        scored_by_id = {p.arxiv_id: p for p in readiness.shortlist}

        def pairs_for(ids: set[str] | None = None) -> list[Pair]:
            out: list[Pair] = []
            for outcome in assessed:
                if ids is not None and outcome.arxiv_id not in ids:
                    continue
                scored = scored_by_id.get(outcome.arxiv_id)
                if scored is None:
                    continue
                out.append(
                    Pair(
                        arxiv_id=outcome.arxiv_id,
                        industry_id=outcome.assessment.best_industry,
                        relevance=scored.matched.result.relevance,
                        disruption=outcome.assessment.disruption,
                        readiness=scored.total,
                        confidence=scored.tagged.tag.confidence,
                        pipeline_id=scored.tagged.tag.primary_pipeline,
                    )
                )
            return out

        selection = select(pairs_for(), limit=cfg.select.max_papers)
        abstracts = {row["arxiv_id"]: row["abstract"] for row in rows}
        chosen = [by_id[p.arxiv_id] for p in selection.selected if p.arxiv_id in by_id]
        verified, verify_failures = await verify_papers(
            client, cfg, taxonomy, chosen, abstracts
        )
        for outcome in verified:
            db.insert_verification(conn, outcome.arxiv_id, run_id, outcome.verification)
        surviving, dropped_ids, _ = apply_verifications(chosen, verified)
        report.counts.update(
            n_verified=len(verified), n_verify_dropped=len(dropped_ids)
        )
        report.failures["verify"] = len(verify_failures)
        db.update_run(
            conn, run_id, n_verified=len(verified), n_verify_dropped=len(dropped_ids)
        )

        # Verification can lower a disruption score and can drop a paper outright,
        # so selection is re-run over everything except the dropped ones. A paper
        # promoted in here was never verified and is marked unverified on the page.
        dropped_set = set(dropped_ids)
        verified_ids = {v.arxiv_id for v in verified}
        final_pairs = select(
            [p for p in pairs_for() if p.arxiv_id not in dropped_set],
            limit=cfg.select.max_papers,
        )
        unverified = {
            p.arxiv_id for p in final_pairs.selected if p.arxiv_id not in verified_ids
        }
        db.insert_selections(
            conn, run_id,
            [(p, p.arxiv_id in verified_ids) for p in final_pairs.selected],
        )
        report.counts["n_selected"] = len(final_pairs.selected)
        db.update_run(conn, run_id, n_selected=len(final_pairs.selected))

        if stop_after == _stage_index("verify"):
            return _finish(conn, cfg, report, usage, status="ok")

        # -- Digest ----------------------------------------------------------
        report.stage_reached = "digest"
        papers_by_id = {
            row["arxiv_id"]: row
            for row in (db.get_paper(conn, p.arxiv_id) for p in final_pairs.selected)
            if row is not None
        }
        # Part 2: the self-improvement pass. Mechanical and free -- it reads this
        # run's own output plus state/feedback.yaml and calls no model.
        if cfg.improve.enabled:
            report.improvement = run_improvement(
                today=window_until.date().isoformat(),
                proposals_path=cfg.proposals_path,
                feedback_path=cfg.feedback_path,
                pipelines_path=cfg.pipelines_path,
                tagged=tagged,
                scored=readiness.scored,
                matched=matched,
                selected_ids={p.arxiv_id for p in final_pairs.selected},
                taxonomy=taxonomy,
                pipelines=pipeline_set,
                dry_run=dry_run,
            )
            report.counts.update(
                n_proposals=len(report.improvement.proposals),
                n_auto_applied=len(report.improvement.applied),
            )
            db.update_run(
                conn, run_id,
                n_proposals=len(report.improvement.proposals),
                n_auto_applied=len(report.improvement.applied),
            )

        entries = build_entries(
            taxonomy, pipeline_set, final_pairs.selected, by_id, papers_by_id,
            unverified, {p.arxiv_id: p for p in readiness.shortlist},
        )
        data = DigestData(
            entries=entries,
            week_of=window_until.date().isoformat(),
            n_fetched=report.counts.get("n_fetched", 0),
            n_evaluated=report.counts.get("n_to_triage", 0),
            n_after_tag=report.counts.get("n_after_tag", 0),
            n_shortlisted=report.counts.get("n_shortlisted", 0),
            max_readiness=pipeline_set.max_score,
            cost_usd=usage.cost_usd(cfg.price),
            counts=dict(report.counts),
            notes=_provenance_notes(report),
            improvement=report.improvement,
        )
        markdown = render_digest(data)
        path = write_digest(cfg, markdown)
        report.digest_path = str(path)
        db.update_run(conn, run_id, digest_path=str(path))

        if cfg.digest.write_json:
            json_path = write_json(cfg, render_json(data))
            report.digest_json_path = str(json_path)
            db.update_run(conn, run_id, digest_json_path=str(json_path))

        subject = render_subject(data, cfg.email.subject_prefix)
        if not no_email:
            report.emailed = send_digest(cfg.email, subject, markdown, path)
        if email_preview:
            # Write the exact HTML that would be sent, without sending it. This
            # is the only way to check the rendering without mailing the list.
            preview = cfg.digest_dir / f"{path.stem}.email.html"
            preview.write_text(render_html(cfg.email, markdown), encoding="utf-8")
            report.email_preview_path = str(preview)
            log.info("email preview written to %s (subject: %s)", preview, subject)

        return _finish(conn, cfg, report, usage, status="ok")

    except Exception as exc:
        log.exception("run %d failed during %s", run_id, report.stage_reached)
        _finish(
            conn, cfg, report, usage, status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if client is not None:
            await client.close()
        conn.close()


def _provenance_notes(report: RunReport) -> list[str]:
    """One-line facts a reader needs to judge the report, not a statistics section."""
    notes: list[str] = []
    pre = report.prefilter_report
    if pre is not None and pre.enabled and pre.dropped:
        notes.append(
            f"{pre.dropped} of {pre.total} new papers were dropped by the free "
            f"pre-filter before any model saw them."
        )
    if report.budget is not None and report.budget.acted:
        notes.append(
            f"Budget guard trimmed {report.budget.trimmed_count} papers to stay "
            f"under the cost cap, so this week's scan was not exhaustive."
        )
    dropped = report.counts.get("n_verify_dropped", 0)
    if dropped:
        notes.append(
            f"{dropped} selected paper{'s' if dropped != 1 else ''} "
            f"{'were' if dropped != 1 else 'was'} removed by the verification pass."
        )
    return notes


def _finish(
    conn,
    cfg: Config,
    report: RunReport,
    usage: db.Usage,
    *,
    status: str,
    error: str | None = None,
) -> RunReport:
    report.cost_usd = usage.cost_usd(cfg.price)
    if report.projected_cost_usd and report.cost_usd:
        log.info(
            "cost: $%.2f actual vs $%.2f projected (%.0f%% of projection)",
            report.cost_usd, report.projected_cost_usd,
            100 * report.cost_usd / report.projected_cost_usd,
        )
    if report.cost_usd > cfg.budget.hard_cap_usd:
        log.error(
            "run cost $%.2f EXCEEDED the $%.2f hard cap -- the projection in "
            "[budget] is too optimistic, re-derive it from this run's model_calls",
            report.cost_usd, cfg.budget.hard_cap_usd,
        )
    db.finish_run(
        conn,
        report.run_id,
        status=status,
        stage_reached=report.stage_reached,
        error=error,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=report.cost_usd,
    )
    return report


def _final_rows(conn, shortlist: list) -> list[dict]:
    """Rows for the final pass, in readiness order, carrying the matched industry.

    Built from the in-memory shortlist rather than re-queried, because the readiness
    score and pipeline tag are not in the `triage` table and the final pass wants
    the industry the match stage actually chose.
    """
    rows: list[dict] = []
    for scored in sorted(shortlist, key=lambda p: -p.total):
        paper = scored.tagged.paper
        rows.append(
            {
                "arxiv_id": paper.arxiv_id,
                "title": paper.title,
                "abstract": paper.abstract,
                "pdf_url": paper.pdf_url,
                "categories": ", ".join(paper.categories),
                "best_industry": scored.matched.result.best_industry,
                "reason": scored.matched.result.reason,
            }
        )
    return rows
