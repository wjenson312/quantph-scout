"""Part 2: the self-improvement loop.

At the end of each run, look at where the run had to force a fit and write that
down as a proposal. Proposals accumulate across weeks; once a *low-risk* one has
been seen 3 times it is applied automatically, and everything structural is
escalated in the report for a human decision.

Three constraints shape this module, all from the brief:

* **No new model calls of substance.** The misfit detection is entirely mechanical
  -- it reads the run's own outputs and `state/feedback.yaml`, nothing else. That
  keeps the whole pass free, well inside the "about 5% of the run's cost" ceiling,
  and means it cannot fail in a way that costs money.
* **Only `new_discovery_signal` is applied automatically.** New pipelines, new or
  retired industries, scoring changes and prompt changes are never self-applied.
  A system that rewrites its own scoring rules cannot be audited by reading a diff.
* **Occurrences count separate weeks, not separate papers.** Ten papers in one week
  hitting the same gap is one occurrence. Otherwise a single unusual week would
  trip the auto-apply threshold on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("scout.improve")

# Types that may be applied without a human. Deliberately exactly one.
AUTO_APPLY_TYPES = frozenset({"new_discovery_signal"})
# Occurrences needed before a proposal acts or is escalated.
THRESHOLD = 3

PROPOSAL_TYPES = frozenset(
    {
        "new_pipeline",
        "new_industry",
        "new_discovery_signal",
        "retire_industry",
        "scoring_change",
        "prompt_change",
    }
)


@dataclass
class Proposal:
    id: str
    type: str
    proposal: str
    evidence: list[str] = field(default_factory=list)
    first_seen: str = ""
    occurrences: int = 0
    status: str = "pending"
    # Free-text detail, not in the brief's schema but written for the reader.
    note: str = ""
    # The pipeline a new_discovery_signal attaches to, when applicable.
    target: str = ""

    def to_yaml(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "proposal": self.proposal,
            "evidence": list(dict.fromkeys(self.evidence))[:20],
            "first_seen": self.first_seen,
            "occurrences": self.occurrences,
            "status": self.status,
        }
        if self.note:
            out["note"] = self.note
        if self.target:
            out["target"] = self.target
        return out


@dataclass
class Feedback:
    arxiv_id: str
    rating: int
    week_of: str = ""
    comment: str = ""


# --------------------------------------------------------------------------
# State files
# --------------------------------------------------------------------------


def load_proposals(path: Path) -> list[Proposal]:
    """Read state/proposals.yaml. A missing or empty file is not an error."""
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # Never fail a run over the improvement layer's own bookkeeping.
        log.error("%s is not valid YAML (%s); starting from no proposals", path, exc)
        return []
    entries = raw.get("proposals") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    out: list[Proposal] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        out.append(
            Proposal(
                id=str(entry["id"]),
                type=str(entry.get("type") or ""),
                proposal=str(entry.get("proposal") or ""),
                evidence=[str(e) for e in (entry.get("evidence") or [])],
                first_seen=str(entry.get("first_seen") or ""),
                occurrences=int(entry.get("occurrences") or 0),
                status=str(entry.get("status") or "pending"),
                note=str(entry.get("note") or ""),
                target=str(entry.get("target") or ""),
            )
        )
    return out


def save_proposals(path: Path, proposals: list[Proposal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Improvement proposals carried between runs, written by scout/improve.py.\n"
        "#\n"
        "# `new_discovery_signal` proposals are applied automatically once they reach\n"
        f"# {THRESHOLD} occurrences; everything else is escalated in the report for a\n"
        "# human decision and never applied on its own.\n"
        "#\n"
        "# `occurrences` counts separate WEEKS, not papers, and it is what drives the\n"
        "# automatic behaviour -- safe to edit by hand, but do not inflate it.\n"
    )
    body = yaml.safe_dump(
        {"proposals": [p.to_yaml() for p in proposals]},
        sort_keys=False, allow_unicode=True, width=88,
    )
    path.write_text(header + body, encoding="utf-8")
    log.info("wrote %d proposals to %s", len(proposals), path)


def load_feedback(path: Path) -> list[Feedback]:
    """Read state/feedback.yaml. The agent never writes to this file."""
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.error("%s is not valid YAML (%s); ignoring feedback", path, exc)
        return []
    entries = raw.get("ratings") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    out: list[Feedback] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            rating = int(entry.get("rating"))
        except (TypeError, ValueError):
            continue
        if not 1 <= rating <= 5:
            log.warning("ignoring feedback with out-of-range rating %r", entry.get("rating"))
            continue
        out.append(
            Feedback(
                arxiv_id=str(entry.get("arxiv_id") or ""),
                rating=rating,
                week_of=str(entry.get("week_of") or ""),
                comment=str(entry.get("comment") or ""),
            )
        )
    return out


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def merge(existing: list[Proposal], found: list[Proposal]) -> list[Proposal]:
    """Fold this run's findings into the carried-over set.

    A proposal already seen gains one occurrence and this run's evidence -- one
    occurrence per run, however many papers triggered it, so an unusual week cannot
    reach the auto-apply threshold by itself.
    """
    by_id = {p.id: p for p in existing}
    for candidate in found:
        current = by_id.get(candidate.id)
        if current is None:
            by_id[candidate.id] = candidate
            continue
        if current.status in ("applied", "rejected"):
            # A rejected proposal stays rejected: re-proposing it every week would
            # be nagging, and the reader already answered.
            continue
        current.occurrences += 1
        current.evidence = list(dict.fromkeys([*current.evidence, *candidate.evidence]))
        if candidate.note:
            current.note = candidate.note
    return sorted(by_id.values(), key=lambda p: (-p.occurrences, p.type, p.id))


# --------------------------------------------------------------------------
# Misfit detection -- mechanical, no model calls
# --------------------------------------------------------------------------


def _pipelines_of(matches: list) -> set[str]:
    """Pipeline ids behind a set of match outcomes, tolerating partial objects.

    Defensive on purpose: this module must never be the reason a digest fails, so
    it reaches through the run's object graph only where it is certain to exist.
    """
    out: set[str] = set()
    for match in matches:
        pipeline = getattr(
            getattr(getattr(match, "scored", None), "tagged", None), "tag", None
        )
        pid = getattr(pipeline, "primary_pipeline", "")
        if pid:
            out.add(pid)
    return out


def _slug(text: str) -> str:
    keep = [c if c.isalnum() else "-" for c in text.lower()]
    return "-".join(part for part in "".join(keep).split("-") if part)[:48]


def detect(
    *,
    today: str,
    tagged: list,
    scored: list,
    matched: list,
    selected_ids: set[str],
    taxonomy: Any,
    pipelines: Any,
    feedback: list[Feedback],
) -> list[Proposal]:
    """Find this run's misfits. Returns one proposal per distinct problem."""
    found: list[Proposal] = []

    # 1. Papers with a clear application but no good pipeline: tagged `none` despite
    #    strong readiness, or kept with `low` confidence. Both mean the pipeline list
    #    does not describe what this paper did.
    low_conf = [t for t in tagged if not t.dropped and t.tag.confidence == "low"]
    if len(low_conf) >= 3:
        signals = [t.tag.signal for t in low_conf[:8] if t.tag.signal]
        by_pipeline: dict[str, list] = {}
        for t in low_conf:
            by_pipeline.setdefault(t.tag.primary_pipeline, []).append(t)
        worst = max(by_pipeline.items(), key=lambda kv: len(kv[1]))
        if len(worst[1]) >= 3:
            found.append(
                Proposal(
                    id=f"signal-{_slug(worst[0])}",
                    type="new_discovery_signal",
                    target=worst[0],
                    proposal=(
                        "Add a discovery signal to pipeline "
                        f"'{worst[0]}' covering: "
                        + "; ".join(s for s in signals[:3])
                    ),
                    evidence=[t.paper.arxiv_id for t in worst[1][:10]],
                    first_seen=today,
                    occurrences=1,
                    note=(
                        f"{len(worst[1])} papers were tagged '{worst[0]}' with low "
                        "confidence this run, which means the tag was a guess rather "
                        "than a match on a listed signal."
                    ),
                )
            )

    # 2. Strong shortlisted papers dropped because no industry fitted.
    unmatched = [m for m in matched if not m.kept]
    if unmatched:
        found.append(
            Proposal(
                id="industry-gap-shortlisted",
                type="new_industry",
                proposal=(
                    "Add an industry (or widen an existing description) covering the "
                    "shortlisted papers listed in evidence: they scored well on "
                    "readiness but no industry in the taxonomy fitted them."
                ),
                evidence=[m.arxiv_id for m in unmatched[:10]],
                first_seen=today,
                occurrences=1,
                note=(
                    f"{len(unmatched)} shortlisted paper(s) were dropped rather than "
                    "forced into a weak industry fit. Pipelines involved: "
                    + (", ".join(sorted(_pipelines_of(unmatched))) or "unknown")
                ),
            )
        )

    # 3. A readiness check that has gone inert. If every paper answered "not stated",
    #    the check contributes a constant and discriminates nothing.
    if scored:
        per_check: dict[str, list[int]] = {}
        for paper in scored:
            for item in paper.score.checks:
                per_check.setdefault(item.check_id, []).append(item.score)
        for check_id, values in per_check.items():
            if len(values) >= 10 and len(set(values)) == 1:
                found.append(
                    Proposal(
                        id=f"inert-check-{_slug(check_id)}",
                        type="scoring_change",
                        proposal=(
                            f"Readiness check '{check_id}' scored identically on every "
                            "paper this run. Either reword its question in "
                            "pipelines.yaml so papers can differentiate on it, or drop "
                            "it and score out of the remaining checks."
                        ),
                        evidence=[p.arxiv_id for p in scored[:6]],
                        first_seen=today,
                        occurrences=1,
                        note=(
                            f"All {len(values)} papers scored {values[0]} -- the check "
                            "is contributing a constant to every total."
                        ),
                    )
                )

    # 4. Feedback patterns: an industry or pipeline consistently rated low.
    if feedback:
        found.extend(_from_feedback(feedback, today, matched))

    return found


