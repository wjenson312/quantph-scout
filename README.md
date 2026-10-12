# quantph-scout

A weekly digest that reads new arXiv papers — **quant-ph** plus an allowlist of
**cond-mat** and **physics** subcategories — and reports the handful that could
change one of the industries in `industries.yaml`, where supply or production is
concentrated outside the US.

The scope is **experimental physical science**, not quantum technology
specifically: solar cells, electrical steel, thermoelectrics, catalysts,
metallurgy and power electronics all count, and so does anything quantum. What a
paper must have is a real measurement — see "Why the gate tests for a measurement".

It runs under cron every Monday at 10:00 America/Chicago, writes a short Markdown
digest to `digests/YYYY-MM-DD.md` plus a machine-readable `.json` beside it, and
optionally emails it.

The report is deliberately **short and selective**: at most 10 papers, at most one
per industry, four lines each, written in plain language. A week with nothing worth
reporting produces a one-line report, and that is a feature. Every entry has to
name its catch — the gap between the lab result and real use.

Measured weekly scan volume for the configured allowlist: **955 papers**, for a
projected **~$2 per run** against a $3.50 target and a $5 hard cap.

---

## How it works

Cheapest stage first. Everything before triage is free and needs no API key.

| # | Stage | Model | What it does |
|---|-------|-------|--------------|
| 1 | **Ingest** | — | Queries the `arxiv:` allowlist in `industries.yaml` (cross-lists included) over a 7-day window plus a dedupe lookback, drops replacements and revised versions, and skips arXiv IDs already in the database. Secondary-category papers must hit `keyword_sets.quantum_core` or they are dropped. |
| 1b | **Keyword pre-filter** | — (free) | Matches every new paper against tier-1/2 industries. Tier 3 is not an eligible match. |
| 1c | **Engagement pre-filter** | — (free) | Cheap proxies for "this is a real experimental result", each configurable, each reporting what it dropped. See below — one of them is off for measured reasons. |
| 1d | **Budget guard** | — (free) | Projects the run's cost. If it would exceed the cap, trims to the strongest keyword evidence and records what it shed. |
| 2 | **Pipeline tagging** | `claude-haiku-4-5-20251001` | Title + abstract, **~20 papers per request**. Assigns the *route* by which the result could reach a product, from `pipelines.yaml`. Papers tagged `none` are dropped. |
| 3 | **Industry matching** | `claude-haiku-4-5-20251001` | Every tagged paper, **batched by pipeline** so the candidate industry list narrows from 78 to a handful. A paper whose best fit is weak is **dropped, not forced**. |
| 4 | **Readiness scoring** | `claude-haiku-4-5-20251001` | Matched papers only. Five checks from `pipelines.yaml`, 0–2 each, out of 10. Produces the top-20 shortlist, and **this is what the report is ordered by**. |
| 5 | **Final pass** | `claude-sonnet-5` | **Abstracts only**. Confirms the industry, scores `disruption` 1–5, writes the report prose. Pulls full text only for a paper whose abstract it says is too vague. |
| 6 | **Selection** | — | Ranks (paper, industry) pairs by readiness, then tag confidence, then disruption. Greedily takes the best, removes that paper *and* that industry, repeats to at most 10. |
| 7 | **Verification** | `claude-sonnet-5` | Independent check of the **selected** papers only: every number against the source, whether the industry really fits, what was overstated. |
| 8 | **Digest** | — | Orders by readiness, renders the Markdown report and the JSON sibling, optionally emails. |
| 9 | **Self-improvement** | — (free) | Logs where the run forced a fit, carries proposals between weeks, applies only low-risk ones, escalates the rest in the report. |

### Why matching runs before readiness scoring

The pipeline-layer addendum specifies the reverse — score readiness, take the top 20,
then match industries. **Built that way the report came out empty**, and the reason
generalises:

Readiness rewards work with no physical barriers, and software has none. A crystal
structure prediction tool needs no cryogenics, scales infinitely, and integrates with
existing tooling, so it scores near 10/10 — and it has no industry in the taxonomy.
Measured on the first run under that order:

| Pipeline | Papers | Avg readiness | Shortlist slots taken |
|---|---|---|---|
| `design_tools` | 49 | **7.5** | **9 of 20** |
| `inspection` | 35 | 6.4 | 6 |
| `photonics` | 52 | 6.3 | 1 |
| `logic_memory` | 30 | 4.9 | 0 |
| `quantum_hardware` | 3 | 4.0 | 0 |

Readiness and industry relevance are **orthogonal** — only 82 of 236 tagged papers
had even a keyword-level industry hit. So matching dropped 18 of the 20 shortlisted
papers for having no real industry, verification dropped the other two, and nothing
reached the page.

Matching now runs first, over every tagged paper, and readiness scores only what has
an industry. Everything else the addendum asks for is unchanged: tagging first,
readiness out of 10, a top-N shortlist, report ordered by readiness, no repeated
industries. **If you restore the original order, expect empty reports.**

### Why operating conditions are judged against the incumbent

`operating_conditions` asks whether a result works without cryogenics, vacuum, or
lab-grade isolation. Scored literally, **every superconducting and quantum paper gets
0 by definition** and can never rank — which would silently delete `hts_tape`,
`lts_wire` and `superconducting_electronics` from every report, days after they were
added.

So it is scored against what the incumbent in that paper's *pipeline* already
requires. A 77 K tape competing with a 4 K magnet is an improvement, not a barrier; a
thin film that needs a cryostat to do a room-temperature job is still a 0. The rule
and its worked examples are in `prompts/readiness.md`, and the note has to say what
was compared against ("77 K vs 4 K incumbent") so the reasoning is auditable.

### Watch the per-check means

`"not stated"` scores 1, per the addendum. That makes a check every paper ignores
contribute a *constant* and discriminate nothing. Every run logs the mean per check:

```
readiness mean per check: operating_conditions 1.62, scalability 1.32,
integration 1.30, performance_margin 1.43, reproducibility 0.95
```

`reproducibility` sits near 1 because single preprints rarely report independent
replication — so the real scale is closer to 0–9 than 0–10. The self-improvement
layer raises a `scoring_change` proposal when a check goes completely flat.

### Why the window overlaps

The report covers 7 days, but the query reaches back 14 (`window_days` +
`lookback_days`). That is deliberate and it is not redundant.

arXiv announces papers on a delay, so a paper submitted on Sunday evening may not
be retrievable when Monday's run fires. Querying only the last 7 days would drop
it permanently. Reaching further back and deduping on the version-stripped arXiv
ID (`2609.01234v2` → `2609.01234`) means the next run still sees it and nothing is
processed — or paid for — twice. The extra papers are discarded before any model
call, so the overlap is free.

The window is always computed at runtime from the clock. No dates are hard-coded
anywhere; `tests/test_window.py` enforces that.

### Why the gate tests for a measurement, not for quantum content

Papers that reach the pipeline only through a secondary category (cond-mat,
physics) must pass a keyword gate, because eight whole subcategories would
otherwise flood triage. Until 2026-09-30 that gate asked *"is this quantum"*, and
it was discarding **75% of cond-mat/physics papers** — including perovskite tandem
solar cells, core-loss measurements on electrical steel, phonon transport in
thermoelectric clathrates, electrolyser degradation and high-entropy catalysts.
Every one of those bears on an industry already in the file.

It now asks *"is there a measurement here"* (`keyword_sets.experimental_evidence`).
Measured on 878 secondary-only papers: the old gate kept 25%, this keeps 42%.

**Changing the gate alone does nothing**, and this is the part worth remembering.
The rest of the taxonomy was quantum-framed, so freed papers reached triage and
scored 0 anyway:

- tier 1 was defined as "Direct **quantum-hardware** linkage";
- tier 2 as "**Quantum sensing or metrology** applied to this industry";
- 11 of 39 eligible industries were defined in purely quantum terms. `polysilicon`
  read "feedstock for isotopically enriched Si-28 **spin-qubit** substrates", so a
  solar paper scored 0 against the one industry that should have matched it.

