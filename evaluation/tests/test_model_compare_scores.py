"""Scoring identities, neutral-vs-missing values, and matched sample boundaries."""

import copy

import pytest

from evaluation.model_compare.runner import MODELS
from evaluation.model_compare.scorecard import scoring_data, summarize_scores


@pytest.fixture
def comparison():
    cases = [{"case_id": name, "friend": "alex", "kind": "reply", "input_excerpt": name,
              "messages": [{"role": "user", "content": name}]} for name in ("first", "second")]
    reveal = {"first": dict(zip("ABCD", MODELS)), "second": dict(zip("ABCD", MODELS[1:] + MODELS[:1]))}
    records = [{"case_id": case["case_id"], "model": model, "status": "ok",
                "parsed": {"respond": False}, "raw": '{"respond": false}'} for case in cases for model in MODELS]
    return cases, records, reveal


def exported(data, scores):
    return {"version": 1, "run_id": data["run_id"], "scores": scores}


def test_neutral_is_scored_and_missing_is_not_zero(comparison):
    cases, records, reveal = comparison
    data = scoring_data(cases, records, reveal)
    summary = summarize_scores(data, reveal, exported(data, {"first:A": 0, "first:B": 2}))
    sonnet = summary["models"][MODELS[0]]
    haiku = summary["models"][MODELS[1]]
    assert sonnet["scored"] == 1
    assert sonnet["neutral"] == 1
    assert sonnet["unscored"] == 1
    assert sonnet["mean"] == 0
    assert haiku["mean"] == 2
    assert summary["models"][MODELS[2]]["mean"] is None
    assert summary["complete_cases"] == 0
    assert all(row["complete_case_mean"] is None for row in summary["models"].values())


def test_changed_letters_align_scores_per_case_not_per_column(comparison):
    cases, records, reveal = comparison
    data = scoring_data(cases, records, reveal)
    scores = {"first:A": 3, "first:B": 1, "first:C": -1, "first:D": 0,
              "second:D": -3, "second:A": 3, "second:B": 1, "second:C": 2}
    summary = summarize_scores(data, reveal, exported(data, scores))
    assert summary["complete_cases"] == 2
    assert summary["models"][MODELS[0]]["mean"] == 0
    assert summary["models"][MODELS[1]]["mean"] == 2
    assert summary["models"][MODELS[2]]["mean"] == 0
    assert summary["models"][MODELS[3]]["mean"] == 1
    assert all(row["mean"] == row["complete_case_mean"] for row in summary["models"].values())


def test_incomplete_case_is_excluded_from_matched_means(comparison):
    cases, records, reveal = comparison
    data = scoring_data(cases, records, reveal)
    scores = {"first:A": 0, "first:B": 0, "first:C": 0, "first:D": 0, "second:D": 3}
    summary = summarize_scores(data, reveal, exported(data, scores))
    assert summary["complete_cases"] == 1
    assert summary["models"][MODELS[0]]["mean"] == 1.5
    assert summary["models"][MODELS[0]]["complete_case_mean"] == 0


@pytest.mark.parametrize("scores", [{"first:A": True}, {"first:A": "0"}, {"first:A": 4}, {"other:A": 1}, [3, 1, -1]])
def test_unidentified_or_invalid_scores_are_rejected(comparison, scores):
    cases, records, reveal = comparison
    data = scoring_data(cases, records, reveal)
    with pytest.raises(ValueError):
        summarize_scores(data, reveal, exported(data, scores))


def test_letter_map_change_invalidates_previous_export(comparison):
    cases, records, reveal = comparison
    data = scoring_data(cases, records, reveal)
    changed = copy.deepcopy(reveal)
    changed["first"]["A"], changed["first"]["B"] = changed["first"]["B"], changed["first"]["A"]
    changed_data = scoring_data(cases, records, changed)
    with pytest.raises(ValueError):
        summarize_scores(changed_data, changed, exported(data, {"first:A": 2}))


def test_unattempted_response_cannot_be_scored_as_silence(comparison):
    cases, records, reveal = comparison
    records = [row for row in records if not (row["case_id"] == "first" and row["model"] == MODELS[0])]
    data = scoring_data(cases, records, reveal)
    with pytest.raises(ValueError):
        summarize_scores(data, reveal, exported(data, {"first:A": 0}))
