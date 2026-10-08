"""Schema validation, including the strict-schema derivation sent to the API."""

import pytest
from anthropic.lib._parse._transform import transform_schema
from pydantic import ValidationError

from scout.schemas import (
    FinalAssessment,
    NumberCheck,
    TriageBatch,
    TriageItem,
    Verification,
)
from scout.match import is_kept

# --------------------------------------------------------------------------
# TriageItem / TriageBatch
# --------------------------------------------------------------------------

VALID_ITEM = {
    "ref": 1,
    "best_industry": "gallium",
    "relevance": 3,
    "experimental": True,
    "reason": "Measured single-photon rate in a fabricated GaAs dot.",
}


def test_valid_triage_item_parses():
    item = TriageItem.model_validate(VALID_ITEM)
    assert item.relevance == 3
    assert item.experimental is True
    assert item.ref == 1


def test_triage_item_parses_from_json():
    item = TriageItem.model_validate_json(
        '{"ref": 4, "best_industry": "", "relevance": 0,'
        ' "experimental": false, "reason": "Pure theory."}'
    )
    assert item.relevance == 0
    assert item.best_industry == ""


@pytest.mark.parametrize("relevance", [4, 5, -1, 10])
def test_relevance_outside_0_to_3_is_rejected(relevance):
    """v2 moved to a 0-3 scale; a v1-style 5 must not slip through."""
    with pytest.raises(ValidationError):
        TriageItem.model_validate({**VALID_ITEM, "relevance": relevance})


def test_relevance_must_be_an_integer_not_a_string():
    with pytest.raises(ValidationError):
        TriageItem.model_validate({**VALID_ITEM, "relevance": "high"})


@pytest.mark.parametrize("field", list(VALID_ITEM))
def test_every_triage_field_is_required(field):
    payload = {k: v for k, v in VALID_ITEM.items() if k != field}
    with pytest.raises(ValidationError):
        TriageItem.model_validate(payload)


def test_unknown_triage_field_is_rejected():
    with pytest.raises(ValidationError):
        TriageItem.model_validate({**VALID_ITEM, "confidence": "high"})


def test_batch_holds_many_items():
    batch = TriageBatch.model_validate(
        {"results": [VALID_ITEM, {**VALID_ITEM, "ref": 2, "relevance": 0}]}
    )
    assert [i.ref for i in batch.results] == [1, 2]


def test_empty_batch_is_valid():
    """A batch of zero results is a parse success and a pipeline problem.

    It must not be a ValidationError, because triage.py handles a short response
    by retrying the missing papers individually -- that path needs the object.
    """
    assert TriageBatch.model_validate({"results": []}).results == []


# --------------------------------------------------------------------------
# The industry-fit rule (was the shortlist rule before the pipeline layer)
# --------------------------------------------------------------------------


def _item(**kwargs):
    return TriageItem.model_validate({**VALID_ITEM, **kwargs})


def test_a_real_industry_fit_is_kept():
    assert is_kept(_item(relevance=3), 2)
    assert is_kept(_item(relevance=2), 2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"relevance": 1},        # too weak: dropped rather than forced
        {"relevance": 0},
        {"best_industry": ""},   # no industry means nothing to report on
    ],
)
def test_a_weak_fit_is_dropped_not_forced(kwargs):
    """The addendum is explicit: if the no-repeat rule would force a paper into a
    weak match, drop it. Readiness has already decided the paper is worth
    reporting, so this stage only judges whether the industry fit is real."""
    assert not is_kept(_item(**kwargs), 2)


# --------------------------------------------------------------------------
# FinalAssessment
# --------------------------------------------------------------------------

VALID_FINAL = {
    "best_industry": "gallium",
    "disruption": 4,
    "abstract_too_vague": False,
    "what_they_found": "They made a light source that emits one photon at a time.",
    "why_it_matters": "Gallium refiners supply the wafers these are grown on.",
    "the_catch": "It only works at 4 kelvin, colder than deep space.",
}


def test_valid_final_assessment_parses():
    assessment = FinalAssessment.model_validate(VALID_FINAL)
    assert assessment.disruption == 4