Four things changed together: the gate, both tier definitions, those 11
descriptions, and **38 of the 68 inert tier-3 industries were promoted** (plus
`solar_pv_cells` added, the most concentrated industry on the list and previously
absent). Eligible industries went **39 → 78**, keyword terms **298 → 509**.

Measured on the 278 papers the old gate had rejected — a population that
previously produced **nothing at all**:

| | result |
|---|---|
| relevance 3 | 7 |
| papers in the report | 5 |
| run cost | $0.37 |

All five sit in new or rewritten industries: AlGaN power FETs under `gallium`
(a non-quantum use its old description excluded), perovskite impurity screening
under `solar_pv_cells`, radiative coatings under `thermoelectrics`, plus
`graphene_2d` and `sputtering_targets`.

**30 tier-3 entries stay inert on purpose** — food, pharmaceutical formulation,
agrochemicals, assembly-only manufacturing. Triage picks *one* best industry, so an
option no physics paper can match is not extra recall, it is an extra wrong answer.

**After widening a gate, clear the papers it already rejected** or dedupe skips them
forever:

```bash
sqlite3 data/scout.db "DELETE FROM papers WHERE gate_passed = 0;"
```

### Why the engagement filter is proxies, not citations

The brief asked for a real engagement signal. There isn't one for papers this new:

* **Semantic Scholar** does index arXiv preprints, and its `paper/batch` endpoint
  would cover a week in ~3 requests with a free API key (unauthenticated is a
  shared, contended pool that returns 429 under load). But a seven-day-old
  preprint has essentially no citations, so `citationCount` is 0 for nearly every
  paper and cannot rank or filter anything.
* **Altmetric** has the same problem, plus thin preprint coverage and a licence
  that does not cover this.

So the pipeline uses proxies. Each was measured against run 1's 998 papers, using
that run's own LLM triage scores as ground truth. "Recall" is the share of
top-scored papers the proxy keeps:

| Proxy | Keeps | Recall | Default |
|---|---|---|---|
| Journal ref / "accepted"/"published in" | 2.2% | **3.3%** | **off** |
| Experimental phrases in the abstract | 27.2% | 78.9% | on |
| Cross-listed into an allowlisted category | 27.3% | 73.7% | on |
| *phrases OR cross-list (the default)* | *42.9%* | *84.2%* | — |
| phrases AND cross-list | 11.5% | 68.4% | — |

The journal-reference proxy is **off by default and that is the finding, not an
oversight**: as a gate it would discard 119 of 123 genuinely good papers, and
adding it to the default OR changed recall by exactly zero. Every proxy reports
what it dropped — and what it *would* have dropped when disabled — so the
measurement survives being switched off.

### Why there is still a verification pass

The brief's pipeline had none. Dropping it outright would have been a capability
loss rather than a simplification: in run 1 the old critic measurably took claims
away, downgrading confidence and removing applications that ignored operating
constraints. Nothing else in the pipeline does that, because the model that writes
the prose is a poor judge of whether its own prose overreached.

What makes it affordable here is scope. It runs on the **selected** papers (at
most 10, not the whole shortlist) and reads the abstract rather than 120k
characters of full text — about **$0.19 a run**.

The trade is worth stating plainly: checking prose against an abstract is weaker
than checking it against the full paper. It catches a number that was never in the
source, a claim the source does not support, and an industry that does not fit. It
cannot catch a number the abstract states correctly and the body contradicts. Where
the final pass already pulled full text, the check is as strong as v1's.

### Why there is no prompt caching

Because it would not work and would not help, and both halves matter:

* Haiku 4.5's minimum cacheable prefix is **4,096 tokens**. The static block
  (instructions + tier definitions + 32 eligible industries) measured **~2,700** in
  a real run. Below the minimum, a `cache_control` marker silently does nothing —
  no error, no saving. That margin is thinner than it first looked, so re-check it
  if the eligible industry list grows a lot.
* Even if it cached, batching 20 abstracts per request already amortises that
  prefix 20×, leaving **$0.04–0.09 per run** on the table.

`model_calls` has `cache_read_input_tokens` and `cache_creation_input_tokens`
columns so this stays a measurement rather than a belief. If you ever enable
`[triage].use_prompt_caching`, check those columns are nonzero before assuming it
did anything.

### Why the synchronous API, not Batches

The Batches API would halve the bill (~$1 saved per run) at the cost of
submit/poll/reconcile code, 24-hour expiry handling, and moving the run to Sunday
night so batch turnaround cannot miss the 10:00 delivery. At ~$2 against a $3.50
target the discount is not needed, so the simpler path won and **the Monday 10:00
schedule is unchanged**.

The consequence to know: without the 50% discount, headroom under the $5 cap is
~6,000 papers per run rather than ~14,000. That is why the budget guard and the
free pre-filter are load-bearing rather than decorative.

### The industry taxonomy

`industries.yaml` is the single source of truth for the industry layer. No industry
name, group name, or keyword appears anywhere in the Python. It defines the arXiv
category allowlist (with the measured per-category volumes behind each choice),
eight shared `keyword_sets`, nine `groups`, and 107 `industries` — of which the 39
in tiers 1 and 2 are the eligible matches.

