# handoff.md — quantph-scout v3 (pipeline layer)

Written 2026-10-02. The v2 spec is at `~/Desktop/arxiv_agent_v2_prompt.md`; the
pipeline-layer addendum is at `~/Desktop/pipeline_layer_addendum.md`.

Everything below is from the code and from real runs in `data/scout.db`. Where
something is untested I say so.

**Platform floor: Python 3.11.** `scout/config.py` uses `tomllib`, which is 3.11+.
Development and the venv here are 3.12.7 on macOS; the deployment target is
Raspberry Pi OS, which ships 3.11. No PEP 695 or other 3.12-only syntax is used —
keep it that way or the Pi cannot run it.

---

## 0. Read this first: the stage order is NOT the addendum's

The addendum says: score readiness, take the top 20, then match industries. **Built
that way, the report came out empty**, and the reason generalises:

* Readiness rewards work with no physical barriers. Software has none. On the first
  run under that order, `design_tools` papers averaged **7.5/10** and took **9 of
  the 20** shortlist slots — crystal-structure-prediction tools, generative design
  datasets, simulation harnesses.
* Readiness and industry relevance are **orthogonal**. Only 82 of 236 tagged papers
  had even a keyword-level industry hit, so a top-20-by-readiness shortlist is
  mostly papers with no industry.
* Result: matching dropped **18 of 20** for having no real industry fit, and
  verification dropped the other two with `industry_fits: false`. Zero papers
  reached the page.

So matching runs **before** readiness: `tag -> match -> readiness`. Readiness then
scores only papers that have an industry, which also makes it cheaper. Everything
else the addendum asks for is unchanged — pipeline tagging first, readiness out of
10, a top-N shortlist, report ordered by readiness, no repeated industries.

**If you ever restore the addendum's order, expect empty reports.** The
self-improvement layer does detect it (`industry-gap-shortlisted` fired with 18
papers on the first run), but it takes three weeks to escalate and the reports are
empty in the meantime.

## 1. What it does

1. **Fetch** — one arXiv query for `quant-ph` plus eight allowlisted cond-mat and
   physics subcategories, over a 7-day report window with a 7-day dedupe lookback.
   Categories come from `industries.yaml`, not from code.
2. **Exclude revisions**, then **dedupe** against `papers` (the seen-papers store).
3. **Gate** — papers arriving only via a secondary category must hit a
   `keyword_sets.experimental_evidence` term or they are dropped (stored with
   `gate_passed=0` so dedupe skips them forever). This used to require a *quantum*
   mention; see §4c.
4. **Keyword pre-filter (free)** — tier 1/2 only. Tier 3 is inert.
5. **Engagement pre-filter (free)** — configurable proxies, each reporting what it
   dropped. See §5.
6. **Budget guard (free)** — projects cost, trims to the strongest keyword evidence
   if the projection exceeds the cap.
7. **Tag** — Haiku 4.5, ~20 abstracts/request. Assigns the *pipeline*: the route by
   which the result could reach a product, from `pipelines.yaml`. Papers tagged
   `none` are dropped (~11–15%). The tagger is told to prefer a real pipeline at
   `low` confidence over `none`, because a paper dropped here is gone for good.
8. **Match** — Haiku 4.5, every tagged paper, **batched by pipeline** so the
   candidate industry list is genuinely narrowed (78 → a handful) rather than
   diluted into a union. A paper whose best fit is below `[match] min_relevance` is
   **dropped rather than forced**, per the addendum.
9. **Readiness** — Haiku 4.5, matched papers only. Five checks from
   `pipelines.yaml`, 0–2 each, out of 10 (denominator derived, not hardcoded).
   Produces the top-N shortlist. **This is what the report is ordered by.**
10. **Final pass** — Sonnet 5 on **abstracts only**, capped at 30. Confirms the
   industry, scores `disruption` 1–5, writes the report prose. Pulls full text only
   for a paper it flags `abstract_too_vague`.
11. **Selection** — greedy over (paper, industry) by **readiness**, then tag
    confidence, then disruption; no industry twice; at most 10.
12. **Verification** — Sonnet 5 over the selected papers only.
13. **Report** — Markdown + JSON sibling, opening with the funnel counts, each entry
    carrying its pipeline and readiness score, closing with a paste-ready feedback
    block. Optional SMTP.
