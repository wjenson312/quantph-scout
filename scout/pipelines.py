"""Loads and validates `pipelines.yaml`: the discovery-to-application pipelines.

A pipeline is the *mechanism* by which a physics result could become a product —
"environmental sensitivity becomes sensing", "anomalous transport becomes energy
and power". Papers are tagged with one before any industry is chosen, which
narrows the industry candidates from 78 to a handful.

Same contract as `industries.py`, and for the same reason: this file is loaded at
the top of a run, before any network call or spend, and it reports *every* problem
at once rather than failing on the first. A weekly cron job should not discover a
typo after 90 seconds of arXiv paging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("scout.pipelines")

# The id reserved for "no application pipeline". Papers tagged with it are dropped.
NONE_ID = "none"


class PipelineError(RuntimeError):
    """`pipelines.yaml` is missing or malformed. Lists every problem found."""


@dataclass(frozen=True)
class Pipeline:
    id: str
    name: str
    discovery_signals: tuple[str, ...] = ()
    becomes: tuple[str, ...] = ()
    typical_domains: tuple[str, ...] = ()
    # "drop" on the `none` pipeline; empty otherwise.
    action: str = ""

    @property
    def is_drop(self) -> bool:
        return self.id == NONE_ID or self.action == "drop"


@dataclass(frozen=True)
class ReadinessCheck:
    id: str
    question: str


@dataclass(frozen=True)
class PipelineSet:
    version: int
    pipelines: dict[str, Pipeline]
    readiness_checks: tuple[ReadinessCheck, ...]
    path: Path

    @property
    def max_score(self) -> int:
        """Each check scores 0-2, so the ceiling follows from the file.

        Derived rather than hardcoded to 10: the shipped file has five checks, but
        adding a sixth should change the denominator, not silently cap the scale.
        """
        return 2 * len(self.readiness_checks)

    @property
    def scorable(self) -> tuple[Pipeline, ...]:
        """Pipelines a paper can be tagged with and survive. Excludes `none`."""
        return tuple(p for p in self.pipelines.values() if not p.is_drop)

    def check_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.readiness_checks)


def _seq(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v).strip() for v in value if str(v).strip())


def load_pipelines(path: Path) -> PipelineSet:
    """Read and validate pipelines.yaml, reporting every problem at once."""
    if not path.exists():
        raise PipelineError(f"pipelines.yaml not found at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PipelineError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PipelineError(f"{path}: top level must be a mapping")

    problems: list[str] = []
    pipelines: dict[str, Pipeline] = {}

    entries = raw.get("pipelines")
    if not isinstance(entries, list) or not entries:
        problems.append("`pipelines:` must be a non-empty list")
        entries = []

    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"pipelines[{n}] is not a mapping")
            continue
        pid = str(entry.get("id") or "").strip()
        if not pid:
            problems.append(f"pipelines[{n}] has no id")
            continue
        if pid in pipelines:
            problems.append(f"duplicate pipeline id: {pid}")
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            problems.append(f"{pid}: has no name")
        signals = _seq(entry.get("discovery_signals"))
        action = str(entry.get("action") or "").strip()
        if pid != NONE_ID and not signals:
            # Without signals the model has nothing to match on, so the pipeline
            # can never legitimately be chosen -- it would only ever be guessed.
            problems.append(f"{pid}: has no discovery_signals, so it can never be matched")
        pipelines[pid] = Pipeline(
            id=pid,
            name=name or pid,
            discovery_signals=signals,
            becomes=_seq(entry.get("becomes")),
            typical_domains=_seq(entry.get("typical_domains")),
            action=action,
        )

    if NONE_ID not in pipelines:
        problems.append(
            f"no `{NONE_ID}` pipeline: the tagger needs somewhere to put "
            "foundational work, or it will force a bad fit instead"
        )

    checks: list[ReadinessCheck] = []
    raw_checks = raw.get("readiness_checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        problems.append("`readiness_checks:` must be a non-empty list")
        raw_checks = []
    seen: set[str] = set()
    for n, entry in enumerate(raw_checks, start=1):
        if not isinstance(entry, dict):
            problems.append(f"readiness_checks[{n}] is not a mapping")
            continue
        cid = str(entry.get("id") or "").strip()
        question = str(entry.get("question") or "").strip()
        if not cid:
            problems.append(f"readiness_checks[{n}] has no id")
            continue
        if cid in seen:
            problems.append(f"duplicate readiness check id: {cid}")
            continue
        if not question:
            problems.append(f"readiness check {cid}: has no question")
        seen.add(cid)
        checks.append(ReadinessCheck(id=cid, question=question))

    if problems:
        raise PipelineError(
            f"{path} is invalid ({len(problems)} problem"
            f"{'s' if len(problems) != 1 else ''}):\n  - "
            + "\n  - ".join(problems)
        )

    result = PipelineSet(
        version=int(raw.get("version") or 1),
        pipelines=pipelines,
        readiness_checks=tuple(checks),
        path=path,
    )
    log.info(
        "loaded %s: %d pipelines (%d scorable), %d readiness checks, max score %d",
        path.name, len(pipelines), len(result.scorable),
        len(checks), result.max_score,
    )
    return result


def render_pipeline_list(pipelines: PipelineSet) -> str:
    """The pipeline menu shown to the tagging model.

    Signals are what the model actually matches on, so they are included in full;
    `becomes` and `typical_domains` are one line of context each. `none` is listed
    last and described plainly, because a tagger with no escape hatch invents fits.
    """
    blocks: list[str] = []
    for pipeline in pipelines.pipelines.values():
        if pipeline.is_drop:
            continue
        lines = [f"### {pipeline.id} — {pipeline.name}", "Signals that indicate it:"]
        lines += [f"  - {s}" for s in pipeline.discovery_signals]
        if pipeline.becomes:
            lines.append(f"Becomes: {', '.join(pipeline.becomes)}")
        if pipeline.typical_domains:
            lines.append(f"Typical domains: {', '.join(pipeline.typical_domains)}")
        blocks.append("\n".join(lines))
    none = pipelines.pipelines.get(NONE_ID)
    if none is not None:
        blocks.append(
            f"### {none.id} — {none.name}\n"
            "Use this for purely foundational or theoretical work with no physical "
            "handle for application. Papers tagged this way are dropped."
        )
    return "\n\n".join(blocks)


def render_readiness_checks(pipelines: PipelineSet) -> str:
    return "\n".join(
        f"{n}. **{c.id}** — {c.question}"
        for n, c in enumerate(pipelines.readiness_checks, start=1)
    )