**Tiers.** Tier 1 is direct quantum-hardware linkage (the paper's materials or
devices *are* this industry's product). Tier 2 is quantum sensing or metrology
applied to the industry.

**Tier 3 is unused.** The 68 tier-3 entries stay in the file but are inert: they are
not eligible matches, they are never offered to a model, and tier-3 tagging was
removed from the pipeline along with the "China-dominant supply chain" line it fed
— they no longer even produce `industry_matches` rows.
They are kept so that deleting them can be a separate, deliberate decision rather
than something lost in a rewrite. The loader is tier-agnostic, so nothing needed to
change to make them inert — and a test asserts none of their ids can reach a prompt.

**Effective keywords.** An industry matches on the union of three sources: its own
`keywords`, the `keyword_sets` it names itself, and the `keyword_sets` its group
names. The first two are *industry-specific*; the third is *group-level*. That
distinction decides what gets tagged:

- A hit on industry-specific vocabulary tags **that industry**.
- A hit on group-level vocabulary tags **the group**, not its members — unless a
  tier-1/2 industry in that group also matched specifically, in which case the
  specific match wins. This is what stops a vague "magnetic anomaly" from tagging
  all nine critical-mineral industries at once.

**Matching** is case-insensitive and word-boundary, over title + abstract, with
hyphens, slashes and colons flattened to spaces on both sides — so `cryo-CMOS`
matches "cryo CMOS", and `Er:YSO` matches "Er YSO".

Two escape hatches exist because word boundaries alone are not enough, and both
live in the YAML rather than in code:

- **`case_sensitive`** — chemical formulae that collide with English words once
  lowercased. `GaP` vs. "gap" is the bad one: left case-insensitive it fires on
  7.8% of all papers (band gap, energy gap, spectral gap) and makes Gallium
  refining the top match by a factor of twelve. Shipped with `[GaP, TiN, TES]`.
  Empty the list to restore fully case-insensitive matching.
- **`exclusions`** — phrases in which a keyword should not count. Shipped with
  `bearing` excluded inside "bearing in mind".

**Validation** is strict and reports every problem at once, before any network
call or spend: duplicate ids, a `tier` outside {1,2,3}, a `group` that isn't
defined, a `keyword_sets` reference that doesn't resolve, an `exclusions` or
`case_sensitive` entry naming a keyword that doesn't exist, and any industry whose
effective keyword set is empty (it could never match).

**Turning it off.** Set `[industry] enabled = false` in `config.toml` and the whole
layer disappears: quant-ph only, no pre-filter, no scoring calls, no extra digest
sections.

**Trimming noisy keywords.** Every run stores per-keyword hit counts. After a run:

```bash
sqlite3 -box data/scout.db \
  "SELECT keyword, paper_count, owners FROM keyword_hits
   WHERE run_id = (SELECT MAX(id) FROM runs) AND owners != '[]'
   ORDER BY paper_count DESC LIMIT 20;"
```

Anything firing on more than ~1% of papers is worth a look. Cross-check it against
what the model actually did with it — a keyword whose links all score 0 is dead
weight:

```bash
sqlite3 -box data/scout.db \
  "SELECT name, score, COUNT(*) n FROM industry_links
   WHERE run_id = (SELECT MAX(id) FROM runs) GROUP BY name, score ORDER BY name, score;"
```

---

## Setup on a fresh Raspberry Pi

Raspberry Pi OS (Bookworm or later) ships Python 3.11, which is the minimum.

```bash
python3 --version    # need 3.11 or newer
```

### 1. Get the code and build a venv

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo-url> ~/quantph-scout
cd ~/quantph-scout
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

On a Pi Zero or Pi 3 the `pydantic-core` wheel may not be prebuilt for your
platform and pip will try to compile it (slow, and it needs Rust). If that
happens, install the Debian package instead and build the venv with system
packages visible:

```bash
sudo apt install -y python3-pydantic
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Add your API key

```bash
cp .env.example .env
nano .env          # paste your ANTHROPIC_API_KEY
chmod 600 .env     # it holds a live credential
```

Get a key at <https://console.anthropic.com/settings/keys>. `.env` is gitignored;
`.env.example` is the committed template.

### 3. Check it works, cheaply

```bash
# Stage 1 only. No API key needed, no cost -- just proves arXiv access.
.venv/bin/python -m scout run --until ingest --since 2026-09-15

# Ingest + triage on a past week. Costs about $0.60 at a full week's volume;
# makes no Sonnet calls at all.
.venv/bin/python -m scout run --dry-run --since 2026-09-15

# Read the triage output before spending money on the later stages.
.venv/bin/python -m scout report
```

`report` prints the shortlist with the industry each paper was filed under and the
model's one-line reason, so you can see whether the threshold is where you want it
before paying Sonnet. `report --all` shows every paper that was triaged, not just
the ones that passed; `report --picks` shows what reached the report, in order;
`report --keywords` lists the noisiest keywords as candidates to trim from
`industries.yaml`.

Note that `--until ingest` is genuinely free and needs no API key, which makes it
the right way to test a taxonomy or allowlist change: it exercises the query, the
gate, the keyword pre-filter, the engagement proxies and the cost projection
without spending anything.

### 4. Full run

```bash
.venv/bin/python -m scout run --no-email
cat digests/$(date +%F).md
cat digests/$(date +%F).json     # the machine-readable sibling
```

Drop `--no-email` once you're happy with the output and have SMTP configured.

The run refuses to exceed `[budget].hard_cap_usd`: the projection runs after the
free filters and trims the triage input if it would go over. If a run's *actual*
cost ever exceeds the cap, that is logged as an error and means the token
estimates in `[budget]` have drifted — re-derive them from `model_calls` rather
than raising the cap.

### 5. Verify the Pi's clock and cron

Cron's `CRON_TZ` handles the schedule, but a Pi with a wrong system clock will
still query the wrong arXiv window, so check both.

```bash
timedatectl                      # look for the right Time zone and "System clock synchronized: yes"
sudo timedatectl set-timezone America/Chicago   # optional; CRON_TZ works regardless
systemctl status cron            # should be "active (running)"
sudo systemctl enable --now cron # if it isn't
```

A Pi has no battery-backed real-time clock. If it boots without a network it will
have a nonsense date until NTP catches up, and the window will be wrong. Confirm
`System clock synchronized: yes` before trusting a run.

### 6. Install the cron job

```bash
crontab -e
```

Paste this (the full contents are in `scripts/crontab.txt`):

```cron
CRON_TZ=America/Chicago
MAILTO=""

# min hour dom mon dow  command
0 10 * * 1 /home/pi/quantph-scout/scripts/run_weekly.sh
```

Adjust the path if you cloned somewhere other than `/home/pi/quantph-scout`.
`CRON_TZ` must come **before** the job line — it applies to every schedule line
that follows it. Then:

```bash
crontab -l                       # confirm it's installed
tail -f ~/quantph-scout/logs/cron.log
```

Two notes on `CRON_TZ`:

- It is a feature of Debian's cron (vixie-cron/cronie), which is what Raspberry Pi
  OS uses. If `crontab -l` accepts it but the job fires at the wrong hour, your
  cron doesn't support it — set the system timezone with `timedatectl` instead and
  drop the `CRON_TZ` line.
- With `CRON_TZ=America/Chicago`, the job fires at 10:00 local time year-round, so
  the UTC hour shifts by one at the DST boundaries. That is the intended behaviour.

To test the cron path without waiting for Monday:

```bash
./scripts/run_weekly.sh --dry-run --since 2026-09-15
```

### Overlapping runs

`scripts/run_weekly.sh` and `python -m scout run` both take an exclusive `flock`
on `data/scout.lock` for the duration of the run. A second run that finds the lock
held logs a warning and exits 0 without doing anything — so a job that overruns
into the next week cannot be joined by its successor, and a manual run while cron
is working won't corrupt anything. Because it's `flock` and not a bare PID file,
the kernel releases it even if the process is SIGKILLed or the Pi reboots
mid-run; a stale lock file can't wedge the next run.

---

## CLI

```
python -m scout run [--until STAGE] [--since YYYY-MM-DD] [--dry-run] [--no-email]
                    [--email-preview] [-v]
python -m scout email [PATH] [--send] [--out FILE] [-v]
python -m scout report [--run N] [--all] [--picks] [--keywords] [-v]
```

| Flag | Effect |
|------|--------|
| `--until {ingest,tag,match,readiness,final,verify,digest}` | Stop after that stage. Default `digest`. `ingest` is free and needs no API key. |
| `--since YYYY-MM-DD` | Override the window's lower bound. Widens the window rather than shifting it — the upper bound stays "now". For backfills and testing. |
| `--dry-run` | Fetch + triage only. No final pass, no verification, no email. Implies `--until triage`. |
| `--no-email` | Render the digest but don't send it. |
| `--email-preview` | Also write the HTML email to `digests/<date>.email.html`. |
| `-v` | Debug logging, including the SDK's. |

`report` prints a stored run's counts, token usage, estimated versus projected
cost, and the triage table. With no `--run` it uses the most recent run.

| `report` flag | Shows |
|---|---|
| *(none)* | the shortlist, with each paper's industry and the model's reason |
| `--all` | every triaged paper, not just the shortlist |
| `--picks` | what reached the report, in order, with `?` marking anything the verification pass could not check |
| `--pipelines` | every paper's pipeline tag and readiness score, best first, with `*` marking the shortlist |
| `--keywords` | the noisiest keywords, as candidates to trim from `industries.yaml` |

**Exit codes:** `0` success, `1` run failed, `2` configuration error, `130`
interrupted. A run in which more than `[run].max_failure_rate` of a stage's
papers failed exits `1` — so an expired API key reads as a failure, not as a
quiet week.

---

## Configuration

Behaviour lives in `config.toml`; secrets live in `.env`. The file is commented;
these are the settings worth knowing about.

| Setting | Default | What it does |
|---|---|---|
| `[arxiv] window_days` | `7` | How many days the report covers. |
| `[arxiv] lookback_days` | `7` | Extra days fetched and deduped, so a late-announced paper is not lost. Free — see "Why the window overlaps". |
| `[arxiv] new_submissions_only` | `true` | Drop replacements and revised versions. Near-free and near-no-op (4.5% of the measured corpus), because arXiv's date filter already keys on original submission. |
| `[arxiv] delay_seconds` | `3.0` | Seconds between arXiv API requests. **Don't lower this** — 3s is what arXiv asks for. |
| `[arxiv] request_timeout` | `60.0` | Per-request timeout. The `arxiv` package sets none of its own, so without this a hung connection blocks the run forever — and since the run holds the flock throughout, every later week too. Observed: a fetch sat 25 minutes with no progress and no error after a DNS blip. |
| `[arxiv] max_results` | `6000` | Ceiling per run. The 9-category allowlist is ~955 papers/week, so this is slack, not a limit. |
| `[arxiv] max_attempts` / `backoff_seconds` | `4` / `60.0` | Whole-search retries on a 429 or 5xx from arXiv, doubling each time. The `arxiv` package's own retries wait only `delay_seconds` apart, which a rate limit outlasts. |
| `[prefilter] enabled` | `true` | Master switch for the engagement proxies. `false` keeps every paper but still reports what each proxy would have dropped. |
| `[prefilter] combine` | `any` | `any` is the measured sweet spot. `all` drops a third of the good papers. |
| `[prefilter] journal_ref` | **`false`** | Leave it off. Measured recall 3.3% — see the table above. |
| `[prefilter] experimental_phrases` / `phrases` | `true` | Abstract language indicating a real measurement. |
| `[prefilter] relevant_cross_list` | `true` | Cross-listed into an allowlisted category. Needs no list of its own — it reads `industries.yaml`. |
| `[triage] papers_per_request` | `20` | Abstracts per request. The main cost lever: raising it cuts the per-request prefix cost, at the risk of the model skipping papers (which is detected and retried). |
| `[triage] min_relevance` | `3` | Keep only the top score. Lowering it to 2 widens the net a lot; the run-1 data says those papers were not worth the expensive pass. |
| `[triage] concurrency` | `4` | In-flight triage requests. |
| `[triage] use_prompt_caching` | `false` | Off for measured reasons — see "Why there is no prompt caching". |
| `[final] max_papers` | `60` | Hard ceiling on papers reaching the expensive pass. The shortlist is ordered by keyword-evidence strength first, so when this binds it cuts the weakest evidence rather than the alphabetically unlucky. |
| `[final] effort` | `medium` | Thinking depth. Abstracts are short, so `high` buys little here; adaptive thinking bills as output tokens. |
| `[final] allow_fulltext_fallback` | `true` | Let the model request full text for a paper whose abstract is too vague. Per-paper and logged. |
| `[verify] enabled` | `true` | The independent check over the selected papers. ~$0.19/run. Turning it off saves that and loses the only thing that takes claims away. |
| `[select] max_papers` | `10` | Report length ceiling. One industry each; fewer is a valid week. |
| `[budget] target_usd` / `hard_cap_usd` | `3.50` / `5.00` | Over target logs a warning; over the cap trims the triage input before spending. |
| `[budget] est_*` | measured | Per-token estimates behind the projection. If actual cost drifts from projected, fix these rather than the cap. |
| `[digest] write_json` | `true` | Write the machine-readable sibling next to the Markdown. |
| `[run] max_failure_rate` | `0.5` | A stage that loses more than this fraction of its papers fails the run. |
| `[models] triage` | `claude-haiku-4-5-20251001` | Cheap batched pass. |
| `[models] final` / `verify` | `claude-sonnet-5` | The two expensive passes. A weaker `verify` model is not a check. |
| `[industry] enabled` | `true` | Master switch for the taxonomy. `false` disables triage entirely, since v2's triage needs the industry list. |
| `[pricing]` | Haiku $1/$5, Sonnet $2/$10 per MTok | Verified 2026-09-30. Used to estimate cost in the `runs` table and to drive the budget guard. |

Prompts live in `prompts/triage.md`, `prompts/final.md`, and `prompts/verify.md`,
loaded at runtime. Each has a `## System` and a `## User` section; the user section
is a `str.format` template. `triage.md`'s *system* half is also a template — it
takes `{industry_list}` and `{tier_definitions}` from `industries.yaml`, so the
eligible industries are never hard-coded in a prompt. Edit any of them and rerun —
no code change needed. Use `eval/run_eval.py` to check whether an edit to
`triage.md` actually helped.

### Email

Set `SMTP_*` in `.env` (or as environment variables) and the digest is emailed
after a successful run; leave them unset and email is skipped silently. See
`.env.example` for every variable with its explanation.

**Gmail only, in practice.** Microsoft retired basic authentication, reaching
100% rejection on 30 April 2026, and an app password *is* basic auth — an
Outlook address fails `535 5.7.3 Authentication unsuccessful` no matter what
password you use. There is no password that works; Microsoft requires OAuth2 and
this project does not implement it. For Gmail you need 2-Step Verification on,
then an [App Password](https://myaccount.google.com/apppasswords). Google
displays it as four groups of four and people paste the spaces; a value matching
that exact shape has its spaces stripped for you, so either form works. Past
roughly 100 subscribers, move to Resend or SES — a four-line `.env` change.

Port 465 uses implicit TLS; anything else uses STARTTLS. On macOS, TLS goes
through certifi's CA bundle rather than the system keychain, because python.org
builds do not read the keychain and otherwise every connection dies with
`CERTIFICATE_VERIFY_FAILED` — which looks like a wrong password and is not.

**What gets sent.** A `multipart/alternative` message: the Markdown verbatim as
the plain-text part (the fallback, and what spam filters read) and a styled HTML
part, with the Markdown file attached for the archive. Recipients go in **Bcc**,
never To or Cc, so subscribers never see each other's addresses. Every message
carries `List-Unsubscribe`, which gives the native Unsubscribe button in Gmail
and Apple Mail; set `EMAIL_UNSUBSCRIBE` to a URL for one-click, or to an address
for a "just reply" opt-out.

The subject names the top paper and its industry rather than the date —
`[quant-ph Scout] Gallium refining: Ambient Pressure Growth of ScB2 + 7 more`.
A subject like "Digest #14" tells a reader nothing and trains them not to open
it. `EMAIL_SUBJECT_PREFIX` supplies the filterable tag separately.

#### Checking the rendering without mailing anyone

```bash
# Render the newest digest to digests/<date>.email.html and print the subject.
python -m scout email

# A specific issue, written somewhere else.
python -m scout email digests/2026-10-02.md --out /tmp/preview.html

# Actually deliver it (re-sends an existing issue; nothing is recomputed).
python -m scout email --send
```

`python -m scout run --email-preview` does the same alongside a real run.

## Estimated weekly API cost

Volumes and token sizes below are **measured**; the v2 totals are **projected** from
those measurements, because a v2 run's own numbers replace them as soon as you have
one. Every run records what it actually spent, and `report` prints actual against
projected.

### Where v1's money went, and why v2 is cheaper

Measured, run 1, 14-day window, 998 papers triaged:

| Stage | Model | Calls | Avg in / out | Cost |
|---|---|---|---|---|
| Triage | Haiku 4.5 | 998 | 1,246 / 78 | $1.63 |
| Deep analysis | Sonnet 5 | 25 | 25,858 / 4,457 | $2.41 |
| Critic | Sonnet 5 | 28 | 27,045 / 9,662 | $4.22 |
| Industry scoring | Haiku 4.5 | 11 | 1,693 / 112 | $0.03 |
| **Total** | | | 2.67M / 461K | **$8.29** |

**80% of that bill was 53 calls on 25 papers**, because both Sonnet stages read full
text — ~87k characters, about 22k tokens, per paper, twice. v2's saving is almost
entirely one decision: *the expensive pass reads abstracts*.

### What v2 projects

| Papers triaged | Triage | Final (30) | Verify (10) | **Total** |
|---|---|---|---|---|
| 955 *(measured weekly volume)* | $0.68 | $0.46 | $0.17 | **$1.31** |
| 1,500 | $1.07 | $0.46 | $0.17 | **$1.70** |
| 3,000 | $2.15 | $0.46 | $0.17 | **$2.77** |
| 5,000 | $3.58 | $0.46 | $0.17 | **$4.20** |

Against a $3.50 target and a $5.00 cap. The cap is reached at **~6,120 papers per
run** — roughly 6× the measured volume. These estimates were recalibrated against a
real run's `model_calls`; the later stages are projected **at their caps**, so a
normal week costs less than the table says.

### What a real run actually cost

Run 8, 2026-09-30, 120 new papers after dedupe:

| Stage | Model | Calls | In / out tokens | Cost |
|---|---|---|---|---|
| Triage | Haiku 4.5 | 6 | 56,926 / 5,814 | $0.086 |
| Final pass | Sonnet 5 | 3 | 10,654 / 1,187 | $0.033 |
| Verification | Sonnet 5 | 3 | 8,673 / 3,012 | $0.048 |
| **Total** | | **12** | **76,253 / 10,013** | **$0.167** |

**Twelve model calls for a complete weekly report.** v1 made 1,062 for one. Triage
was 120 papers in 6 requests; the final pass and verification ran on 3 papers each,
and none needed full text. Verification dropped one of the three — a Josephson
junction paper filed under *indium refining* because the device used InAs, which is
not the same thing as bearing on indium as a refined input.

Note the shape: unlike v1, the fixed stages are cheap and **triage volume is the
variable that matters**. That is why the budget guard trims triage input rather
than the later stages, and why the free pre-filter earns its place.

### Scan volume per category

Measured over 7 days (2026-09-15 → 2026-09-22), with each category's unique
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
| **Deduped union** | **955** | |

Excluded as theory-heavy or off-topic, with the volume each would have added:
`cond-mat.stat-mech` 139, `physics.chem-ph` 73, `cond-mat.soft` 57,
`physics.plasm-ph` 48, `cond-mat.dis-nn` 32, `cond-mat.quant-gas` 30,
`cond-mat.other` 23, `physics.acc-ph` 16, `physics.hist-ph` 8.

No category is a volume bomb, so the allowlist was not trimmed for cost. Widening it
is cheap; widening it into `stat-mech` mostly buys theory.

### When the report looks too thin, audit coverage before blaming the filters

The first full run produced 2 papers and looked like over-filtering. It was not —
it was a taxonomy gap, and the audit that proved it is worth repeating.

Take every paper triage could not place at relevance 3 and ask a model to name the
industrial pipeline it bears on, *unconstrained by the taxonomy*. On 117 such
papers that cost $0.18 and found:

- **48% genuinely had no industrial pipeline** — foundational physics, correctly
  rejected. A thin report is partly just honest.
- **52% had a real pipeline with no industry to match**, 27 of them experimental,
  against 3 papers that got through.

**The symptom to look for is shoehorning.** A superconducting-wire paper was being
filed under `tungsten`, a rhenium superconductor under `rare_earth_refining`, a
laser under `led_gan`. Triage scored those **2** — "a real connection, but through
an intermediate step" — which is exactly correct, because the intermediate step
*was the missing industry*. A cluster of relevance-2 papers whose `best_industry`
looks wrong means the taxonomy is too narrow, not that the model is confused.

```bash
# The relevance-2 papers and what they were forced into:
sqlite3 -separator ' | ' data/scout.db \
  "SELECT t.best_industry, substr(p.title,1,70) FROM triage t JOIN papers p USING(arxiv_id)
   WHERE t.run_id = (SELECT MAX(id) FROM runs) AND t.relevance = 2 AND t.is_experimental = 1;"
```

Seven industries were added as a result (`hts_tape`, `lts_wire`,
`superconducting_electronics`, `fiber_lasers`, `graphene_2d`, `sputtering_targets`,
`thermoelectrics`). Measured on the same 120 papers:

| | before (32 eligible) | after (39 eligible) |
|---|---|---|
| relevance 3 | 3 | 13 |
| relevance 0, "no industry fits" | 95 | 71 |
| papers in the report | 2 | 5 |
| run cost | $0.17 | $0.32 |

### The levers, and what they actually buy

Run 1's triage distribution is still the useful guide, with the caveat that **the
scales differ** — v1 scored 1–5, v2 scores 0–3:

| v1 `applied_relevance` | Papers (14 days) | Shortlisted at v1's threshold |
|---|---|---|
| 5 | 19 | 19 |
| 4 | 111 | 104 |
| 3 | 66 | 0 |
| 2 | 179 | 0 |
| 1 | 623 | 0 |

Only **1.9%** of papers scored top marks. If v2's `relevance == 3` behaves like
v1's 5-of-5, expect ~20 papers per 14 days — comfortably under the 30-paper cap,
which means the final pass will usually cost *less* than the $1.00 projected above.
Verify this rather than assuming it; the scales are not the same.

- **`[triage] papers_per_request`** — the biggest remaining triage lever. Going
  from 1 to 20 papers per request cut the prefix cost 20×. Going from 20 to 40 buys
  much less and raises the chance of skipped papers.
- **`[prefilter]` on** — roughly halves triage volume for ~16% of the top-scored
  papers. The default is on; the numbers to judge it by are in the table above.
- **`[final] max_papers`** — a direct cap on the expensive stage, but triage rarely
  produces 30 papers, so this is insurance rather than a live lever.
- **`[verify] enabled = false`** — saves $0.19 and removes the only step that takes
  claims away. Not recommended.
- **Batches API** — would halve the whole bill. Deliberately not used; see "Why the
  synchronous API, not Batches".

### Checking your own numbers

```bash
sqlite3 -box data/scout.db \
  "SELECT id, n_new, n_shortlisted, n_assessed, n_selected, input_tokens,
          output_tokens, round(estimated_cost_usd,4) actual,
          round(projected_cost_usd,2) projected
   FROM runs WHERE stage_reached = 'digest' ORDER BY id DESC LIMIT 5;"

# Where the money actually went, per stage:
sqlite3 -box data/scout.db \
  "SELECT stage, model, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout
   FROM model_calls WHERE run_id = (SELECT MAX(id) FROM runs) GROUP BY stage, model;"

# Did prompt caching do anything? (It is off by default; expect zeros.)
sqlite3 -box data/scout.db \
  "SELECT stage, SUM(cache_read_input_tokens) cache_read,
          SUM(cache_creation_input_tokens) cache_write
   FROM model_calls WHERE run_id = (SELECT MAX(id) FROM runs) GROUP BY stage;"
```

Two traps when querying run history:

- **`MAX(id)` is not the last pipeline run.** `eval/run_eval.py` and
  `--until ingest` also create `runs` rows. Filter on `stage_reached` — the first
  query above does.
- **`triage.applied_relevance` changed scale**, 1–5 in v1 and 0–3 from v2. Query
  `triage.relevance` instead, which only ever holds the 0–3 scale, or filter by
  `run_id`.

## Evaluating the triage stage

Triage decides both what you pay for and what can possibly reach the report, so it is the stage worth measuring.

1. Open `eval/labels.csv` and fill in your own judgements. Three example rows ship
   as a template — real papers, so the harness runs out of the box:

   ```csv
   arxiv_id,should_flag,notes
   2609.35702,yes,"Si-on-SiC platform for vanadium dopants -- experimental device platform."
   2609.35660,no,"Pure theory, no device, no measurement."
   ```

   `should_flag` accepts `yes/no`, `true/false`, or `1/0`. Version suffixes in
   `arxiv_id` are stripped, so `2609.35702v1` is fine. 30–50 rows with a realistic
   mix gives numbers worth acting on; 3 rows only proves the harness works.

2. Run it:

   ```bash
   .venv/bin/python eval/run_eval.py
   ```

It runs the real prompt and model over the labelled papers and reports precision,
recall, F1, and accuracy, then prints every disagreement with the model's reason
next to your note — which is the part you actually learn from. Papers already in
`data/scout.db` are read from there; the rest are fetched from arXiv. Eval runs
never write to `papers` or `triage`, so they can't pollute a digest.

Then edit `prompts/triage.md` or `[triage] min_relevance` and run it again.
At well under a cent per labelled paper once batched, a 50-row eval costs a few
cents.

Because the harness uses the real batched prompt, an eval with more rows than
`[triage] papers_per_request` also tests that batching does not change verdicts —
worth knowing, since batching is the one thing in v2 that could quietly make triage
worse. If precision drops when you add rows, suspect the batch size before the
prompt.

---

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

247 tests, no network and no API calls. They cover the date-window and dedupe
logic (`test_window.py`, `test_dedupe.py`), schema validation including the strict
JSON schema actually sent to the API (`test_schemas.py`), retry/backoff and the
model-call log against a stubbed SDK (`test_llm.py`), HTML/PDF extraction and
truncation (`test_fulltext.py`), taxonomy loading and every validation failure
(`test_industries.py`), the keyword pre-filter against the real shipped YAML
(`test_matcher.py`), and — in `test_pipeline.py` — batched triage, selection,
digest rendering, the pre-filter, the budget guard, the arXiv backoff and the run
lock.

Four of those deserve specific mention, because they pin behaviour that fails
silently if it breaks:

- **`relevance` and `disruption` are enforced enums.** `Field(ge=, le=)` is *not*
  enforced by the API — the SDK moves the bound into the field description, so an
  out-of-range value comes back with no error anywhere. `test_schemas.py` asserts
  both fields derive a real JSON-schema `enum`, and separately that no schema has
  smuggled a numeric bound into a description.
- **Batched triage matches results by echoed `ref`, not by position.** A reordered
  or short response must not hand paper 1's verdict to paper 3. Both cases are
  tested, including that a skipped paper is retried individually.
- **No industry appears twice.** Tested in `select.py`, and enforced again by a
  `UNIQUE (run_id, industry_id)` constraint on `selections` — so if a refactor ever
  breaks the greedy loop, the database raises instead of quietly shipping a report
  with one industry twice.
- **Tier 3 never reaches a prompt.** Tier-3 industries stay in `industries.yaml` but
  are not eligible matches, and a test asserts none of their ids appear in the
  rendered industry list.

---

## Layout

```
config.toml            behaviour: models, thresholds, window, concurrency, pricing
industries.yaml        the industry taxonomy: categories, keyword sets, groups, 100 industries
.env                   secrets: ANTHROPIC_API_KEY, SMTP_* (gitignored)
.env.example           the same keys with placeholders (committed)
.githooks/pre-commit   blocks commits carrying a credential
.github/workflows/     the Monday retry ladder + Tuesday catch-up
.env.example           committed template
prompts/               triage.md, final.md, verify.md -- loaded at runtime
scout/
  cli.py               argparse entry point; python -m scout
  pipeline.py          stage orchestration, --until / --dry-run, failure-rate guard
  ingest.py            date window, arXiv query, version stripping, dedupe, revisions
  prefilter.py         free engagement proxies, with per-proxy drop accounting
  budget.py            cost projection and the trim-to-fit guard
  triage.py            batched cheap pass (~20 abstracts/request)
  final.py             the expensive pass: abstracts, with on-demand full text
  select.py            greedy no-duplicate-industry selection (pure, no I/O)
  verify.py            independent check of the selected papers
  industries.py        industries.yaml loader and validation
  matcher.py           keyword pre-filter: normalisation, tier and group rules
  digest.py            Markdown report + machine-readable JSON sibling
  fulltext.py          arXiv HTML with PDF fallback
  llm.py               the only code that calls the API: retries, logging, usage
  schemas.py           every Pydantic schema
  db.py                SQLite schema and queries
  config.py            config.toml + .env loading
  prompts.py           prompt file loading and System/User splitting
  lock.py              flock-based run lock
  mailer.py            Markdown -> HTML email, and SMTP delivery
  logging_setup.py     rotating file log + stderr
eval/                  labels.csv, run_eval.py
scripts/               run_weekly.sh, crontab.txt, alarm_email.py
tests/
data/scout.db          SQLite database (gitignored)
digests/YYYY-MM-DD.md    the report
digests/YYYY-MM-DD.json  the same picks, machine-readable, for tracking trends
logs/scout.log         rotating, 5 MB x 5
logs/cron.log          appended by run_weekly.sh
```

### Database

Schema changes are migrated in on open: `connect()` adds any column listed in
`db.MIGRATIONS` that an existing database lacks, because `CREATE TABLE IF NOT
EXISTS` does nothing to a table that already exists. Without that, a Pi running
for weeks would break on the first query naming a new column. The list is
append-only — add entries, never remove or retype them.

`papers` is the **seen-papers store**: one row per arXiv ID ever seen, and
membership is what dedupe checks — which is what makes the lookback window free.
Then `triage`, `assessments` (the final pass, stored for every paper that got the
expensive call, selected or not), `verifications`, `selections` (what reached the
report, in order), `runs` (start/end, status, counts, tokens, projected and actual
cost), and `model_calls` (every request and response, including failed attempts,
with tokens, latency, and cache-token columns). The taxonomy layer adds
`industry_matches` and `keyword_hits` (per-keyword counts for noise triage). WAL
mode, so `report` can read while a run writes.

`analyses`, `critiques` and `industry_links` are **v1 tables**. Their DDL is kept so
an existing database still opens and run 1's history stays queryable; v2 never
writes to them.

A secondary-category paper that fails the `quantum_core` gate is still recorded,
with `gate_passed = 0`, so dedupe never pays to look at it again. If you widen the
gate and want those papers reconsidered:

```bash
sqlite3 data/scout.db "DELETE FROM papers WHERE gate_passed = 0;"
```

To re-triage papers you've already seen — after editing `prompts/triage.md`, say —
delete them from `papers`; dedupe will then treat them as new. Note this is also
the thing to remember when testing: a second run over the same window will find
almost nothing new, which looks like a quiet week rather than a working pipeline.

```bash
sqlite3 data/scout.db "DELETE FROM papers WHERE first_seen_run = 3;"
```

---

## Scheduled runs: GitHub Actions

`.github/workflows/weekly.yml` runs the digest every **Monday at 10:00
America/Chicago** and emails it. This is the deployment that is actually in use;
the Raspberry Pi instructions above remain valid for a local alternative, but
**never run both** — subscribers would get two copies.

### Secrets

Under **Settings → Secrets and variables → Actions**:

| Secret | Required | What |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Billing for the run. |
| `SMTP_USER` | for email | The sending Gmail address. |
| `SMTP_PASSWORD` | for email | Its 16-character App Password. |
| `SMTP_TO` | for email | Comma-separated recipients. |
| `SMTP_FROM` | no | Defaults to `SMTP_USER`. |
| `EMAIL_UNSUBSCRIBE` | no | URL or address for the opt-out. |

`EMAIL_LIST_ID` is **not** a secret and lives in the workflow's `env:` block
(`quantph-scout.wjenson312.github.io`). `scout/config.py` reads it to emit the
RFC 2919 `List-Id` header; leaving it unset silently drops the header, which
was the state of things until it was wired up — the deliverability fix that
added the header had never actually shipped in CI.

**The recipient list is a secret, not configuration** — those are other people's
addresses. Everything non-sensitive (host, port, sender name, subject prefix)
lives in the workflow file where it is readable.

No `.env` is needed. `load_dotenv(..., override=False)` means a real environment
variable always wins and a missing `.env` is a no-op.

### Seven cron lines, on purpose

GitHub cron is best-effort. It fires late by 10–60+ minutes as a matter of
course, and under load it drops firings entirely — no retry, no notification,
no run in the Actions tab at all. One cron line a week therefore means one
chance a week, and a dropped Monday is a seven-day gap.

So the schedule is a **retry ladder**: six lines spread across Monday in
`America/Chicago`, plus a Tuesday catch-up.

| Cron (UTC) | CDT (summer) | CST (winter) | Role |
|---|---|---|---|
| `0 15 * * 1` | Mon 10:00 | Mon 09:00 | the target, summer |
| `0 16 * * 1` | Mon 11:00 | Mon 10:00 | the target, winter |
| `0 18 * * 1` | Mon 13:00 | Mon 12:00 | retry |
| `0 21 * * 1` | Mon 16:00 | Mon 15:00 | retry |
| `0 1  * * 2` | Mon 20:00 | Mon 19:00 | retry |
| `0 4  * * 2` | Mon 23:00 | Mon 22:00 | retry |
| `0 12 * * 2` | Tue 07:00 | Tue 06:00 | catch-up |

The last two Monday rungs are Tuesday in UTC and still Monday in Chicago; that
is the point of deciding in local time rather than UTC.

`schedule-gate` lets exactly one rung through by asking two questions:

1. Is it Monday in `$TARGET_TZ`?
2. Does `state/last_sent` already hold today's local date?

It does **not** compare the clock against the cron that fired. That is what
makes a late delivery still send: under the old "exit unless it is exactly
10am" gate, a run delivered at 11:05 concluded it was the wrong side of
daylight saving and exited green, producing a silent week. It is also why no
DST arithmetic appears anywhere any more — local date and local weekday are
already correct in both halves of the year, so the two-cron dance that existed
only to hit 10:00 exactly is gone.

`TARGET_HOUR` survives as a *soft* preference: a Monday rung that lands before
10:00 local stands down, which in practice is only the 15:00 UTC rung in
winter, covered by 16:00 UTC an hour later. Later rungs never check the hour,
so this cannot cause a silent week.

The **Tuesday catch-up** fires only when `state/last_sent` is not Monday's
date — i.e. when every Monday rung failed or was dropped. A bad Monday then
costs you a late digest rather than a missing one.

To change the time, edit `TARGET_TZ`/`TARGET_HOUR` and the two target cron
lines. The retry rungs only need to stay inside Monday-local.

### The send marker, and why a duplicate is the better failure

`state/last_sent` holds one line: the local date of the last real send. It is
written only after a successful `send` (never by `preview`, and deliberately
never by `resend`, so a deliverability test on a Monday cannot suppress that
day's real digest), and it is committed and pushed with the digest.

Two consequences worth being explicit about:

- Workflow-level `concurrency` serialises the ladder, so a queued rung's gate
  does not run until the rung in front of it has finished and pushed. Two rungs
  cannot both decide to send.
- If the email goes out but the marker fails to push, a later rung sends a
  **duplicate**. The push retries three times, but with retries you must choose
  which way to fail, and a rare duplicate beats a silent week. If you would
  rather have the opposite trade, drop the retry rungs.

`send_digest()` returns `False` without raising when mail is unconfigured, so a
`send` run can otherwise succeed having mailed nobody. The marker step fails
loudly on an empty `SMTP_TO` rather than recording a send that never happened.

### The silence alarm

Every interesting failure — pipeline crash, revoked Gmail app password,
exhausted API budget, GitHub outage mid-run — looks identical from the inbox:
nothing arrives. A red run in a tab nobody opens is not a notification.

So a scheduled run that fails mails `scripts/alarm_email.py`: plain text, no
model calls, no digest rendering, naming the cron that fired, the run URL, what
the ladder will try next, and the `gh workflow run` line to send by hand. It
shares `load_config()` with the pipeline so the SMTP settings cannot drift.

### How state survives an ephemeral runner

Split by kind, because the two halves have opposite requirements:

- **`data/scout.db` rides as an artifact** (90-day retention), downloaded from
  the last successful run at the start of the next. It is 22MB of abstracts and
  model-call logs growing ~5MB a week; committing it weekly would put a quarter
  of a gigabyte of binary into git history within a year. Without it the dedupe
  table starts empty and the run re-features papers that already went out.
- **The digest, `state/last_sent`, `state/proposals.yaml` and `pipelines.yaml` are
  committed back**,
  because those are small, text, and meant to be read in the repo. That commit
  also resets GitHub's 60-day inactivity timer, which would otherwise disable
  this schedule after two quiet months.

`state/feedback.yaml` is only ever read by a run, never written, so your ratings
are never clobbered.

### Testing order that works

`workflow_dispatch` exposes three modes. Use them in this order:

| Mode | Proves | Cost |
|---|---|---|
| `collect` | The runner reaches arXiv and the database restored | $0 |
| `dry-run` | Tagging and readiness scoring work end to end | ~$0.60 |
| `preview` | The whole pipeline; uploads the rendered email, sends nothing | ~$1.30 |
| `send` | The full path including SMTP | ~$1.30 |
| `seed` | — (not a test; see below) | $0 |
| `resend` | Re-delivers the newest digest. SMTP only, no pipeline | $0 |

Start with `collect`: it needs no secrets at all, so it is also the fastest way
to confirm the workflow file itself is valid.

Both `collect` and `seed` accept a `since` date, as does every other mode.

### If the digest lands in Junk

Deliverability is mostly about what the message does *not* claim about itself.
The first version set three things that are self-demoting:

| Removed | Why it hurt |
|---|---|
| `Precedence: bulk` | Legacy "this is bulk mail" marker. Google's sender guidance advises against it, and it buys nothing for a hand-managed list. |
| `Auto-Submitted: auto-generated` | Same category: marks the mail as machine-generated. Its real job is suppressing vacation replies, which is not worth the cost. |
| `text/markdown` attachment | An unrecognised attachment subtype on automated mail. The digest is already archived in the repo and as an artifact, so it was redundant. |

Also fixed: **a single recipient is now addressed in `To:` directly** rather
than `To: yourself, Bcc: them`. That shape is a bulk blast, and when the list is
just you it makes `From` and `To` identical — which is what a spoofed phishing
message looks like. Several recipients still go in Bcc; that privacy guarantee
is unchanged and tested.

Kept, because they are *positive* signals: `List-Unsubscribe` (what renders the
native Unsubscribe button), and `List-Id` if you set `EMAIL_LIST_ID`.

Test changes with `resend` — it re-delivers the newest digest over SMTP with no
pipeline run, so iterating costs nothing:

```bash
gh workflow run "Weekly Digest" -f mode=resend
```

If it still lands in Junk, the remaining lever is the sending identity. Mail
from a personal Gmail address to yourself has no domain reputation behind it;
marking it "Not junk" once usually settles it permanently. For a real
subscriber list, move to Resend or SES with your own domain and set up
SPF, DKIM and DMARC — that, not headers, is what actually earns inbox placement.

### Ingest marks papers as seen, even if you never look at them

This is the sharpest edge in the whole system and it has already bitten once.

De-duplication asks exactly one question: *is this arXiv id already in the
`papers` table?* Nothing else. So any run that reaches the ingest stage records
its whole window as seen — **including a run that stops before screening
anything.** Those papers are then skipped forever and can never be featured.

Two `collect` runs did this during setup: they ingested 2,224 papers to prove
the artifact round-trip worked, and the next real run correctly found zero new
papers and emailed an empty digest.

Two guards now exist:

- **`collect` does not upload its database.** The free connectivity check is
  side-effect free, so it can be run as often as you like.
- **`seed` exists for when you actually want this.** It ingests a window and
  keeps the database *without* screening it — the right tool when history lives
  somewhere else and you need this database to know about it. It is free, and it
  permanently excludes everything in the window, so only give it a range you
  have already judged elsewhere.

`seed` was needed once already: local runs and Actions runs keep *separate*
databases, so after moving to Actions the papers screened on the laptop were
unknown to the cloud. Three papers from the 2026-10-02 issue fell back inside
the first scheduled run's 14-day fetch window and would have been sent twice.

```bash
# "Everything up to this date has already been judged elsewhere."
gh workflow run "Weekly Digest" -f mode=seed -f since=2026-09-20
```

### The fetch window is 14 days, and there is no separate report window

`window_days` (7) + `lookback_days` (7). Everything fetched in those 14 days that
is not already in the database is screened and can be featured. The lookback is
not a "don't report these" zone — it works *only* because the earlier week is
already in the database. **An empty database plus a 14-day window re-features two
weeks of papers.** This is why losing the artifact is not harmless, and why the
recovery is `seed` followed by a normal run, not just a normal run.

`gh workflow run` **returns immediately** — a clean terminal means *queued*, not
*succeeded*. To actually wait:

```bash
gh run watch $(gh run list --workflow="Weekly Digest" --limit 1 \
  --json databaseId --jq '.[0].databaseId') --exit-status
```

### Things that will bite later

- **Scheduled runs are not punctual.** 10–30 minutes late is routine, and under
  load a run can be dropped entirely. Fine for a weekly digest.
- **Rotating the Gmail app password** means updating the GitHub secret too; the
  local `.env` and the secret are independent copies.
- **Pushing `.github/workflows/` needs the `workflow` OAuth scope.** If a push
  is rejected with *"refusing to allow an OAuth App to create or update
  workflow"*, run `gh auth refresh -h github.com -s workflow`.

### The commit hook

`.githooks/pre-commit` blocks any commit containing an API-key-shaped string, or
a tracked `.env`-style file with a filled-in value — `.gitignore` cannot protect
you in `.env.example`, which is the one env file that is *meant* to be
committed, and is the file you edit with the real values in front of you.

Active via `git config core.hooksPath .githooks`. **Hooks do not travel with a
clone, so a fresh checkout must run that command again.**

---

## Troubleshooting

**`ANTHROPIC_API_KEY is not set`** — `.env` is missing or in the wrong directory.
It must sit next to `config.toml`. Config is located relative to the `scout`
package, not the working directory, so this is not a cron `cwd` problem.

**Spend shows up on the wrong API key.** Every run records a fingerprint of the key
it used — a SHA-256 prefix, never the key itself — so attribution is always
answerable after the fact:

```bash
sqlite3 -box data/scout.db \
  "SELECT id, started_utc, api_key_source, api_key_fingerprint,
          round(estimated_cost_usd,4) cost FROM runs ORDER BY id DESC LIMIT 10;"
```

`api_key_source` is the thing to check. `.env` is what you want. **`environment`
means an exported `ANTHROPIC_API_KEY` overrode your `.env` file** — `.env` is loaded
with `override=False`, so a real environment variable always wins. That is
deliberate (it lets you test a one-off key on the command line) but it is easy to
forget, so the run also logs a warning when it happens. Check with:

```bash
env | grep -c ANTHROPIC_API_KEY   # 0 is what you want; 1 means .env is being ignored
```

To match the fingerprint against a row in the Anthropic console, compare the last
four characters of your key — this prints only those four, never the key:

```bash
grep '^ANTHROPIC_API_KEY' .env | tail -c 5
```

If the key is right and the spend still isn't itemised against it, the usual cause
is reporting lag: the console's org-level total updates well before the per-key
breakdown, and cost is attributed per workspace, so check you are looking at the
workspace the key belongs to.

**Cron didn't run.** Check `logs/cron.log` first — if it's empty, cron never
started the script. `grep CRON /var/log/syslog` shows what cron thinks it did.
Cron runs with a minimal `PATH` and no profile, which is why `run_weekly.sh`
resolves the project directory from its own location and activates the venv
explicitly.

**A run exited 1 with `StageFailure`.** More than half the papers in a stage
failed. The reason is in `logs/scout.log` and in `model_calls.error`:

```bash
sqlite3 data/scout.db \
  "SELECT stage, error, COUNT(*) FROM model_calls
   WHERE run_id = (SELECT MAX(id) FROM runs) AND error IS NOT NULL
   GROUP BY stage, error;"
```

**The report is shorter than I expected, or empty.** Work down the free stages
before suspecting the models, because each one logs what it dropped:

```bash
.venv/bin/python -m scout report      # counts for every stage, in order
```

Read `gated out`, `prefiltered out` and `budget trimmed` first. A large
`prefiltered out` means `[prefilter]` is too aggressive for your taxonomy — set
`enabled = false` and rerun `--until ingest` (free) to see the difference. A nonzero
`budget trimmed` means the scan was not exhaustive and the report says so on the
page. If all of those are small and `shortlisted` is still 0, the threshold is doing
it: `[triage] min_relevance = 3` keeps only top-scored papers by design, and an
empty week is a legitimate result.

**A paper appears marked "unverified".** The verification call failed for it, so its
text was never checked — or it was promoted into the report after verification had
already run, which happens when a verified paper is dropped or downgraded and
something below it moves up. Either way the marker is honest; `grep <arxiv_id>
logs/scout.log` says which.

**Verification dropped a paper I wanted.** Its full reasoning is stored:

```bash
sqlite3 data/scout.db \
  "SELECT arxiv_id, verdict, n_unsupported, payload FROM verifications
   WHERE run_id = (SELECT MAX(id) FROM runs) AND verdict = 'drop';"
```

If the check is wrong rather than the paper, tighten `prompts/verify.md` — it
already says not to invent problems and that absence from an abstract is not
disproof, so if it is still over-dropping, that guidance needs strengthening rather
than the pass needs disabling.

**A paper pulled full text unexpectedly.** The final pass set `abstract_too_vague`.
That is per-paper and logged; `assessments.used_fulltext` records it. If it is
happening often, the cost projection in `[budget]` will drift low — check the rate
before raising the cap.

**`industries.yaml is invalid (N problems)`.** The loader runs before any network
call or spend and lists every problem at once — duplicate ids, a bad `tier`, an
undefined `group`, an unresolvable `keyword_sets` reference. Fix them all and rerun;
nothing was spent.

**An industry is matching far too much.** Check its keywords against the hit
counts (`keyword_hits`, query in the taxonomy section above). The usual cause is a
short keyword that collides with ordinary English once lowercased — add it to
`case_sensitive`, or a grammatical phrase — add that to `exclusions`. If the
keyword is simply too generic, delete it from the YAML; the group-level
`keyword_sets` usually still catch the real papers.

**Two papers both belong to the same industry.** Only one can appear — that is the
rule, not a bug. The loser is recorded as crowded out in the log, and the next-best
industry-distinct paper takes the remaining slot. If you would rather see both,
raise `[select] max_papers` and relax the rule in `select.py` deliberately; the
`UNIQUE (run_id, industry_id)` constraint on `selections` will need to go too.

**A paper is filed under an obviously wrong industry.** Triage picks it and the
final pass gets a chance to correct it, so a wrong industry that survives both is
usually a keyword problem rather than a model problem — check
`report --keywords` for the term that put that industry in front of the model.

**arXiv returns HTTP 429.** Rate-limited. The run now backs off 60s/120s/240s and
retries, so a transient limit no longer kills the digest. If it persists, you have
another process querying arXiv — check that a manual run isn't racing cron (the
lock prevents two *scout* runs, not a stray script).

**arXiv returns 0 papers for today.** Expected — arXiv announces on a delay, so
the last day or two of a window is usually empty. This is the reason the window
overlaps.

**arXiv returns HTTP 500 for a query you built by hand.** Check `max_results`: the
API rejects `max_results=0` with a 500 rather than a 4xx, which reads like an outage
or a rate limit and is neither. Use `max_results=1` and read
`opensearch:totalResults` if you only want a count.

**A second run over the same window finds nothing.** Dedupe is working: `papers`
holds every ID ever seen. To re-run a window for real, delete those rows first (see
the Database section).
