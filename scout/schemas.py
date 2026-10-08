"""All Pydantic schemas for quantph-scout.

These double as the JSON schemas sent to the API: `client.messages.parse(
output_format=Model)` derives a strict JSON schema from the model, so the shape
here is the contract the model is held to.

Two notes on how that derivation behaves, because it shapes the choices below:

* `extra="forbid"` becomes `additionalProperties: false`.
* Numeric `ge`/`le` bounds are NOT enforced by the API -- they get appended to
  the field description as a hint. `Literal` ints become a real JSON-schema
  `enum`, which is enforced. So every bounded integer here uses `Literal`, not
  `Field(ge=)`. `tests/test_schemas.py` pins this, because getting it wrong
  fails silently: the model returns a 7 for a 1-5 field and nothing objects.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Enforced enums, not ge/le bounds. See the module docstring.
Relevance = Literal[0, 1, 2, 3]
Disruption = Literal[1, 2, 3, 4, 5]


class Strict(BaseModel):
    """Base model that rejects unknown fields, both locally and in the schema."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Stage: triage (cheap, batched -- many papers per request)
# --------------------------------------------------------------------------


class TriageItem(Strict):
    """One paper's verdict inside a batched triage response."""

    ref: int = Field(
        description=(
            "The paper's number exactly as labelled in the input list. Return one "
            "entry per paper given, using that paper's number. Do not renumber."
        )
    )
    best_industry: str = Field(
        description=(
            "The single best-fitting industry id, copied exactly from the eligible "
            "industry list. Exactly one. If no industry in the list fits, return "
            "the empty string and score relevance 0 -- do not invent an id and do "
            "not return one that was not offered."
        )
    )
    relevance: Relevance = Field(
        description=(
            "How relevant this paper is to that industry. "
            "0 = no real connection. "
            "1 = tenuous or generic. "
            "2 = a specific connection, but several steps from anything actionable. "
            "3 = the paper's central measured result is directly about an input to, "
            "or a measurement of, this industry. "
            "Only 3 survives, so reserve it. Most papers are 0 or 1."
        )
    )
    experimental: bool = Field(
        description=(
            "True only if the work was done on physical hardware, samples, or "
            "devices AND at least one quantity was actually measured. Proposals, "
            "models, simulations, and numerical studies are false, however useful."
        )
    )
    reason: str = Field(
        description="At most 15 words naming the concrete evidence. No line breaks."
    )


class TriageBatch(Strict):
    """The whole response for one batched triage request."""

    results: list[TriageItem] = Field(
        description="Exactly one entry per paper in the input, in the order given."
    )


# --------------------------------------------------------------------------
# Stage: final pass (stronger model, shortlist only, abstracts only)
# --------------------------------------------------------------------------


class FinalAssessment(Strict):
    best_industry: str = Field(
        description=(
            "The confirmed single best industry id, copied exactly from the eligible "
            "list. Usually triage's choice; change it if triage got it wrong."
        )
    )
    disruption: Disruption = Field(
        description=(
            "How much this could change that industry IF it scales. "
            "1 = a marginal improvement on current practice. "
            "3 = a materially better way to do something the industry already does. "
            "5 = it would displace the incumbent technology outright. "
            "Score the ceiling if it scales, not the probability that it will -- "
            "'the catch' is where the odds belong."
        )
    )
    abstract_too_vague: bool = Field(
        description=(
            "True only if the abstract does not say enough to explain what was found. "
            "Setting this true costs a full-text fetch, so use it sparingly."
        )
    )
    what_they_found: str = Field(
        description=(
            "2-3 short sentences, written for a bright twelve-year-old: plain words, "
            "no jargon, one everyday comparison if it helps. State the actual number "
            "that was measured if there is one."
        )
    )
    why_it_matters: str = Field(
        description=(
            "1-2 short sentences on why this matters for that specific industry. "
            "Name the thing it would change. Same plain language."
        )
    )
    the_catch: str = Field(
        description=(
            "One sentence on the gap between this lab result and real-world use: "
            "scale, cost, temperature, durability, purity, speed. Be concrete and "
            "name the limit. There is always a catch; find it."
        )
    )


# --------------------------------------------------------------------------
# Stage: verify (independent check over the SELECTED papers only)
#
# v2's replacement for v1's critic. Scoped to the final selection rather than
# the whole shortlist, which is what makes an independent check affordable.
# --------------------------------------------------------------------------


class NumberCheck(Strict):
    claim: str = Field(description="The number or quantitative claim being checked.")
    supported: bool = Field(
        description=(
            "True only if the supplied source text actually states this. A number "
            "that is close but not what the source says is NOT supported."
        )
    )
    note: str = Field(
        description="If unsupported, what the source actually says, or that it is absent."
    )