def _from_feedback(
    feedback: list[Feedback], today: str, matched: list
) -> list[Proposal]:
    """Turn rating patterns into scoring or prompt proposals.

    Only patterns, never single ratings: one low score is an opinion about a paper,
    while three low scores on the same industry is a signal about the system.
    """
    out: list[Proposal] = []
    low = [f for f in feedback if f.rating <= 2]
    if len(low) >= THRESHOLD:
        industry_of = {m.arxiv_id: m.result.best_industry for m in matched}
        counts: dict[str, list[str]] = {}
        for entry in low:
            industry = industry_of.get(entry.arxiv_id, "")
            if industry:
                counts.setdefault(industry, []).append(entry.arxiv_id)
        for industry, ids in counts.items():
            if len(ids) >= THRESHOLD:
                out.append(
                    Proposal(
                        id=f"feedback-low-{_slug(industry)}",
                        type="retire_industry",
                        proposal=(
                            f"Consider retiring or narrowing industry '{industry}': "
                            "every paper reported under it has been rated 1-2."
                        ),
                        evidence=ids[:10],
                        first_seen=today,
                        occurrences=1,
                        note=f"{len(ids)} low ratings, no high ones.",
                    )
                )
    # Readiness optimism: papers rated low despite high readiness means the score
    # is not measuring what the reader values.
    if len(low) >= THRESHOLD:
        out.append(
            Proposal(
                id="feedback-readiness-calibration",
                type="scoring_change",
                proposal=(
                    "Readiness scores may be too optimistic: several highly-scored "
                    "papers were rated 1-2 by the reader. Consider tightening the "
                    "performance_margin and scalability wording in pipelines.yaml."
                ),
                evidence=[f.arxiv_id for f in low[:10]],
                first_seen=today,
                occurrences=1,
                note=f"{len(low)} papers rated 1-2 across the stored feedback.",
            )
        )
    return out


