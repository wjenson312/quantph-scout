"""Configuration loading: config.toml for behaviour, .env for secrets."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Project root: the directory containing config.toml, i.e. the parent of scout/.
ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when config.toml or .env is missing something required."""


@dataclass(frozen=True)
class ArxivConfig:
    categories: list[str] = field(default_factory=lambda: ["quant-ph"])
    window_days: int = 7
    # Fetched-but-deduped overlap. v1 got this for free from a 14-day window;
    # v2's 7-day window would otherwise drop papers announced after last run.
    lookback_days: int = 7
    new_submissions_only: bool = True
    delay_seconds: float = 3.0
    # Per-request timeout. arxiv 4.0.1 sets none, and an unbounded hang in a cron
    # job also blocks every later run, because the flock is held throughout.
    request_timeout: float = 60.0
    page_size: int = 100
    max_results: int = 6000
    # Whole-search retries on a 429/5xx from arXiv, with exponential backoff.
    max_attempts: int = 4
    backoff_seconds: float = 60.0


@dataclass(frozen=True)
class ModelsConfig:
    # Stage A and Stage B run over the whole corpus, so both are cheap-tier.
    tag: str = "claude-haiku-4-5-20251001"
    readiness: str = "claude-haiku-4-5-20251001"
    # Industry matching now sees only the readiness shortlist (~20 papers), so it
    # could afford a stronger model; kept cheap because the choice is narrow.
    match: str = "claude-haiku-4-5-20251001"
    final: str = "claude-sonnet-5"
    verify: str = "claude-sonnet-5"


@dataclass(frozen=True)
class TagConfig:
    """Stage A: pipeline tagging over every paper."""

    papers_per_request: int = 20
    concurrency: int = 4
    max_tokens: int = 3072


@dataclass(frozen=True)
class ReadinessConfig:
    """Stage B: readiness scoring, and the shortlist it produces."""

    papers_per_request: int = 15
    concurrency: int = 4
    # Five checks x (score + note) per paper needs real headroom.
    max_tokens: int = 8192
    # How many papers reach industry matching. The brief says 20.
    shortlist_size: int = 20


@dataclass(frozen=True)
class MatchConfig:
    """Industry matching over the readiness shortlist only."""

    papers_per_request: int = 20
    concurrency: int = 2
    max_tokens: int = 2048
    # Below this, the paper is dropped rather than forced into a weak industry fit.
    min_relevance: int = 2
    # If narrowing by pipeline leaves fewer candidates than this, offer the full
    # eligible list instead -- a thin overlap means the pipeline vocabulary did not
    # describe that corner of the taxonomy, not that only two industries fit.
    min_candidates: int = 6
    # False by design: Haiku 4.5 needs a 4,096-token prefix to cache. See config.toml.
    use_prompt_caching: bool = False


@dataclass(frozen=True)
class ImproveConfig:
    """Part 2: the self-improvement pass. Mechanical, so it makes no model calls."""

    enabled: bool = True
    proposals_file: str = "state/proposals.yaml"
    feedback_file: str = "state/feedback.yaml"
    pipelines_file: str = "pipelines.yaml"
    # Occurrences before a low-risk proposal self-applies or a structural one is
    # escalated in the report.
    threshold: int = 3
    # Ceiling on what this pass may spend, as a fraction of the run. It currently
    # spends nothing, so this is a tripwire for a future version that adds a call.
    max_cost_share: float = 0.05


@dataclass(frozen=True)
class FinalConfig:
    concurrency: int = 3
    max_tokens: int = 8000
    effort: str = "medium"
    max_papers: int = 60
    allow_fulltext_fallback: bool = True
    max_fulltext_chars: int = 120000


@dataclass(frozen=True)
class VerifyConfig:
    enabled: bool = True
    concurrency: int = 3
    max_tokens: int = 4000
    effort: str = "medium"


@dataclass(frozen=True)
class IndustryConfig:
    """Industry scoring against industries.yaml.

    `enabled = false` turns the whole feature off: no secondary categories, no
    keyword pre-filter, no scoring calls, no extra digest sections.
    """

    enabled: bool = True
    taxonomy_file: str = "industries.yaml"
    # Log/store keywords firing on at least this share of papers, as trim candidates.
    noise_share: float = 0.01


@dataclass(frozen=True)
class DigestConfig:
    write_json: bool = True


@dataclass(frozen=True)
class RunConfig:
    """When does a partially-failed stage make the whole run a failure?

    A weekly cron job should survive a handful of bad papers but must not exit 0
    after every call failed -- that looks like "a quiet week" and hides outages.
    """

    max_failure_rate: float = 0.5


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 4
    base_delay: float = 2.0
    max_delay: float = 45.0


@dataclass(frozen=True)
class FulltextConfig:
    timeout: int = 60
    user_agent: str = "quantph-scout/0.1"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    max_bytes: int = 5_000_000
    backup_count: int = 5


