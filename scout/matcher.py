"""Keyword pre-filter. Runs before any LLM call, so it must be cheap and explainable.

Matching rules, all driven by industries.yaml:

* Case-insensitive, word-boundary, over title + abstract.
* Hyphens, slashes, colons and underscores are all normalised to spaces on both
  sides of the comparison, so "cryo-CMOS" == "cryo CMOS" and "Er:YSO" == "Er YSO".
* A hit on an industry's *own* keywords (or its own keyword_sets) tags that
  industry. Tier 1 and tier 2 make the paper a candidate; tier 3 never does --
  a tier-3 hit only attaches a tag to a paper that is already a candidate.
* A hit on a *group's* keyword_sets tags the group, not its member industries --
  unless some tier-1/2 industry in that group also matched specifically, in which
  case the specific match wins and the group tag is dropped.
* `exclusions` in the YAML suppress a keyword inside a known false-positive
  phrase ("bearing" inside "bearing in mind"). Word boundaries alone cannot do
  this, and the alternative would be hardcoding industry vocabulary in Python.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .industries import Industry, Taxonomy

log = logging.getLogger("scout.matcher")

# Characters that differ between how a term is written in the YAML and how it
# appears in prose. Collapsed to spaces on both sides before comparison.
_SEPARATORS = re.compile(r"[\-‐-―_/:,()\[\]{}]+")
_WHITESPACE = re.compile(r"\s+")

# Group-level hits count as tier 2: every group-level keyword_set in the shipped
# taxonomy is sensing-applied-to-industry vocabulary, which is the tier-2 definition.
GROUP_HIT_TIER = 2


def normalise(text: str, *, lower: bool = True) -> str:
    """Flatten separators (and by default lowercase), so "cryo-CMOS" == "cryo CMOS".

    `lower=False` keeps the original case, for keywords listed in the taxonomy's
    `case_sensitive` set -- chemical formulae like GaP that collide with ordinary
    English words once lowercased.
    """
    if lower:
        text = text.lower()
    return _WHITESPACE.sub(" ", _SEPARATORS.sub(" ", text)).strip()


def _pattern(term: str) -> re.Pattern[str]:
    """Word-boundary pattern for an already-normalised term.

    `(?<!\\w)`/`(?!\\w)` rather than `\\b` so terms that start or end with a digit
    ("28Si", "18650", "Bi2Se3") behave the same as alphabetic ones.
    """
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


@dataclass(frozen=True)
class KeywordIndex:
    """Compiled patterns for every distinct keyword in the taxonomy.

    Keys are the normalised keyword. Case-sensitive terms are keyed by their
    separator-flattened original spelling and matched against an unlowercased
    haystack; everything else is lowercased on both sides.
    """

    patterns: dict[str, re.Pattern[str]]
    exclusions: dict[str, tuple[re.Pattern[str], ...]]
    # normalised keyword -> the original spellings it came from, for reporting
    spellings: dict[str, tuple[str, ...]]
    # subset of `patterns` keys that must be matched against the cased haystack
    cased_keys: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self.patterns)

    def key_for(self, term: str) -> str:
        cased = normalise(term, lower=False)
        return cased if cased in self.cased_keys else normalise(term)


def build_index(taxonomy: Taxonomy) -> KeywordIndex:
    patterns: dict[str, re.Pattern[str]] = {}
    spellings: dict[str, set[str]] = defaultdict(set)
    cased_lookup = {t.lower() for t in taxonomy.case_sensitive}
    cased_keys: set[str] = set()

    def add(term: str) -> None:
        is_cased = term.lower() in cased_lookup
        key = normalise(term, lower=not is_cased)
        if not key:
            return
        if is_cased:
            cased_keys.add(key)
        spellings[key].add(term)
        patterns.setdefault(key, _pattern(key))

    for terms in taxonomy.keyword_sets.values():
        for term in terms:
            add(term)
    for industry in taxonomy.industries.values():
        for term in industry.keywords:
            add(term)

    exclusions: dict[str, tuple[re.Pattern[str], ...]] = {}
    for term, phrases in taxonomy.exclusions.items():
        is_cased = term.lower() in cased_lookup
        key = normalise(term, lower=not is_cased)
        exclusions[key] = tuple(
            _pattern(normalise(phrase, lower=not is_cased))
            for phrase in phrases
            if normalise(phrase, lower=not is_cased)
        )

    log.info(
        "keyword index: %d distinct terms, %d with exclusion phrases, %d case-sensitive",
        len(patterns), len(exclusions), len(cased_keys),
    )
    return KeywordIndex(
        patterns=patterns,
        exclusions=exclusions,
        spellings={k: tuple(sorted(v)) for k, v in spellings.items()},
        cased_keys=frozenset(cased_keys),
    )


@dataclass
class PaperMatch:
    """What the pre-filter concluded about one paper."""

    arxiv_id: str
    # industry_id -> the keywords that matched, for tier-1/2 industry-specific hits
    industries: dict[str, list[str]] = field(default_factory=dict)
    # group_id -> matched keywords, where no tier-1/2 member matched specifically
    groups: dict[str, list[str]] = field(default_factory=dict)
    # tier-3 industry_id -> matched keywords. Only populated if the paper is a candidate.
    tier3_tags: dict[str, list[str]] = field(default_factory=dict)
    # Every keyword that fired, including ones that led nowhere. For noise reporting.
    all_hits: list[str] = field(default_factory=list)

    @property
    def is_candidate(self) -> bool:
        return bool(self.industries) or bool(self.groups)

    @property
    def tier(self) -> int | None:
        """The strongest tier backing this candidacy, or None if not a candidate."""
        if not self.is_candidate:
            return None
        if self._tiers and min(self._tiers) == 1:
            return 1
        return GROUP_HIT_TIER if self.groups else (min(self._tiers) if self._tiers else None)

    _tiers: tuple[int, ...] = ()

    def matched_industry_ids(self) -> list[str]:
        return sorted(self.industries)

    def matched_group_ids(self) -> list[str]:
        return sorted(self.groups)


def passes_secondary_gate(taxonomy: Taxonomy, index: KeywordIndex, text: str) -> bool:
    """True if the text hits at least one term in the secondary_requires keyword set.

    Applied only to papers from `arxiv.secondary` categories, to stop generic
    materials-science papers flooding the pipeline.
    """
    gate_terms = taxonomy.secondary_gate_keywords
    if not gate_terms:
        return True
    hay = _haystacks(text)
    for term in gate_terms:
        key = index.key_for(term)
        pattern = index.patterns.get(key) or _pattern(key)
        haystack = hay[key in index.cased_keys]
        if _has_real_hit(pattern, index.exclusions.get(key, ()), haystack):
            return True
    return False


def _haystacks(text: str) -> tuple[str, str]:
    """(lowercased, original-case) normalised forms. Indexed by `is_cased`."""
    return normalise(text), normalise(text, lower=False)


def _has_real_hit(
    pattern: re.Pattern[str],
    exclusion_patterns: tuple[re.Pattern[str], ...],
    haystack: str,
) -> bool:
    """A hit that isn't wholly contained inside an excluded phrase."""
    spans = [m.span() for m in pattern.finditer(haystack)]
    if not spans:
        return False
    if not exclusion_patterns:
        return True
    excluded: list[tuple[int, int]] = []
    for exclusion in exclusion_patterns:
        excluded.extend(m.span() for m in exclusion.finditer(haystack))
    if not excluded:
        return True
    return any(
        not any(start >= lo and end <= hi for lo, hi in excluded) for start, end in spans
    )


