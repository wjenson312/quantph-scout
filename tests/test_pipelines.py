"""The pipelines.yaml loader and its validation."""

import pytest

from scout.config import load_config
from scout.pipelines import (
    NONE_ID,
    PipelineError,
    load_pipelines,
    render_pipeline_list,
    render_readiness_checks,
)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def pipeline_set(cfg):
    return load_pipelines(cfg.pipelines_path)


def write(tmp_path, text):
    path = tmp_path / "pipelines.yaml"
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL = """
version: 1
pipelines:
  - id: sensing
    name: Sensing
    discovery_signals: [something shifts measurably]
    becomes: [sensors]
    typical_domains: [navigation]
  - id: none
    name: No clear application pipeline
    action: drop
readiness_checks:
  - id: operating_conditions
    question: Does it work without cryogenics?
  - id: scalability
    question: Can it be made beyond one sample?
"""


# --------------------------------------------------------------------------
# The shipped file
# --------------------------------------------------------------------------


def test_the_shipped_file_is_valid(pipeline_set):
    assert len(pipeline_set.pipelines) == 14
    assert len(pipeline_set.scorable) == 13   # everything except `none`
    assert NONE_ID in pipeline_set.pipelines


def test_the_max_score_is_derived_not_hardcoded(pipeline_set):
    """Five checks x 2 points = 10 today. Adding a sixth should move the
    denominator, not silently cap the scale at 10."""
    assert pipeline_set.max_score == 2 * len(pipeline_set.readiness_checks)
    assert pipeline_set.max_score == 10


def test_every_scorable_pipeline_has_signals_to_match_on(pipeline_set):
    for pipeline in pipeline_set.scorable:
        assert pipeline.discovery_signals, pipeline.id


def test_the_none_pipeline_is_marked_as_a_drop(pipeline_set):
    assert pipeline_set.pipelines[NONE_ID].is_drop


def test_the_none_pipeline_is_not_offered_as_a_scorable_option(pipeline_set):
    assert NONE_ID not in {p.id for p in pipeline_set.scorable}


# --------------------------------------------------------------------------
# Rendering for the prompt
# --------------------------------------------------------------------------


def test_the_rendered_list_includes_every_signal(pipeline_set):
    """Signals are what the model matches on, so an omitted one is an option the
    model cannot reach."""
    rendered = render_pipeline_list(pipeline_set)
    for pipeline in pipeline_set.scorable:
        assert pipeline.id in rendered
        for signal in pipeline.discovery_signals:
            assert signal in rendered


def test_the_rendered_list_still_offers_none_as_an_escape_hatch(pipeline_set):
    """A tagger with nowhere to put foundational work invents a fit instead."""
    assert NONE_ID in render_pipeline_list(pipeline_set)


def test_the_rendered_checks_are_numbered_and_named(pipeline_set):
    rendered = render_readiness_checks(pipeline_set)
    for check in pipeline_set.readiness_checks:
        assert check.id in rendered
        assert check.question in rendered


# --------------------------------------------------------------------------
# Validation -- every problem reported at once, before any spend
# --------------------------------------------------------------------------


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(PipelineError, match="not found"):
        load_pipelines(tmp_path / "absent.yaml")


def test_malformed_yaml_is_an_error(tmp_path):
    with pytest.raises(PipelineError, match="not valid YAML"):
        load_pipelines(write(tmp_path, "pipelines: [\n  - id: x\n"))


def test_a_minimal_valid_file_loads(tmp_path):
    loaded = load_pipelines(write(tmp_path, MINIMAL))
    assert loaded.max_score == 4          # two checks
    assert len(loaded.scorable) == 1


def test_a_duplicate_pipeline_id_is_rejected(tmp_path):
    text = MINIMAL.replace(
        "  - id: none",
        "  - id: sensing\n    name: Again\n    discovery_signals: [x]\n  - id: none",
    )
    with pytest.raises(PipelineError, match="duplicate pipeline id"):
        load_pipelines(write(tmp_path, text))


def test_a_pipeline_with_no_signals_is_rejected(tmp_path):
    """It could never legitimately be chosen -- only guessed at."""
    text = MINIMAL.replace("    discovery_signals: [something shifts measurably]\n", "")
    with pytest.raises(PipelineError, match="no discovery_signals"):
        load_pipelines(write(tmp_path, text))


def test_a_file_without_a_none_pipeline_is_rejected(tmp_path):
    text = MINIMAL.replace(
        "  - id: none\n    name: No clear application pipeline\n    action: drop\n", ""
    )
    with pytest.raises(PipelineError, match="somewhere to put"):
        load_pipelines(write(tmp_path, text))


def test_a_file_without_readiness_checks_is_rejected(tmp_path):
    text = MINIMAL[: MINIMAL.index("readiness_checks:")]
    with pytest.raises(PipelineError, match="readiness_checks"):
        load_pipelines(write(tmp_path, text))


def test_a_duplicate_check_id_is_rejected(tmp_path):
    text = MINIMAL + "  - id: scalability\n    question: Again?\n"
    with pytest.raises(PipelineError, match="duplicate readiness check"):
        load_pipelines(write(tmp_path, text))


def test_every_problem_is_reported_at_once(tmp_path):
    """The run should not need three attempts to surface three typos."""
    text = """
version: 1
pipelines:
  - id: a
    discovery_signals: []
  - id: a
    name: Dup
    discovery_signals: [x]
readiness_checks:
  - id: c1
"""
    with pytest.raises(PipelineError) as exc:
        load_pipelines(write(tmp_path, text))
    message = str(exc.value)
    assert "problems" in message
    assert "duplicate pipeline id" in message
    assert "no discovery_signals" in message
    assert NONE_ID in message