@dataclass(frozen=True)
class PrefilterConfig:
    """Free engagement/quality proxies, applied before any model call.

    `journal_ref` defaults off on measured evidence: on run 1's 998 labelled
    papers it kept 2.2% and retained only 3.3% of the top-scored papers, because
    week-old preprints are not published anywhere yet. See config.toml.
    """

    enabled: bool = True
    combine: str = "any"
    journal_ref: bool = False
    experimental_phrases: bool = True
    phrases: list[str] = field(
        default_factory=lambda: [
            "we demonstrate", "we measure", "we report", "we observe",
            "fabricat", "experimentally", "measured", "device",
        ]
    )
    relevant_cross_list: bool = True


@dataclass(frozen=True)
class SelectConfig:
    max_papers: int = 10


@dataclass(frozen=True)
class BudgetConfig:
    """Projection inputs for the post-pre-filter budget guard.

    Token estimates are measured, not guessed: run 1's abstracts averaged 1,250
    characters (~312 tokens) and its titles 79.
    """

    target_usd: float = 3.50
    hard_cap_usd: float = 5.00
    est_tokens_per_paper: int = 340
    # Stage A: the pipeline list is ~1,200 tokens plus instructions.
    est_tag_prefix_tokens: int = 2500
    est_tag_output_per_paper: int = 55
    # Stage B: five checks with notes is the largest per-paper output in the run.
    est_readiness_prefix_tokens: int = 2700
    est_readiness_output_per_paper: int = 190
    # Industry matching (was triage) -- runs on the shortlist only.
    est_triage_prefix_tokens: int = 2700
    est_triage_output_per_paper: int = 48
    # Share of tagged papers that get an industry and so reach readiness scoring.
    # Measured at ~0.31 on run 18; 0.4 leaves headroom in the pessimistic direction.
    est_match_survival_share: float = 0.4
    est_final_input_per_paper: int = 1640
    est_final_output_per_paper: int = 3000
    est_verify_input_per_paper: int = 2040
    est_verify_output_per_paper: int = 1500


@dataclass(frozen=True)
class Price:
    input: float
    output: float


@dataclass(frozen=True)
class EmailConfig:
    """SMTP settings and presentation, read from the environment.

    `enabled` is False if anything required is missing, which is how a run with
    no mail configured stays a successful run rather than a crash.

    Everything here comes from the environment rather than config.toml because
    the recipient list is other people's addresses -- it is a secret, not
    configuration, and must never be committed. The non-sensitive fields
    (sender name, subject prefix, unsubscribe target) are environment variables
    too, simply so that all of email lives in one place.
    """

    host: str | None = None
    port: int = 587
    user: str | None = None
    password: str | None = None
    recipients: tuple[str, ...] = ()
    sender: str | None = None
    # Display name on the From: line.
    sender_name: str = "arXiv Industry Scout"
    # Prepended to every subject so the digest is filterable, and so the
    # model-free subject itself is free to be specific about *this* week.
    subject_prefix: str = "[quant-ph Scout]"
    # A URL (rendered as a link, enables one-click unsubscribe) or an address
    # (rendered as "just reply"). Empty falls back to replying to the sender.
    unsubscribe: str = ""
    # RFC 2919 list identity, e.g. "quantph-scout.example.com". A positive
    # deliverability signal; empty omits the header rather than inventing one.
    list_id: str = ""
    # Attach the Markdown source. Off by default: `text/markdown` is a subtype
    # many spam filters do not recognise, and the digest is already archived in
    # the repository and as a workflow artifact.
    attach_markdown: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.user and self.password and self.recipients)


@dataclass(frozen=True)
class Config:
    root: Path
    arxiv: ArxivConfig
    models: ModelsConfig
    run: RunConfig
    tag: TagConfig
    readiness: ReadinessConfig
    match: MatchConfig
    improve: ImproveConfig
    prefilter: PrefilterConfig
    final: FinalConfig
    verify: VerifyConfig
    select: SelectConfig
    budget: BudgetConfig
    industry: IndustryConfig
    digest: DigestConfig
    retry: RetryConfig
    fulltext: FulltextConfig
    logging: LoggingConfig
    pricing: dict[str, Price]
    email: EmailConfig
    api_key: str | None
    # Where the key came from: 'environment' (a real env var, which OVERRIDES .env),
    # '.env', or 'absent'. Recorded per run so spend can always be attributed.
    api_key_source: str = "absent"

    # Paths are derived from root rather than configured, so that a cron job with
    # a different cwd still writes to the same places.
    @property
    def db_path(self) -> Path:
        return self.root / "data" / "scout.db"

    @property
    def digest_dir(self) -> Path:
        return self.root / "digests"

    @property
    def log_path(self) -> Path:
        return self.root / "logs" / "scout.log"

    @property
    def prompt_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def taxonomy_path(self) -> Path:
        return self.root / self.industry.taxonomy_file

    @property
    def pipelines_path(self) -> Path:
        return self.root / self.improve.pipelines_file

    @property
    def proposals_path(self) -> Path:
        return self.root / self.improve.proposals_file

    @property
    def feedback_path(self) -> Path:
        return self.root / self.improve.feedback_file

    @property
    def lock_path(self) -> Path:
        return self.root / "data" / "scout.lock"

    def price(self, model: str) -> Price:
        """Price for a model, or a zero price if the model isn't in config.toml."""
        return self.pricing.get(model, Price(input=0.0, output=0.0))

    @property
    def api_key_fingerprint(self) -> str:
        """A short, non-reversible fingerprint of the API key.

        Safe to log and to store: it identifies *which* key a run used without
        revealing it, which is what you need when reconciling a run against the
        per-key spend in the Anthropic console.
        """
        if not self.api_key:
            return "none"
        return hashlib.sha256(self.api_key.encode()).hexdigest()[:16]

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        return self.api_key