def match_paper(
    taxonomy: Taxonomy, index: KeywordIndex, arxiv_id: str, title: str, abstract: str
) -> PaperMatch:
    hay = _haystacks(f"{title}\n{abstract}")

    hits = {
        key
        for key, pattern in index.patterns.items()
        if _has_real_hit(
            pattern,
            index.exclusions.get(key, ()),
            hay[key in index.cased_keys],
        )
    }
    match = PaperMatch(arxiv_id=arxiv_id, all_hits=sorted(hits))
    if not hits:
        return match

    # Industry-specific hits, split by tier.
    specific: dict[str, list[str]] = {}
    tier3: dict[str, list[str]] = {}
    tiers: list[int] = []
    groups_with_specific_match: set[str] = set()

    for industry in taxonomy.industries.values():
        own = sorted(hits & {index.key_for(k) for k in industry.own_keywords})
        if not own:
            continue
        if industry.tier == 3:
            tier3[industry.id] = own
        else:
            specific[industry.id] = own
            tiers.append(industry.tier)
            groups_with_specific_match.add(industry.group)

    # Group-level hits, suppressed where a tier-1/2 member already matched.
    group_tags: dict[str, list[str]] = {}
    for group in taxonomy.groups.values():
        if not group.group_keywords or group.id in groups_with_specific_match:
            continue
        group_hits = sorted(hits & {index.key_for(k) for k in group.group_keywords})
        if group_hits:
            group_tags[group.id] = group_hits

    match.industries = specific
    match.groups = group_tags
    match._tiers = tuple(tiers)
    # Tier 3 is context only: it never creates a candidate, it only labels one.
    match.tier3_tags = tier3 if match.is_candidate else {}
    return match