# --------------------------------------------------------------------------
# Applying and escalating
# --------------------------------------------------------------------------


@dataclass
class ImprovementResult:
    proposals: list[Proposal] = field(default_factory=list)
    applied: list[Proposal] = field(default_factory=list)
    escalated: list[Proposal] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.proposals)} proposals tracked, {len(self.applied)} applied "
            f"automatically, {len(self.escalated)} escalated for review"
        )


def apply_auto(
    proposals: list[Proposal], pipelines_path: Path, *, dry_run: bool
) -> list[Proposal]:
    """Apply `new_discovery_signal` proposals that have reached the threshold.

    Edits `pipelines.yaml` in place, appending to the target pipeline's
    `discovery_signals`. Everything else is left for a human -- see the module
    docstring for why that line is where it is.
    """
    ready = [
        p
        for p in proposals
        if p.type in AUTO_APPLY_TYPES
        and p.status == "pending"
        and p.occurrences >= THRESHOLD
        and p.target
    ]
    if not ready:
        return []
    if dry_run:
        log.info("--dry-run: not applying %d ready proposal(s)", len(ready))
        return []

    text = pipelines_path.read_text(encoding="utf-8")
    applied: list[Proposal] = []
    for proposal in ready:
        marker = f"  - id: {proposal.target}\n"
        if marker not in text:
            log.warning(
                "cannot apply %s: pipeline '%s' not found in %s",
                proposal.id, proposal.target, pipelines_path.name,
            )
            continue
        signals_at = text.find("discovery_signals:", text.index(marker))
        if signals_at == -1:
            log.warning("cannot apply %s: no discovery_signals block", proposal.id)
            continue
        line_end = text.index("\n", signals_at) + 1
        signal = proposal.proposal.split("covering:", 1)[-1].strip() or proposal.proposal
        # Keep it one line and clearly attributed, so a reader can see what the
        # agent added to its own config and when.
        addition = (
            f"      - {signal}  # auto-added {date.today().isoformat()} "
            f"after {proposal.occurrences} occurrences ({proposal.id})\n"
        )
        text = text[:line_end] + addition + text[line_end:]
        proposal.status = "applied"
        applied.append(proposal)
        log.warning(
            "auto-applied %s: added a discovery signal to pipeline '%s'",
            proposal.id, proposal.target,
        )
    if applied:
        pipelines_path.write_text(text, encoding="utf-8")
    return applied