def _sub(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"config.toml: [{key}] must be a table")
    return value


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Instantiate a config dataclass, ignoring unknown keys in the TOML."""
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(root: Path | None = None, *, env_file: Path | None = None) -> Config:
    """Load config.toml and .env from the project root."""
    root = (root or ROOT).resolve()

    config_path = root / "config.toml"
    if not config_path.exists():
        raise ConfigError(f"config.toml not found at {config_path}")
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    # override=False so a real environment variable beats the file, which is what
    # you want when testing a one-off key on the command line -- but it also means
    # a stray exported key silently wins over .env, so the source is recorded and
    # logged rather than left to be discovered.
    key_before_dotenv = os.getenv("ANTHROPIC_API_KEY")
    load_dotenv(env_file or (root / ".env"), override=False)
    key_after_dotenv = os.getenv("ANTHROPIC_API_KEY")
    if key_before_dotenv:
        api_key_source = "environment"
    elif key_after_dotenv:
        api_key_source = ".env"
    else:
        api_key_source = "absent"

    pricing = {
        model: Price(input=float(p.get("input", 0.0)), output=float(p.get("output", 0.0)))
        for model, p in _sub(raw, "pricing").items()
        if isinstance(p, dict)
    }

    recipients = tuple(
        addr.strip() for addr in (os.getenv("SMTP_TO") or "").split(",") if addr.strip()
    )
    smtp_user = os.getenv("SMTP_USER") or None
    email = EmailConfig(
        host=os.getenv("SMTP_HOST") or None,
        port=int(os.getenv("SMTP_PORT") or 587),
        user=smtp_user,
        password=os.getenv("SMTP_PASSWORD") or None,
        recipients=recipients,
        sender=os.getenv("SMTP_FROM") or smtp_user,
        sender_name=os.getenv("EMAIL_SENDER_NAME") or "arXiv Industry Scout",
        subject_prefix=(
            os.getenv("EMAIL_SUBJECT_PREFIX")
            if os.getenv("EMAIL_SUBJECT_PREFIX") is not None
            else "[quant-ph Scout]"
        ),
        unsubscribe=os.getenv("EMAIL_UNSUBSCRIBE") or "",
        list_id=os.getenv("EMAIL_LIST_ID") or "",
        attach_markdown=(os.getenv("EMAIL_ATTACH_MARKDOWN") or "").strip().lower()
        in {"1", "true", "yes", "on"},
    )

    return Config(
        root=root,
        arxiv=_build(ArxivConfig, _sub(raw, "arxiv")),
        models=_build(ModelsConfig, _sub(raw, "models")),
        run=_build(RunConfig, _sub(raw, "run")),
        tag=_build(TagConfig, _sub(raw, "tag")),
        readiness=_build(ReadinessConfig, _sub(raw, "readiness")),
        match=_build(MatchConfig, _sub(raw, "match")),
        improve=_build(ImproveConfig, _sub(raw, "improve")),
        prefilter=_build(PrefilterConfig, _sub(raw, "prefilter")),
        final=_build(FinalConfig, _sub(raw, "final")),
        verify=_build(VerifyConfig, _sub(raw, "verify")),
        select=_build(SelectConfig, _sub(raw, "select")),
        budget=_build(BudgetConfig, _sub(raw, "budget")),
        industry=_build(IndustryConfig, _sub(raw, "industry")),
        digest=_build(DigestConfig, _sub(raw, "digest")),
        retry=_build(RetryConfig, _sub(raw, "retry")),
        fulltext=_build(FulltextConfig, _sub(raw, "fulltext")),
        logging=_build(LoggingConfig, _sub(raw, "logging")),
        pricing=pricing,
        email=email,
        api_key=key_after_dotenv or None,
        api_key_source=api_key_source,
    )


def load_prompt(cfg: Config, name: str) -> str:
    """Read a prompt template from prompts/<name>.md at runtime."""
    path = cfg.prompt_dir / f"{name}.md"
    if not path.exists():
        raise ConfigError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")
