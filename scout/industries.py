"""Load and validate industries.yaml.

The YAML file is the single source of truth: no industry name, group name, or
keyword appears anywhere in this codebase. Everything below is structure and
validation.

An industry's *effective* keywords are the union of three sources:
  1. its own `keywords`
  2. the `keyword_sets` it names itself
  3. the `keyword_sets` its group names

Sources 1 and 2 are "industry-specific" -- a hit on them tags that industry.
Source 3 is "group-level" -- a hit there tags the *group* unless some industry in
that group also got an industry-specific hit. That distinction is what keeps a
vague "magnetic anomaly" from tagging all nine critical-mineral industries at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("scout.industries")

VALID_TIERS = (1, 2, 3)

# Fallbacks if the file predates the `tiers:` block. The loader prefers the file.
_DEFAULT_TIER_DEFINITIONS = {
    1: "Direct quantum-hardware linkage.",
    2: "Quantum sensing or metrology applied to this industry.",
    3: "Context only; never a match on its own.",
}


class TaxonomyError(ValueError):
    """industries.yaml is malformed. Raised with every problem found, not just the first."""


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    keyword_sets: tuple[str, ...] = ()
    # Union of the keyword_sets above. A hit here is a group-level hit.
    group_keywords: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Industry:
    id: str
    name: str
    group: str
    tier: int
    domains: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    keyword_sets: tuple[str, ...] = ()
    link: str = ""
    # Own keywords + own keyword_sets. A hit here is an industry-specific hit.
    own_keywords: frozenset[str] = frozenset()
    # own_keywords + the group's keyword_sets.
    effective_keywords: frozenset[str] = frozenset()

    @property
    def description(self) -> str:
        """What the scoring model is shown as this industry's linkage.

        Tier-1 entries carry an explicit `link`; tier-2 and tier-3 entries in the
        shipped file do not, so fall back to the name rather than sending an
        empty string.
        """
        return self.link or self.name


@dataclass(frozen=True)
class Taxonomy:
    version: int
    primary: tuple[str, ...]
    secondary: tuple[str, ...]
    secondary_requires: str
    domains: tuple[str, ...]
    keyword_sets: dict[str, tuple[str, ...]]
    groups: dict[str, Group]
    industries: dict[str, Industry]
    tier_definitions: dict[int, str] = field(default_factory=dict)
    # keyword -> phrases in which that keyword should NOT count as a hit.
    # Word boundaries cannot tell "bearing" in "roller bearing" from "bearing in
    # mind"; this can, without putting industry vocabulary into Python.
    exclusions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Keywords matched with case sensitivity, overriding the default. For chemical
    # formulae that collide with English words when lowercased: GaP/"gap",
    # TiN/"tin", TES/"tes". Empty by default -- matching is case-insensitive
    # unless a term is named here.
    case_sensitive: frozenset[str] = frozenset()
    path: Path | None = None

    def by_tier(self, tier: int) -> list[Industry]:
        return [i for i in self.industries.values() if i.tier == tier]

    @property
    def secondary_gate_keywords(self) -> tuple[str, ...]:
        """Terms a secondary-category paper must hit to enter the pipeline."""
        return self.keyword_sets.get(self.secondary_requires, ())

    def group_of(self, industry_id: str) -> Group:
        return self.groups[self.industries[industry_id].group]

    def tier_definition(self, tier: int) -> str:
        return self.tier_definitions.get(tier, _DEFAULT_TIER_DEFINITIONS.get(tier, ""))


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    raise TaxonomyError(f"expected a string or list, got {type(value).__name__}: {value!r}")


def load_taxonomy(path: Path) -> Taxonomy:
    """Parse and validate industries.yaml. Raises TaxonomyError listing every problem."""
    if not path.exists():
        raise TaxonomyError(f"industries.yaml not found at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaxonomyError(f"{path}: top level must be a mapping")

    problems: list[str] = []

    # -- keyword_sets --------------------------------------------------------
    raw_sets = raw.get("keyword_sets") or {}
    if not isinstance(raw_sets, dict):
        raise TaxonomyError(f"{path}: keyword_sets must be a mapping")
    keyword_sets = {name: _as_tuple(terms) for name, terms in raw_sets.items()}
    for name, terms in keyword_sets.items():
        if not terms:
            problems.append(f"keyword_set {name!r} is empty")

    def check_set_refs(refs: tuple[str, ...], where: str) -> None:
        for ref in refs:
            if ref not in keyword_sets:
                problems.append(
                    f"{where} references keyword_set {ref!r}, which is not defined "
                    f"(defined: {', '.join(sorted(keyword_sets)) or 'none'})"
                )

    # -- arxiv --------------------------------------------------------------
    arxiv_raw = raw.get("arxiv") or {}
    if not isinstance(arxiv_raw, dict):
        raise TaxonomyError(f"{path}: arxiv must be a mapping")
    primary = _as_tuple(arxiv_raw.get("primary"))
    secondary = _as_tuple(arxiv_raw.get("secondary"))
    secondary_requires = str(arxiv_raw.get("secondary_requires") or "")
    if not primary:
        problems.append("arxiv.primary must list at least one category")
    if secondary and not secondary_requires:
        problems.append(
            "arxiv.secondary is set but arxiv.secondary_requires is missing -- "
            "without a gate, secondary categories would flood the pipeline"
        )
    if secondary_requires:
        check_set_refs((secondary_requires,), "arxiv.secondary_requires")

    # -- groups -------------------------------------------------------------
    raw_groups = raw.get("groups") or {}
    if not isinstance(raw_groups, dict):
        raise TaxonomyError(f"{path}: groups must be a mapping")
    groups: dict[str, Group] = {}
    for group_id, body in raw_groups.items():
        body = body or {}
        if not isinstance(body, dict):
            problems.append(f"group {group_id!r} must be a mapping")
            continue
        refs = _as_tuple(body.get("keyword_sets"))
        check_set_refs(refs, f"group {group_id!r}")
        groups[group_id] = Group(
            id=group_id,
            name=str(body.get("name") or group_id),
            keyword_sets=refs,
            group_keywords=frozenset(
                term for ref in refs for term in keyword_sets.get(ref, ())
            ),
        )

    # -- industries ---------------------------------------------------------
    raw_industries = raw.get("industries") or []
    if not isinstance(raw_industries, list):
        raise TaxonomyError(f"{path}: industries must be a list")

    industries: dict[str, Industry] = {}
    seen_ids: set[str] = set()
    for position, entry in enumerate(raw_industries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"industries[{position}] must be a mapping, got {entry!r}")
            continue
        industry_id = str(entry.get("id") or "").strip()
        if not industry_id:
            problems.append(f"industries[{position}] is missing an id")
            continue
        if industry_id in seen_ids:
            problems.append(f"duplicate industry id {industry_id!r}")
            continue
        seen_ids.add(industry_id)

        tier_raw = entry.get("tier")
        if tier_raw not in VALID_TIERS:
            problems.append(
                f"industry {industry_id!r} has tier {tier_raw!r}; must be one of "
                f"{', '.join(str(t) for t in VALID_TIERS)}"
            )
            continue
        tier = int(tier_raw)

        group_id = str(entry.get("group") or "").strip()
        if group_id not in groups:
            problems.append(
                f"industry {industry_id!r} is in group {group_id!r}, which is not "
                f"defined in groups (defined: {', '.join(sorted(groups)) or 'none'})"
            )
            continue

        own_sets = _as_tuple(entry.get("keyword_sets"))
        check_set_refs(own_sets, f"industry {industry_id!r}")
        own_terms = _as_tuple(entry.get("keywords"))

        own_keywords = frozenset(own_terms) | frozenset(
            term for ref in own_sets for term in keyword_sets.get(ref, ())
        )
        if not own_keywords and not groups[group_id].group_keywords:
            problems.append(
                f"industry {industry_id!r} has no keywords of its own and its group "
                f"contributes none, so it can never match"
            )

        industries[industry_id] = Industry(
            id=industry_id,
            name=str(entry.get("name") or industry_id),
            group=group_id,
            tier=tier,
            domains=_as_tuple(entry.get("domains")),
            keywords=own_terms,
            keyword_sets=own_sets,
            link=str(entry.get("link") or ""),
            own_keywords=own_keywords,
            effective_keywords=own_keywords | groups[group_id].group_keywords,
        )

    if not industries and not problems:
        problems.append("industries is empty")

    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise TaxonomyError(
            f"{path} is invalid ({len(problems)} problem"
            f"{'' if len(problems) == 1 else 's'}):\n{bullets}"
        )

    tier_definitions = {
        int(k): str(v) for k, v in (raw.get("tiers") or {}).items() if str(k).isdigit()
    }

    # `exclusions` is validated against the keywords that actually exist, so a
    # typo here is an error rather than a silently dead entry.
    all_terms = {
        term.lower()
        for industry in industries.values()
        for term in industry.keywords
    } | {
        term.lower() for terms in keyword_sets.values() for term in terms
    }
    raw_cased = _as_tuple(raw.get("case_sensitive"))
    for term in raw_cased:
        if term.lower() not in all_terms:
            problems.append(
                f"case_sensitive names keyword {term!r}, which is not a keyword in "
                f"any industry or keyword_set"
            )

    exclusions: dict[str, tuple[str, ...]] = {}
    raw_exclusions = raw.get("exclusions") or {}
    if not isinstance(raw_exclusions, dict):
        raise TaxonomyError(f"{path}: exclusions must be a mapping")
    for term, phrases in raw_exclusions.items():
        if str(term).lower() not in all_terms:
            problems.append(
                f"exclusions names keyword {term!r}, which is not a keyword in any "
                f"industry or keyword_set"
            )
            continue
        exclusions[str(term)] = _as_tuple(phrases)

    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise TaxonomyError(
            f"{path} is invalid ({len(problems)} problem"
            f"{'' if len(problems) == 1 else 's'}):\n{bullets}"
        )

    taxonomy = Taxonomy(
        version=int(raw.get("version") or 1),
        primary=primary,
        secondary=secondary,
        secondary_requires=secondary_requires,
        domains=_as_tuple(raw.get("domains")),
        keyword_sets=keyword_sets,
        groups=groups,
        industries=industries,
        tier_definitions=tier_definitions,
        exclusions=exclusions,
        case_sensitive=frozenset(raw_cased),
        path=path,
    )
    log.info(
        "loaded %s: %d industries (tier 1/2/3 = %d/%d/%d), %d groups, %d keyword sets",
        path.name,
        len(industries),
        len(taxonomy.by_tier(1)),
        len(taxonomy.by_tier(2)),
        len(taxonomy.by_tier(3)),
        len(groups),
        len(keyword_sets),
    )
    return taxonomy