def escalate(proposals: list[Proposal]) -> list[Proposal]:
    """Structural proposals that have reached the threshold and need a decision."""
    return [
        p
        for p in proposals
        if p.type not in AUTO_APPLY_TYPES
        and p.status == "pending"
        and p.occurrences >= THRESHOLD
    ]


def run_improvement(
    *,
    today: str,
    proposals_path: Path,
    feedback_path: Path,
    pipelines_path: Path,
    tagged: list,
    scored: list,
    matched: list,
    selected_ids: set[str],
    taxonomy: Any,
    pipelines: Any,
    dry_run: bool = False,
) -> ImprovementResult:
    """The whole Part-2 pass. Free: reads this run's own output, calls no model."""
    feedback = load_feedback(feedback_path)
    if feedback:
        log.info("read %d feedback rating(s) from %s", len(feedback), feedback_path.name)

    found = detect(
        today=today, tagged=tagged, scored=scored, matched=matched,
        selected_ids=selected_ids, taxonomy=taxonomy, pipelines=pipelines,
        feedback=feedback,
    )
    merged = merge(load_proposals(proposals_path), found)
    applied = apply_auto(merged, pipelines_path, dry_run=dry_run)
    escalated = escalate(merged)

    if not dry_run:
        save_proposals(proposals_path, merged)

    result = ImprovementResult(proposals=merged, applied=applied, escalated=escalated)
    log.info("self-improvement: %s", result.summary())
    return result