class Verification(Strict):
    number_checks: list[NumberCheck] = Field(
        description=(
            "One entry per quantitative claim in the report text. An empty list is "
            "valid only if the text contains no numbers at all."
        )
    )
    industry_fits: bool = Field(
        description=(
            "False if the paper's result does not really bear on the industry it was "
            "filed under."
        )
    )
    overstated: list[str] = Field(
        description=(
            "Phrases in the report text that claim more than the source supports. "
            "Empty list if there are none -- do not invent problems."
        )
    )
    corrected_what_they_found: str = Field(
        description=(
            "The 'what they found' text, corrected. Return it unchanged if nothing "
            "was wrong. Same plain language, same length."
        )
    )
    corrected_why_it_matters: str = Field(description="Likewise, corrected or unchanged.")
    corrected_the_catch: str = Field(
        description=(
            "Likewise. If the source reveals a harder limit than the original named, "
            "say that one instead."
        )
    )
    adjusted_disruption: Disruption = Field(
        description=(
            "The disruption score after accounting for anything found above. Lower it "
            "if claims did not hold up. This is what the report is ordered by."
        )
    )
    verdict: Literal["ok", "corrected", "drop"] = Field(
        description=(
            "'ok' = nothing wrong. 'corrected' = text was fixed but the paper stands. "
            "'drop' = the central claim is unsupported or the industry does not fit, "
            "and the paper should not appear in the report at all."
        )
    )


# --------------------------------------------------------------------------
# Stage A: pipeline tagging (cheap, batched, every paper)
#
# A pipeline is the mechanism by which a result could become a product. Tagging
# happens before any industry is chosen, which narrows 78 industries to a handful.
# --------------------------------------------------------------------------

Confidence = Literal["high", "medium", "low"]


class PipelineTag(Strict):
    ref: int = Field(
        description=(
            "The paper's number exactly as labelled in the input list. One entry "
            "per paper given, using that paper's number. Do not renumber."
        )
    )
    primary_pipeline: str = Field(
        description=(
            "The single best-fitting pipeline id, copied exactly from the list. Use "
            "'none' for purely foundational work with no physical handle for "
            "application. Never invent an id."
        )
    )
    secondary_pipeline: str = Field(
        description=(
            "A second pipeline id if the result plausibly fits two, otherwise the "
            "empty string. Do not repeat the primary."
        )
    )
    confidence: Confidence = Field(
        description=(
            "Confidence in the primary tag. If you are torn between 'none' and a "
            "real pipeline, choose the real pipeline with 'low' -- the next stage "
            "filters on evidence, so a low-confidence guess is cheap and a wrongly "
            "dropped paper is gone for good."
        )
    )
    signal: str = Field(
        description=(
            "One short phrase naming the specific result that justified the tag. "
            "Name the measured thing, not the field. No line breaks."
        )
    )


class PipelineTagBatch(Strict):
    results: list[PipelineTag] = Field(
        description="Exactly one entry per paper in the input, in the order given."
    )


# --------------------------------------------------------------------------
# Stage B: readiness scoring (cheap, batched, every surviving paper)
# --------------------------------------------------------------------------

CheckScore = Literal[0, 1, 2]


class ReadinessItem(Strict):
    check_id: str = Field(
        description="The readiness check id, copied exactly from the list given."
    )
    score: CheckScore = Field(
        description=(
            "0 = major barrier. 1 = partial, unclear, or not stated by the paper. "
            "2 = favorable. Score only what the paper states."
        )
    )
    note: str = Field(
        description=(
            "At most 12 words of justification. Write exactly 'not stated' when the "
            "paper does not address this check. No line breaks."
        )
    )


class ReadinessScore(Strict):
    ref: int = Field(description="The paper's number as labelled in the input.")
    checks: list[ReadinessItem] = Field(
        description="One entry per readiness check given, in the order listed."
    )
    most_limiting: str = Field(
        description=(
            "The check_id of the biggest barrier -- the lowest-scoring check, and "
            "if several tie, the one that most blocks real use. This is what the "
            "report's lab-to-practice gap is built from."
        )
    )
    concreteness: CheckScore = Field(
        description=(
            "How concrete the paper's headline measurement is. 2 = a specific "
            "number with units. 1 = a qualitative or relative claim. 0 = no "
            "measurement stated. Used only to break ties in ranking."
        )
    )


class ReadinessBatch(Strict):
    results: list[ReadinessScore] = Field(
        description="Exactly one entry per paper in the input, in the order given."
    )