@dataclass
class MatchReport:
    """Run-level pre-filter statistics, for the digest summary and noise triage."""

    total: int = 0
    candidates: list[PaperMatch] = field(default_factory=list)
    tier_counts: Counter = field(default_factory=Counter)
    industry_counts: Counter = field(default_factory=Counter)
    group_counts: Counter = field(default_factory=Counter)
    tier3_counts: Counter = field(default_factory=Counter)
    # keyword -> how many papers it fired on, across every paper examined
    keyword_counts: Counter = field(default_factory=Counter)
    # keyword -> which industries/groups it can tag, for judging whether noise matters
    keyword_owners: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def most_matched(self, taxonomy: Taxonomy, limit: int = 10) -> list[tuple[str, int]]:
        """Most-matched industries and groups, by display name."""
        combined: Counter = Counter()
        for industry_id, count in self.industry_counts.items():
            combined[taxonomy.industries[industry_id].name] = count
        for group_id, count in self.group_counts.items():
            combined[f"{taxonomy.groups[group_id].name} (group)"] += count
        return combined.most_common(limit)

    def noisy_keywords(
        self, limit: int = 20, min_share: float = 0.02
    ) -> list[tuple[str, int, float]]:
        """Keywords firing on an implausibly large share of papers.

        Terms that cannot tag anything are excluded: the secondary-category gate
        vocabulary is *supposed* to fire on most of the corpus, so listing
        "quantum" as noisy would bury the terms actually worth trimming.
        """
        if not self.total:
            return []
        rows = [
            (keyword, count, count / self.total)
            for keyword, count in self.keyword_counts.items()
            if count / self.total >= min_share and self.keyword_owners.get(keyword)
        ]
        rows.sort(key=lambda row: -row[1])
        return rows[:limit]


def match_papers(taxonomy: Taxonomy, index: KeywordIndex, papers: list) -> MatchReport:
    """Run the pre-filter over papers (anything with arxiv_id/title/abstract)."""
    report = MatchReport(total=len(papers))

    owners: dict[str, set[str]] = defaultdict(set)
    for industry in taxonomy.industries.values():
        for term in industry.own_keywords:
            owners[index.key_for(term)].add(f"{industry.id} (t{industry.tier})")
    for group in taxonomy.groups.values():
        for term in group.group_keywords:
            owners[index.key_for(term)].add(f"{group.id} (group)")

    for paper in papers:
        match = match_paper(
            taxonomy, index, paper.arxiv_id, paper.title, paper.abstract
        )
        report.keyword_counts.update(match.all_hits)
        if not match.is_candidate:
            continue
        report.candidates.append(match)
        report.tier_counts[match.tier] += 1
        for industry_id in match.industries:
            report.industry_counts[industry_id] += 1
        for group_id in match.groups:
            report.group_counts[group_id] += 1
        for industry_id in match.tier3_tags:
            report.tier3_counts[industry_id] += 1

    for keyword in report.keyword_counts:
        report.keyword_owners[keyword] = owners.get(keyword, set())

    log.info(
        "pre-filter: %d/%d papers are candidates (tier 1: %d, tier 2: %d)",
        len(report.candidates), report.total,
        report.tier_counts.get(1, 0), report.tier_counts.get(2, 0),
    )
    return report


def evidence_strength(arxiv_id: str, matches: dict) -> tuple:
    """Rank a paper by the strength of its keyword evidence. Higher sorts first.

    A tier-1 industry-specific hit beats tier 2, which beats a group-level hit
    only; more distinct hits break ties, and `arxiv_id` makes the order total so
    two runs over the same input rank identically.

    Used in two places that must agree: the budget guard sheds the weakest
    evidence first, and the shortlist is ordered by this before `[final]
    max_papers` truncates it. Without it, every shortlisted paper scores exactly
    `relevance == 3` and truncation falls back to arXiv-ID order -- which is to
    say, at random.
    """
    match = matches.get(arxiv_id)
    if match is None:
        # No keyword evidence at all: the first thing to shed. This must sort
        # BELOW every matched paper, so it cannot be expressed as a negated tier
        # -- `-tier` would make tier 1 (-1) sort under an unmatched 0.
        return (0, 0, 0, arxiv_id)
    tier = match.tier or 9
    return (
        max(0, 10 - tier),          # tier 1 -> 9, tier 2 -> 8, unknown -> 1
        len(match.industries),      # specific industry hits
        len(match.all_hits),        # total keyword evidence
        arxiv_id,
    )