14. **Self-improvement** — mechanical, free, no model calls. See §9.

## 2. File map

| File | What it does |
|---|---|
| `scout/__main__.py` | **Entry point.** `python -m scout` |
| `scout/cli.py` | argparse; `run` and `report` subcommands; exit codes |
| `scout/pipeline.py` | Stage orchestration, `--until`, `--dry-run`, failure-rate guard |
| `scout/ingest.py` | Window + lookback, query, version stripping, revisions, dedupe, 429 backoff |
| `scout/prefilter.py` | Free engagement proxies, with per-proxy drop accounting |
| `scout/budget.py` | Cost projection and the trim-to-fit guard |
| `scout/industries.py` | Loads + validates `industries.yaml`; effective-keyword resolution |
| `scout/matcher.py` | Keyword pre-filter: normalisation, tier rules, group rules, hit counts |
| `scout/batching.py` | Shared batching: `chunk`, ref-`align`, retry-the-unanswered |
| `scout/pipelines.py` | Loads + validates `pipelines.yaml`; renders it for prompts |
| `scout/tagging.py` | Pipeline tagging |
| `scout/match.py` | Industry matching (was `triage.py`); pipeline-group batching |
| `scout/readiness.py` | Readiness scoring and the shortlist |
| `scout/improve.py` | Proposals, feedback, and the one thing that self-applies |
| `scout/final.py` | The expensive pass: abstracts, with on-demand full text |
| `scout/select.py` | Greedy no-duplicate-industry selection. Pure, no I/O |
| `scout/verify.py` | Independent check of the selected papers |
| `scout/digest.py` | Markdown report + machine-readable JSON |
| `scout/fulltext.py` | arXiv HTML with PDF fallback, truncation |
| `scout/llm.py` | **The only code that calls the API.** Retries, call logging, usage |
| `scout/schemas.py` | Every Pydantic schema |
| `scout/db.py` | SQLite schema, queries, and `MIGRATIONS` |
| `scout/config.py` | `config.toml` + `.env`; API-key provenance |
| `scout/prompts.py` | Loads `prompts/*.md`, splits `## System` / `## User` |
| `scout/lock.py` | `flock`-based run lock |
| `scout/mailer.py` | Markdown -> HTML email + SMTP. Rendering and message assembly are covered by `tests/test_mailer.py` against a fake SMTP; **a real send has still never been performed** |
| `industries.yaml` | The taxonomy. Single source of truth for categories + keywords |
| `config.toml` | Models, thresholds, window, concurrency, budget, pricing |
| `prompts/*.md` | `tag`, `match`, `readiness`, `final`, `verify` — loaded at runtime |
| `pipelines.yaml` | The 13 pipelines + 5 readiness checks. Single source of truth |
| `state/proposals.yaml` | Improvement proposals carried between runs (agent writes) |
| `state/feedback.yaml` | Your ratings of past reports (agent only reads) |
| `eval/run_eval.py` | Triage precision/recall vs `eval/labels.csv` |
| `scripts/run_weekly.sh` | Cron entry point |

## 3. How to run it