@pytest.mark.parametrize("disruption", [0, 6, -1])
def test_disruption_outside_1_to_5_is_rejected(disruption):
    with pytest.raises(ValidationError):
        FinalAssessment.model_validate({**VALID_FINAL, "disruption": disruption})


@pytest.mark.parametrize("field", list(VALID_FINAL))
def test_every_final_field_is_required(field):
    payload = {k: v for k, v in VALID_FINAL.items() if k != field}
    with pytest.raises(ValidationError):
        FinalAssessment.model_validate(payload)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

VALID_VERIFICATION = {
    "number_checks": [
        {"claim": "4 kelvin", "supported": True, "note": ""},
        {"claim": "99% purity", "supported": False, "note": "Abstract says 95%."},
    ],
    "industry_fits": True,
    "overstated": ["described a single measurement as a capability"],
    "corrected_what_they_found": "They made a light source that emits one photon.",
    "corrected_why_it_matters": "Gallium refiners supply the wafers.",
    "corrected_the_catch": "It only works at 4 kelvin.",
    "adjusted_disruption": 3,
    "verdict": "corrected",
}


def test_valid_verification_parses():
    verification = Verification.model_validate(VALID_VERIFICATION)
    assert verification.verdict == "corrected"
    assert sum(1 for c in verification.number_checks if not c.supported) == 1


@pytest.mark.parametrize("verdict", ["OK", "fine", "reject", ""])
def test_unknown_verdict_is_rejected(verdict):
    with pytest.raises(ValidationError):
        Verification.model_validate({**VALID_VERIFICATION, "verdict": verdict})


def test_verification_allows_no_numbers_and_no_problems():
    payload = {
        **VALID_VERIFICATION,
        "number_checks": [],
        "overstated": [],
        "verdict": "ok",
    }
    verification = Verification.model_validate(payload)
    assert verification.number_checks == []
    assert verification.overstated == []


def test_verification_json_round_trip_survives_the_database():
    """Verifications are stored as JSON, so the round trip has to be lossless."""
    original = Verification.model_validate(VALID_VERIFICATION)
    assert Verification.model_validate_json(original.model_dump_json()) == original


# --------------------------------------------------------------------------
# The schema actually sent to the API
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model", [TriageItem, TriageBatch, FinalAssessment, Verification])
def test_strict_schema_derivation_succeeds(model):
    schema = transform_schema(model)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(model.model_fields)


def test_bounded_ints_become_an_enforced_enum_not_a_description_hint():
    """Field(ge=)/le= would be dropped into the description; Literal is enforced.

    This is the pin that matters most in v2: `relevance` and `disruption` are both
    bounded integers, and if either regressed to Field(ge=, le=) the API would
    happily return an out-of-range value with no error anywhere.
    """
    assert transform_schema(TriageItem)["properties"]["relevance"]["enum"] == [0, 1, 2, 3]
    assert transform_schema(FinalAssessment)["properties"]["disruption"]["enum"] == [
        1, 2, 3, 4, 5,
    ]
    assert transform_schema(Verification)["properties"]["adjusted_disruption"][
        "enum"
    ] == [1, 2, 3, 4, 5]


def test_no_schema_puts_a_numeric_bound_in_a_description():
    """Catch a ge=/le= reintroduced anywhere, not just on the fields pinned above."""
    for model in (TriageItem, TriageBatch, FinalAssessment, Verification):
        for name, prop in transform_schema(model).get("properties", {}).items():
            description = prop.get("description", "")
            assert "greater than or equal" not in description, (
                f"{model.__name__}.{name} uses a numeric bound the API will not "
                "enforce; use Literal instead"
            )


def test_nested_models_are_emitted_as_defs():
    schema = transform_schema(Verification)
    assert "$defs" in schema
    assert "NumberCheck" in schema["$defs"]
    assert schema["$defs"]["NumberCheck"]["additionalProperties"] is False


def test_triage_batch_nests_its_items():
    schema = transform_schema(TriageBatch)
    assert "TriageItem" in schema["$defs"]
    assert schema["properties"]["results"]["type"] == "array"


def test_number_check_requires_every_field():
    with pytest.raises(ValidationError):
        NumberCheck.model_validate({"claim": "4 K", "supported": True})
