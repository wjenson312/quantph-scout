"""Report rendering: the Markdown digest and its machine-readable sibling.

The format is fixed by the brief and deliberately short -- one section per
selected paper, four lines each, nothing else. Resist adding sections: the whole
point of v2's report is that it is selective enough to read in full.

Two things here are not in the brief and are worth knowing about:

* a one-line **provenance footer**, because a report that quietly cost $4.80 or
  quietly trimmed 900 papers to fit a budget should say so on the page rather
  than only in the database;
* an `unverified` marker on any entry the verification pass could not check, so a
  failed check is visible instead of looking like a passed one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import Config
from .industries import Taxonomy
from .pipelines import PipelineSet
from .schemas import FinalAssessment

log = logging.getLogger("scout.digest")

EMPTY_REPORT = "No highly relevant papers this week."


@dataclass
class Entry:
    """One selected paper, ready to render."""

    arxiv_id: str
    title: str
    abs_url: str
    first_author: str
    institution: str
    industry_id: str
    industry_name: str
    relevance: int
    disruption: int
    assessment: FinalAssessment
    verified: bool = True
    # Pipeline layer.
    pipeline_id: str = ""
    pipeline_name: str = ""
    readiness: int = 0
    max_readiness: int = 10
    # The limiting readiness check and its note, which is what "the catch" leans on.
    most_limiting: str = ""
    limiting_note: str = ""


@dataclass
class DigestData:
    entries: list[Entry] = field(default_factory=list)
    week_of: str = ""
    # Papers returned by the arXiv query, most of which are usually already known.
    n_fetched: int = 0
    # Funnel counts for the opening line.
    n_after_tag: int = 0
    n_shortlisted: int = 0
    max_readiness: int = 10
    improvement: Any = None
    # Papers actually evaluated this run, after dedupe. This is the honest
    # denominator: quoting n_fetched alone reads as "1168 scanned, 2 picked" when
    # 1048 of those had been seen and judged in an earlier week.
    n_evaluated: int = 0
    cost_usd: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _first_author(authors: Any) -> str:
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except (TypeError, ValueError):
            return authors.split(",")[0].strip()
    if isinstance(authors, list) and authors:
        return str(authors[0])
    return ""


def _institution(comment: str | None) -> str:
    """arXiv has no institution field.

    v1 did not store one either, and the brief says "institution if in metadata"
    -- so this returns empty rather than guessing from an affiliation string that
    is not there. Left as a seam: if institution ever becomes available, fill it
    here and the template picks it up.
    """
    return ""


def build_entries(
    taxonomy: Taxonomy | None,
    pipelines: PipelineSet | None,
    selected: list,
    assessments: dict[str, Any],
    papers: dict[str, Any],
    unverified: set[str],
    scored: dict[str, Any] | None = None,
) -> list[Entry]:
    """Assemble render-ready entries, ordered by readiness as the addendum requires."""
    entries: list[Entry] = []
    for pair in selected:
        outcome = assessments.get(pair.arxiv_id)
        paper = papers.get(pair.arxiv_id)
        if outcome is None or paper is None:
            continue
        industry = (
            taxonomy.industries.get(pair.industry_id) if taxonomy is not None else None
        )
        pipeline = (
            pipelines.pipelines.get(pair.pipeline_id) if pipelines is not None else None
        )
        paper_score = (scored or {}).get(pair.arxiv_id)
        limiting = paper_score.weakest() if paper_score is not None else ""
        entries.append(
            Entry(
                arxiv_id=pair.arxiv_id,
                title=paper["title"],
                abs_url=paper["abs_url"] or f"https://arxiv.org/abs/{pair.arxiv_id}",
                first_author=_first_author(paper["authors"]),
                institution=_institution(paper["comment"] if "comment" in paper.keys() else None),
                industry_id=pair.industry_id,
                industry_name=industry.name if industry else pair.industry_id,
                relevance=pair.relevance,
                disruption=pair.disruption,
                assessment=outcome.assessment,
                verified=pair.arxiv_id not in unverified,
                pipeline_id=pair.pipeline_id,
                pipeline_name=pipeline.name if pipeline else pair.pipeline_id,
                readiness=pair.readiness,
                max_readiness=pipelines.max_score if pipelines is not None else 10,
                most_limiting=limiting,
                limiting_note=(
                    paper_score.note_for(limiting) if paper_score is not None else ""
                ),
            )
        )
    entries.sort(key=_entry_order)
    return entries


def _entry_order(entry: Entry) -> tuple:
    """Readiness first, as the addendum requires; disruption only breaks ties.

    Readiness is a small integer over ~10 papers, so ties are common -- without a
    tiebreak the order would fall back to arXiv ID, which is arbitrary.
    """
    return (-entry.readiness, -entry.disruption, -entry.relevance, entry.arxiv_id)


SUBJECT_MAX = 110


def compose_subject(prefix: str, industry: str, title: str, n_more: int = 0) -> str:
    """Assemble one subject line from the parts, trimmed to SUBJECT_MAX.

    Shared by `render_subject` (which has the structured entries) and by
    `scout email` (which has only the rendered Markdown), so that re-sending an
    issue produces the same subject the run would have produced.
    """
    tail = f" + {n_more} more" if n_more > 0 else ""
    head = f"{prefix} {industry}: ".strip() if industry else f"{prefix} ".strip()
    budget = SUBJECT_MAX - len(head) - len(tail) - 1
    title = title.strip()
    if len(title) > budget > 20:
        # Trim on a word boundary: a subject cut mid-word reads as a bug.
        title = title[:budget].rsplit(" ", 1)[0].rstrip(",;:-·") + "…"
    return f"{head} {title}{tail}".strip()


def render_subject(data: DigestData, prefix: str = "") -> str:
    """Build the email subject line.

    A subject must name the most interesting *specific* thing in the issue.
    "arXiv Industry Scout — 2026-10-02 (8 papers)" tells a reader nothing and
    trains them not to open it; the date is already in the digest's own header.
    So this names the top-ranked paper and the industry it was read for, and
    lets the filterable tag come from `prefix` (EMAIL_SUBJECT_PREFIX) rather
    than letting it eat the subject.

    Deterministic on purpose -- a model-written subject would be better prose,
    but it would add a call to every run, and the top entry's own title is
    already the specific thing a reader needs to see.
    """
    if not data.entries:
        return f"{prefix} no papers this week".strip()
    entries = sorted(data.entries, key=_entry_order)
    top = entries[0]
    return compose_subject(prefix, top.industry_name, top.title, len(entries) - 1)


def render_digest(data: DigestData) -> str:
    """Render the Markdown report. An empty week is exactly one line."""
    if not data.entries:
        return EMPTY_REPORT + "\n"

    # Order by disruption, as the brief specifies. build_entries already does
    # this, but the guarantee belongs with the thing that produces the report, so
    # a caller assembling entries by hand still gets the documented order.
    entries = sorted(data.entries, key=_entry_order)
    n = len(entries)
    lines = [
        f"# arXiv Industry Scout — week of {data.week_of}",
        "",
        # The funnel line the addendum asks for: scanned, past Stage A,
        # shortlisted, featured. `scanned` is papers actually evaluated, not the
        # fetch count -- most of a fetch is papers already judged in earlier weeks.
        f"{data.n_evaluated} scanned · {data.n_after_tag} passed pipeline tagging "
        f"· {data.n_shortlisted} shortlisted · {n} featured "
        f"· run cost ${data.cost_usd:.2f}",
        "",
        f"*{data.n_fetched} papers fetched, "
        f"{max(0, data.n_fetched - data.n_evaluated)} already seen in earlier weeks.*",
        "",
    ]
    for i, entry in enumerate(entries, start=1):
        byline = entry.first_author
        if byline:
            byline = f"{byline} et al."
        if entry.institution:
            byline = f"{byline}, {entry.institution}" if byline else entry.institution

        lines.append(f"## {i}. {entry.industry_name}")
        paper_line = f"**Paper:** [{entry.title}]({entry.abs_url})"
        if byline:
            paper_line += f" — {byline}"
        if not entry.verified:
            paper_line += "  *(unverified — the checking pass failed for this paper)*"
        lines += [
            paper_line,
            f"**Pipeline:** {entry.pipeline_name}",
            f"**Readiness score:** {entry.readiness}/{entry.max_readiness}"
            + (
                f" — weakest: {entry.most_limiting}"
                + (f" ({entry.limiting_note})" if entry.limiting_note else "")
                if entry.most_limiting
                else ""
            ),
            f"**What they found:** {entry.assessment.what_they_found}",
            f"**Why it matters for {entry.industry_name}:** "
            f"{entry.assessment.why_it_matters}",
            f"**The catch:** {entry.assessment.the_catch}",
            "",
        ]

    if data.notes:
        # One line, not a section: provenance the reader needs to trust the page.
        lines += ["---", "", *(f"*{note}*" for note in data.notes), ""]

    lines += _improvement_sections(data.improvement)
    lines += _feedback_prompt(entries, data.week_of)
    return "\n".join(lines)


def _improvement_sections(improvement: Any) -> list[str]:
    """Automatic changes and escalated proposals.

    Both are required by the addendum, and the first matters most: a system that
    edited its own config must say so on the page, not only in a log file.
    """
    if improvement is None:
        return []
    lines: list[str] = []
    if improvement.applied:
        lines += ["---", "", "## Changes applied automatically", ""]
        for proposal in improvement.applied:
            lines.append(
                f"- `{proposal.type}` in **{proposal.target or 'config'}** — "
                f"{proposal.proposal} *(seen {proposal.occurrences} weeks)*"
            )
        lines.append("")
    if improvement.escalated:
        lines += [
            "---",
            "",
            "## Suggested improvements",
            "",
            "These need your decision and are never applied automatically.",
            "",
        ]
        for proposal in improvement.escalated:
            reason = proposal.note or f"seen in {proposal.occurrences} separate weeks"
            lines.append(
                f"- **{proposal.type}** (`{proposal.id}`, {proposal.occurrences}×) — "
                f"{proposal.proposal}  \n  *Why: {reason}*"
            )
        lines.append("")
    return lines


def _feedback_prompt(entries: list[Entry], week_of: str) -> list[str]:
    """Ask for ratings, in the exact shape state/feedback.yaml expects.

    Given as a paste-ready block rather than prose: a reader who has to invent the
    format will not bother, and the next run needs the file to be parseable.
    """
    if not entries:
        return []
    lines = [
        "---",
        "",
        "## Rate this week",
        "",
        "Rate each paper 1 (not worth reading) to 5 (more like this please). The "
        "comment is optional and is the most useful part. Append to "
        "`state/feedback.yaml` under `ratings:` — the next run reads it and adjusts.",
        "",
        "```yaml",
    ]
    for entry in entries:
        lines.append(f"  - arxiv_id: \"{entry.arxiv_id}\"   # {entry.title[:56]}")
        lines.append(f"    week_of: \"{week_of}\"")
        lines.append("    rating:        # 1-5")
        lines.append('    comment: ""')
    lines += ["```", ""]
    return lines


def render_json(data: DigestData) -> str:
    """Machine-readable sibling, for tracking trends across weeks.

    Kept flat and stable on purpose: this file's whole job is to still parse in
    six months, so new keys may be added but existing ones should not change
    meaning.
    """
    entries = sorted(data.entries, key=_entry_order)
    payload: dict[str, Any] = {
        "schema_version": 3,
        "week_of": data.week_of,
        "n_fetched": data.n_fetched,
        "n_evaluated": data.n_evaluated,
        "n_after_tag": data.n_after_tag,
        "n_shortlisted": data.n_shortlisted,
        "max_readiness": data.max_readiness,
        "n_selected": len(data.entries),
        "cost_usd": round(data.cost_usd, 4),
        "counts": data.counts,
        "picks": [
            {
                "rank": i,
                "arxiv_id": e.arxiv_id,
                "title": e.title,
                "abs_url": e.abs_url,
                "first_author": e.first_author,
                "industry_id": e.industry_id,
                "industry_name": e.industry_name,
                "relevance": e.relevance,
                "disruption": e.disruption,
                "pipeline_id": e.pipeline_id,
                "pipeline_name": e.pipeline_name,
                "readiness": e.readiness,
                "most_limiting": e.most_limiting,
                "verified": e.verified,
                "what_they_found": e.assessment.what_they_found,
                "why_it_matters": e.assessment.why_it_matters,
                "the_catch": e.assessment.the_catch,
            }
            for i, e in enumerate(entries, start=1)
        ],
    }
    if data.improvement is not None:
        payload["improvement"] = {
            "applied": [p.to_yaml() for p in data.improvement.applied],
            "escalated": [p.to_yaml() for p in data.improvement.escalated],
            "tracked": len(data.improvement.proposals),
        }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_digest(cfg: Config, markdown: str, *, today: date | None = None) -> Path:
    cfg.digest_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.digest_dir / f"{(today or date.today()).isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    log.info("wrote digest to %s (%d chars)", path, len(markdown))
    return path


def write_json(cfg: Config, payload: str, *, today: date | None = None) -> Path:
    cfg.digest_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.digest_dir / f"{(today or date.today()).isoformat()}.json"
    path.write_text(payload, encoding="utf-8")
    log.info("wrote digest JSON to %s", path)
    return path