```bash
cd ~/Desktop/Projects/quantph-scout
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env      # then add ANTHROPIC_API_KEY
```

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                          # 247 tests, no network. ~3s
.venv/bin/python -m scout run --until ingest        # FREE, no key needed. ~50s
.venv/bin/python -m scout run --dry-run             # + tag/match/readiness, no Sonnet
.venv/bin/python -m scout run --no-email            # full. ~$0.17 measured
.venv/bin/python -m scout report [--picks|--pipelines|--all|--keywords]
.venv/bin/python eval/run_eval.py
```

Exit codes: 0 ok, 1 run failed, 2 config error, 130 interrupted.

**`--until ingest` is genuinely free and needs no API key.** It exercises the
query, the gate, the keyword pre-filter, the engagement proxies and the cost
projection. It is the right way to test any taxonomy or allowlist change.

## 4. Measured numbers

**Scan volume**, 7 days (2026-09-15 → 2026-09-22), per category and unique
contribution to the deduped union:

| Category | Papers | Uniquely adds |
|---|---|---|
| `quant-ph` | 467 | +372 |
| `cond-mat.mtrl-sci` | 205 | +129 |
| `physics.optics` | 119 | +79 |
| `cond-mat.str-el` | 109 | +46 |
| `cond-mat.mes-hall` | 105 | +39 |
| `physics.app-ph` | 55 | +23 |
| `cond-mat.supr-con` | 52 | +20 |
| `physics.atom-ph` | 39 | +21 |
| `physics.ins-det` | 37 | +27 |
| **Deduped union** | **955/week** | |

Excluded, with what each would have added: `cond-mat.stat-mech` 139,
`physics.chem-ph` 73, `cond-mat.soft` 57, `physics.plasm-ph` 48,
`cond-mat.dis-nn` 32, `cond-mat.quant-gas` 30, `cond-mat.other` 23,
`physics.acc-ph` 16, `physics.hist-ph` 8.

**Run 8** (2026-09-30, full pipeline, 120 new papers after dedupe):

| Stage | Model | Calls | In / out | Cost |
|---|---|---|---|---|
| Triage | Haiku 4.5 | 6 | 56,926 / 5,814 | $0.086 |
| Final | Sonnet 5 | 3 | 10,654 / 1,187 | $0.033 |
| Verify | Sonnet 5 | 3 | 8,673 / 3,012 | $0.048 |
| **Total** | | **12** | 76,253 / 10,013 | **$0.167** |

**Twelve model calls for a complete report.** v1 made 1,062 and cost $8.29.

Projected at the measured 955-papers/week volume: **~$1.31**, against a $3.50
target and a $5.00 cap. Cap reached at ~6,120 papers/run.

**Triage shortlist rate: 10.8%** (13/120) after the coverage audit widened the
taxonomy (§4b). It was 2.5% before. Scaled to 955 papers/week that is ~100
shortlisted against `[final] max_papers = 60`, so the cap will bind — which is why
the shortlist is now ordered by keyword-evidence strength before it truncates.

## 4b. The coverage audit (2026-09-30) — read this before touching the taxonomy

The first full run reported 2 papers and looked like a filtering fault. It was not.
The audit that settled it is worth repeating whenever the report feels thin.

**Method.** Take every paper triage could not place at relevance 3, and ask a model
to name the industrial pipeline it bears on *unconstrained by the taxonomy*. Script
pattern is in the run-12 history; it cost $0.18 for 117 papers.

**Result.** Of 117 unplaced papers: **56 (48%) genuinely had no industrial
pipeline** — foundational physics, correctly rejected. **61 (52%) had a real
pipeline the taxonomy had no industry for**, and 27 of those were experimental,
against only 3 papers that got through.

**The diagnostic symptom was shoehorning.** Triage was filing a V-Ti
superconducting wire paper under `tungsten`, a rhenium superconductor under
`rare_earth_refining`, a visible laser under `led_gan`, and an ion-trap RF
quadrupole under `mature_node_foundry` — and scoring them 2, which is exactly
right: "a real connection, but through an intermediate step". **A cluster of
relevance-2 papers whose `best_industry` looks wrong is the signature of a
taxonomy gap, not a model problem.**

**Fix.** Seven tier-1/2 industries added (`hts_tape`, `lts_wire`,
`superconducting_electronics`, `fiber_lasers`, `graphene_2d`,
`sputtering_targets`, `thermoelectrics`), plus a `superconducting_materials`
keyword set. All hold the file's China-dominant premise.

**Measured effect on the same 120 papers:**

| | before (32 eligible) | after (39 eligible) |
|---|---|---|
| relevance 3 | 3 | 13 |
| relevance 0, "no industry fits" | 95 | 71 |
| keyword candidates | 15 | 42 |
| papers in the report | 2 | 5 |
| run cost | $0.17 | $0.32 |

**Open problem this created: `superconducting_electronics` is too broad.** It
absorbed 7 of the 13 shortlisted papers, and 6 were crowded out by the
no-duplicate-industry rule — so widening the taxonomy bought less report breadth
than the shortlist growth suggests. The bucket is mixing genuine superconducting
*electronics* (Josephson devices, cryogenic memory, SNSPDs) with superconducting
*thin-film materials physics* (FeTe heterostructures, 2D fluctuation studies).
Splitting it is the obvious next move.

## 4c. The premise change (2026-09-30) — quantum tech to experimental science

The `quantum_core` gate was discarding **75% of cond-mat/physics papers** for not
mentioning quantum. Among them: perovskite tandem solar cells, core-loss
measurement on electrical steel, phonon transport in thermoelectric clathrates,
fibre-optic pulse propagation, AEM water-electrolyser degradation, high-entropy
catalysts. All of those bear on industries already in `industries.yaml`.

**The trap: changing the gate alone does nothing.** The rest of the system was
quantum-framed, so those papers would reach triage and score 0 anyway:

* tier 1 was defined as "Direct **quantum-hardware** linkage";
* tier 2 was defined as "**Quantum sensing or metrology** applied to this industry";
* 11 of 39 eligible industries were defined in purely quantum terms — `polysilicon`
  was "feedstock for isotopically enriched Si-28 **spin-qubit** substrates", so a
  solar paper scored 0 against the one industry that should have matched it.

Four changes, which only work together:

1. **Gate** → `secondary_requires: experimental_evidence`. Asks "is there a
   measurement" instead of "is this quantum". Measured on 878 secondary-only
   papers: `quantum_core` kept 25%, this keeps 42%. **No code change was needed** —
   the gate already meant "hits any term in keyword set X".
2. **Tier definitions** rewritten domain-neutral. Tier 1 = the materials/devices
   studied *are* this industry's product or feedstock; tier 2 = the paper measures,
   monitors or improves its process, product or infrastructure.
3. **The 11 quantum-framed descriptions** rewritten to name each industry's actual
   main use. A test (`test_no_eligible_industry_is_defined_in_purely_quantum_terms`)
   now fails if one comes back.
4. **38 of the 68 inert tier-3 industries promoted** — 8 refining entries to tier 1,
   30 process/product entries to tier 2 — each given a `link` description, since
   without one the model only sees the industry's name. Plus **`solar_pv_cells`
   added**: the most concentrated industry on the list and entirely absent, which is
   why the perovskite paper had nowhere to go.

**30 tier-3 entries remain inert** — food, pharmaceutical formulation,
agrochemicals, assembly-only manufacturing. Deliberately: triage picks *one* best
industry, so an option no physics paper can match is not extra recall, it is an
extra wrong answer. v1's `tunneling` → "tunnel boring machines" is the precedent.

**Eligible industries: 39 → 78. Keyword terms: 298 → 509.**

**After widening a gate you MUST clear the papers it already rejected**, or dedupe
skips them forever:

```bash
sqlite3 data/scout.db "DELETE FROM papers WHERE gate_passed = 0;"
```

659 rows were cleared when this landed. A backup was taken first; do the same.

## 4d. The pipeline layer (2026-10-02)

**Three deliberate deviations from the addendum, all with reasons:**

1. **Stage order** — matching before readiness. See §0. This is the important one.
2. **`operating_conditions` is judged against the pipeline's incumbent**, not against
   room temperature absolutely. Scored literally, every superconducting and quantum
   paper gets 0 by definition and can never rank, which would silently delete
   `hts_tape`, `lts_wire` and `superconducting_electronics` from every report. A 77 K
   tape competing with a 4 K magnet is an *improvement*. The rule and its worked
   examples are in `prompts/readiness.md`.
3. **`disruption` is kept but no longer ranks.** The addendum never mentions it.
   Readiness orders the report; disruption is displayed and breaks readiness ties,
   which matters because readiness is a small integer over ~10 papers.

**Measured, run 18 (278 papers, the addendum's original order):**

| Pipeline | Papers | Avg readiness | Took shortlist slots |
|---|---|---|---|
| `design_tools` | 49 | **7.5** | **9 of 20** |
| `inspection` | 35 | 6.4 | 6 |
| `photonics` | 52 | 6.3 | 1 |
| `logic_memory` | 30 | 4.9 | 0 |
| `quantum_hardware` | 3 | 4.0 | 0 |

That table *is* the argument for the reorder: the rubric ranks software first and
physics last, and software has no industry.

**Per-check means are logged every run.** Watch them — `"not stated"` scores 1 per
the brief, so a check every paper ignores contributes a constant and discriminates
nothing. Run 18: `operating_conditions` 1.52, `scalability` 1.25,
`performance_margin` 1.41, `integration` 1.11, `reproducibility` **1.09**.
Reproducibility is effectively inert, as expected for single preprints; the
improvement layer raises a `scoring_change` proposal when a check goes fully flat.

## 5. Decisions and why

- **Synchronous API, not Batches.** The user chose this explicitly. Batches would
  halve the bill but add submit/poll/reconcile code, 24h expiry handling, and a
  Sunday-night start. At ~$1.31 the discount is not needed. **The Monday 10:00 cron
  is therefore unchanged.** Consequence: headroom under the cap is ~6,120 papers
  rather than ~14,000, which is why the budget guard and free pre-filter matter.
- **The verification pass was kept**, against the spec, with the user's agreement.
  v1's critic measurably took claims away; nothing else does that. Scoped to the
  selected papers and reading abstracts makes it ~$0.05–0.17 instead of $4.22. It
  earned its place immediately: in run 8 it dropped 1 of 3 papers (a Josephson
  junction paper filed under *indium refining* because the device used InAs).
  **Known weakness:** checking prose against an abstract cannot catch a number the
  abstract states correctly and the body contradicts.
- **No prompt caching**, and this is measured, not lazy. Haiku 4.5's minimum
  cacheable prefix is **4,096 tokens**; the static block measured **~2,700**, so a
  `cache_control` marker silently never caches. Even if it did, 20-paper batching
  already amortises the prefix 20×, worth ~$0.04–0.09/run. `model_calls` has
  `cache_read_input_tokens` / `cache_creation_input_tokens` so this stays checkable.
  **2,700 is closer to 4,096 than expected — re-check if the industry list grows.**
- **The 7-day window keeps a 7-day lookback** (`[arxiv] lookback_days`). This is
  what replaces v1's 14-day overlap. Without it, every late-announced paper is
  silently lost forever. The extra papers are deduped before any model call, so it
  is free. Do not remove it to "simplify".
- **Engagement filter is proxies, not citations.** Semantic Scholar indexes arXiv
  and its `paper/batch` endpoint would cover a week in ~3 requests with a free key —
  but a 7-day-old preprint has no citations, so the signal is 0 for nearly every
  paper. Unauthenticated S2 also 429s under load (it is a shared pool; it failed on
  every attempt during development, including for a 2017 paper). Altmetric is worse.
- **`journal_ref` proxy is OFF by default and must stay off.** Measured on run 1's
  998 labelled papers: keeps 2.2%, retains **3.3%** of top-scored papers. As a gate
  it discards 119 of 123 good papers. Adding it to the default OR changed recall by
  exactly zero. `journal_ref` *is* now stored (v1 didn't store it) so the
  measurement is reproducible.
- **Default proxy set is `experimental_phrases OR relevant_cross_list`**: keeps
  42.9%, retains 84.2% of top-scored papers. `AND` retains only 68.4%.
- **Tier 3 is inert, not deleted.** 30 entries remain (was 68 — see §4c). They are
  never eligible, never reach a prompt, and produce no `industry_matches` rows.
  Evidence: run 1 wrote 12 tier-3 rows, run 8 wrote 0. `matcher.py` still computes
  `PaperMatch.tier3_tags` — nothing consumes it, which is the remaining loose end if
  you want tier 3 gone entirely.
- **The gate tests the presence of a measurement, not a subject area.** That is the
  one line of defence left between eight cond-mat/physics subcategories and triage,
  so if the report ever fills with theory, look there first.
- **Selection re-runs after verification** rather than patching its first result,
  so there is exactly one implementation of the no-duplicate-industry rule. A
  consequence: a paper promoted into the report by that second pass was never
  verified, and is marked *unverified* on the page.
- **`UNIQUE (run_id, industry_id)` on `selections`** makes the no-duplicate rule a
  database invariant, not just a promise made by `select.py`.
- **The shortlist is ordered by keyword-evidence strength before `[final]
  max_papers` truncates it** (`matcher.evidence_strength`, shared with the budget
  guard). Every shortlisted paper scores exactly `relevance == 3`, so without that
  ordering the cut falls in arXiv-ID order — at random. This only started to matter
  once the taxonomy widened and the cap began to bind.

## 5b. Conventions these files follow

Treat these as rules unless you have a reason to break one, and say so if you do.

- **`industries.yaml` is the single source of truth.** No industry name, group
  name, or keyword appears anywhere in Python. Category lists come from the YAML
  too, so the arXiv scope and the taxonomy cannot drift apart.
- **Prompts live in `prompts/*.md`, loaded at runtime.** Note `triage.md`'s
  *system* half is now also a template, taking `{industry_list}` and
  `{tier_definitions}` — so the eligible industries are never hard-coded.
- **Every Pydantic schema lives in `scout/schemas.py`**, in one file.
- **The official `anthropic` SDK, used directly.** `scout/llm.py` is the *only*
  module that calls the API — keep that chokepoint.
- **`requirements.txt` is pinned to exact versions.**
- **Secrets in `.env` (gitignored), `.env.example` committed.**
- **Logging to `logs/scout.log` with rotation, and a nonzero exit on failure.**
- **Dates are computed at runtime, never hardcoded.** `tests/test_window.py`.
- **Every model call is logged to `model_calls`** — including failed attempts. Do
  not add a call path that bypasses it.

## 6. Things that bit me, so they don't bite you

- **Telling the triage model "only 3 survives" made it never award 3.** The first
  run scored 0/120 papers at relevance 3 — genuinely relevant papers (Josephson
  junctions in Al-InAs, a tunable single-photon source) all capped at 2. Stating
  the consequence of a score makes the model conservative about awarding it.
  Rewriting the scale in terms of **directness** ("if you can name the specific
  thing the industry makes that this paper touches, it is a 3"), and deleting the
  "reserve it / very few are 3" language, moved it to a 3.3% rate. **If you edit
  `prompts/triage.md`, do not reintroduce the survival consequence.**
- **`Field(ge=, le=)` bounds are NOT enforced by the API.** The SDK moves them into
  the field *description*. Use `Literal[...]` — that becomes a real enum.
  `tests/test_schemas.py` pins this for `relevance` and `disruption`, and also
  asserts no schema has smuggled a numeric bound into a description.
- **arXiv returns HTTP 500 for `max_results=0`**, not a 4xx. It reads like an
  outage or a rate limit and is neither. Use `max_results=1` and read
  `opensearch:totalResults` for a count.
- **`db.MIGRATIONS` is append-only.** `CREATE TABLE IF NOT EXISTS` does nothing to
  an existing table, so every new column must be added there. This has already
  broken a run once.
- **`triage.applied_relevance` changed scale**: 1–5 in v1, 0–3 from v2. The v2
  column to query is `triage.relevance`, which only ever holds 0–3. Any query
  spanning both eras must filter on `run_id`.
- **`eval/run_eval.py` and `--until ingest` both create `runs` rows**, so `MAX(id)`
  is not the last real pipeline run. Filter on `stage_reached = 'digest'`.
- **Dedupe makes re-testing look like a quiet week.** A second run over the same
  window finds nothing new. Delete the rows first:
  `DELETE FROM papers WHERE first_seen_run = N;`
- **A stage losing >50% of its papers fails the whole run**
  (`[run] max_failure_rate`), so an expired key exits 1 instead of looking quiet.
  Nothing attempted is *not* a failure.
- **arXiv rate limits are easy to hit while developing.** Wrapped with real backoff
  (60/120/240s). The API host (`export.arxiv.org`) limits independently of the
  full-text hosts (`arxiv.org`).
- **Batched triage must match by echoed `ref`, not position.** A reordered or short
  response would otherwise hand paper 1's verdict to paper 3. `triage.py` aligns by
  ref and retries unanswered papers individually; both are tested.

## 9. Self-improvement: what it may and may not do to itself

Mechanical and **free** — it reads the run's own outputs plus `state/feedback.yaml`
and makes no model calls, so it cannot fail in a way that costs money and sits well
inside the addendum's "about 5% of the run's cost" ceiling.

**Only `new_discovery_signal` is ever applied automatically**, and only after 3
occurrences. New pipelines, new or retired industries, scoring changes and prompt
changes are escalated in the report under **Suggested improvements** and never
self-applied. The line is there because a system that rewrites its own scoring rules
cannot be audited by reading a diff. `tests/test_improve.py` pins this per type.

**Occurrences count separate weeks, not papers.** Ten papers hitting one gap in a
single week is one occurrence, or an unusual week would trip the threshold alone.

An auto-applied edit is attributed in `pipelines.yaml` (`# auto-added <date> after
N occurrences (<proposal id>)`) *and* listed in the report under **Changes applied
automatically**. A config the agent edited must say so on the page, not only in a
log file.

A `rejected` proposal is never resurrected — the reader already answered.

## 9b. Run 19: what the reorder actually produced

First run under `tag -> match -> readiness` (2026-10-02, 504 papers after the free
filters):

```
504 scanned -> 396 tagged (108 dropped as 'none')
            -> 77 matched  (319 dropped rather than forced into a weak fit)
            -> 20 shortlisted (readiness 8-10 of 10)
            -> 20 assessed -> 10 selected -> 5 survived verification -> 8 featured
```

**$1.33 actual against $1.51 projected — 89% accurate.** Industries featured:
`mature_node_foundry`, `gallium`, `small_li_cells`, `superconducting_electronics`,
`forgings`, `refractories`, `sputtering_targets`, `solar_pv_cells` — including three
of the promoted former tier-3 entries, which is the breadth the taxonomy work was
for.

**Two numbers to watch:**

* **Matching drops 80% (319 of 396).** That is the no-forcing rule working as
  instructed, but it means most of the tagging spend buys nothing. If cost ever
  matters, tighten Stage A rather than loosening this.
* **Verification dropped 5 of 10 selected papers**, all on `industry_fits: false`.
  That is a high rate and it points upstream: `[match] min_relevance = 2` is lenient
  enough to let through fits that an independent check rejects. Raising it to 3 would
  move that rejection earlier and cheaper. Left at 2 for now because the sample is
  one run.

## 7. Known issues and debt

- **`design_tools` still tops the ranking when it does get an industry.** Run 19's
  #1 entry was a photonics *simulation method* at 10/10 readiness, filed under
  `mature_node_foundry`. Defensible — faster simulation does help a foundry — but a
  weekly physics-to-industry scan probably should not lead with a software tool, and
  software will always win a rubric that measures physical barriers. Options:
  `action: drop` on the pipeline, or exclude it from the top slot. Not done because
  it is a judgement call about what the report is for.
- **`design_tools` is a readiness magnet** (49 papers, avg 7.5/10). Matching now
  catches most of them, but they still consume tagging and matching budget. Consider `action: drop` on it if the cost
  ever matters — its own `typical_domains` say "R&D acceleration across industries",
  which is not an industry input.
- **`reproducibility` is effectively inert** (mean 1.09 — almost every paper is
  "not stated"). It contributes a near-constant +1 to every total, so the real scale
  is closer to 0–9 than 0–10. Rewording it or dropping it would sharpen the ranking.
- **`superconducting_electronics` is too broad** — see §4b. 7 of 13 shortlisted
  papers landed in it and 6 were crowded out. Split it before adding anything else.
- **The shortlist will hit the 60-paper cap at full volume.** Run 12's 10.8% rate on
  955 papers/week projects to ~100 shortlisted against `[final] max_papers = 60`.
  The evidence-strength ordering means the cut is principled rather than random, but
  a cap that always binds is still a quality ceiling. Raising it is cheap (~$0.011
  per paper); tightening triage would be better.
- **Run 12's 120 papers were a biased sample** — the leftovers after dedupe from a
  mostly-ingested window, not a clean week. The 10.8% rate needs confirming on a
  real full run before tuning against it.
- **Triage marked a simulation-only paper `experimental: true`** (run 12, the
  JFS-CryoMem entry). The verification pass caught it in the catch line — "all based
  on computer models and simulations" — but it should not have been shortlisted. One
  instance, worth watching rather than acting on yet.
- **`_institution` in `digest.py` always returns empty.** arXiv has no institution
  field and v1 never stored one. The brief said "institution if in metadata", so the
  seam exists and is documented, but no institution is ever printed.
- **`eval/labels.csv` still has only the 3 template rows**, and their notes are
  written against v1's criteria. It needs 30–50 real labels re-judged against the
  0–3 scale before it measures anything.
- **Never tested against a real server:** SMTP delivery. `mailer.py` is now
  exercised by 32 tests, but they stub `smtplib`, so they prove the message is
  *built* correctly (Bcc, multipart, List-Unsubscribe, password normalisation)
  and not that a real mailbox accepts it. Also never tested: the Raspberry Pi,
  `CRON_TZ` on Raspberry Pi OS, the README's GitHub Actions alternative, the
  full-text fallback path in `final.py` (no paper has yet set `abstract_too_vague`),
  and the budget guard on a real over-cap run (only on synthetic input).
- **No `git` repository.** Never initialised — no history for any of this.
- **`.env` is `-rw-r--r--`;** should be `chmod 600`.
- **No flaky tests observed** — 247 pass deterministically, nothing touches network.
