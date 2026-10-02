"""Blinding, missing measurements, and error semantics in offline reports."""

import json
import stat

import pytest

from evaluation.model_compare import report
from evaluation.model_compare.runner import MODELS


def case(case_id="case-1", kind="reply"):
    return {
        "case_id": case_id,
        "kind": kind,
        "friend": "alex",
        "timestamp": 1234567890.0,
        "input_excerpt": "[human] how did the pottery class go?",
        "messages": [],
        "max_tokens": 512,
    }


def result(case_id="case-1", model=MODELS[0], **changes):
    value = {
        "case_id": case_id,
        "model": model,
        "status": "ok",
        "raw": '{"respond":true,"messages":["it was fun"]}',
        "parsed": {"respond": True, "messages": ["it was fun"], "reaction": "thumbs up",
                   "memory_update": "Alex tried a pottery class."},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "cost": 0.0123456789,
        "latency_seconds": 123.456789,
        "error": None,
        "finish_reason": "stop",
    }
    value.update(changes)
    return value


def outputs(directory):
    return (
        (directory / "report.md").read_text(),
        json.loads((directory / "reveal.json").read_text()),
        json.loads((directory / "metrics.json").read_text())["models"],
    )


def test_blind_report_hides_identities_error_details_and_measurements(tmp_path):
    records = [result(model=model) for model in MODELS]
    records[-1] = result(model=MODELS[-1], status="error", parsed=None, raw="",
                         error=f"{MODELS[-1]} failed at https://provider.example/endpoint")
    report.write_report(tmp_path, [case()], records)
    blind, reveal, metrics = outputs(tmp_path)
    for model in MODELS:
        assert model not in blind
    assert "provider.example" not in blind
    assert "0.0123456789" not in blind
    assert "123.456789" not in blind
    assert set(reveal["case-1"]) == set("ABCD")
    assert set(reveal["case-1"].values()) == set(MODELS)
    assert "it was fun" in blind
    assert "thumbs up" in blind
    assert "Alex tried a pottery class." in blind
    assert "[human] how did the pottery class go?" in blind
    assert metrics[MODELS[-1]]["error_count"] == 1


def test_only_valid_boolean_decisions_count_as_silence_or_responses(tmp_path):
    records = [
        result(parsed={"respond": False}, cost=0),
        result(model=MODELS[1], status="error", error_type="format", parsed=None,
               raw="broken completion", error=f"Invalid output from {MODELS[1]}"),
        result(model=MODELS[2], parsed={"respond": "false"}, raw='{"respond":"false"}'),
        result(model=MODELS[3], status="error", error_type="request", parsed=None,
               raw="", error="503"),
        result(case_id="case-2", parsed={"send": False}),
    ]
    report.write_report(tmp_path, [case(), case("case-2", "initiate")], records)
    blind, _, metrics = outputs(tmp_path)
    assert blind.count("**Decision:** silence (explicit false).") == 2
    assert "broken completion" in blind
    assert '{"respond":"false"}' in blind
    assert "**Request/completion error:**" in blind
    assert "**Format error:**" in blind
    assert metrics[MODELS[0]]["silence_count"] == 2
    for model in MODELS[1:]:
        assert metrics[model]["silence_count"] == 0
        assert metrics[model]["response_count"] == 0
    assert metrics[MODELS[1]]["format_error_count"] == 1
    assert metrics[MODELS[2]]["format_error_count"] == 1
    assert metrics[MODELS[3]]["format_error_count"] == 0
    assert metrics[MODELS[2]]["status_counts"] == {"ok": 1, "error": 0}


def test_missing_cost_usage_and_results_are_unknown_not_free_or_attempted(tmp_path):
    records = [
        result(cost=0.02, latency_seconds=1),
        result(case_id="case-2", cost=None, usage={}, latency_seconds=3),
        result(model=MODELS[1], cost=0),
        result(case_id="case-2", model=MODELS[1], cost=0),
        result(model=MODELS[2], cost=0.03, usage=None),
    ]
    report.write_report(tmp_path, [case(), case("case-2")], records)
    blind, _, metrics = outputs(tmp_path)
    partial = metrics[MODELS[0]]
    assert partial["cost_total"] is None
    assert partial["known_cost_total"] == 0.02
    assert partial["cost_unknown_count"] == 1
    assert partial["usage"]["prompt_tokens"] == {"total": None, "known_total": 10, "unknown_count": 1}
    assert partial["latency_seconds"] == {"mean": 2, "median": 2, "sample_count": 2}
    assert metrics[MODELS[1]]["cost_total"] == 0
    assert metrics[MODELS[1]]["cost_unknown_count"] == 0
    assert metrics[MODELS[2]]["cost_total"] == 0.03
    assert metrics[MODELS[2]]["request_count"] == 1
    assert metrics[MODELS[2]]["missing_result_count"] == 1
    assert metrics[MODELS[2]]["usage"]["total_tokens"]["total"] is None
    absent = metrics[MODELS[3]]
    assert absent["request_count"] == 0
    assert absent["missing_result_count"] == 2
    assert absent["cost_total"] is None
    assert absent["known_cost_total"] is None
    assert absent["cost_unknown_count"] == 0
    assert absent["latency_seconds"] == {"mean": None, "median": None, "sample_count": 0}
    assert absent["usage"]["total_tokens"]["total"] is None
    assert blind.count("**Not attempted:**") == 3


def test_generated_reports_and_existing_files_are_private(tmp_path):
    directory = tmp_path / "output"
    directory.mkdir(mode=0o755)
    for name in ("report.md", "metrics.json"):
        path = directory / name
        path.write_text("old public contents")
        path.chmod(0o644)
    mapping = {"case-1": dict(zip("ABCD", MODELS))}
    (directory / "reveal.json").write_text(json.dumps(mapping))
    (directory / "reveal.json").chmod(0o644)
    report.write_report(directory, [case()], [result()])
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("report.md", "reveal.json", "metrics.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
        assert "old public contents" not in (directory / name).read_text()


def test_regenerating_report_preserves_response_identities(tmp_path):
    cases = [case("case-z"), case("case-a")]
    records = [result(case_id=item["case_id"], model=model) for item in cases for model in MODELS]
    report.write_report(tmp_path, cases, records)
    first_blind, first_reveal, _ = outputs(tmp_path)
    report.write_report(tmp_path, cases, records)
    second_blind, second_reveal, _ = outputs(tmp_path)
    assert first_reveal == second_reveal
    assert first_blind == second_blind


def test_invalid_existing_reveal_is_not_overwritten(tmp_path):
    saved = '{"different-case": {"A": "not-the-comparison-model"}}'
    (tmp_path / "reveal.json").write_text(saved)
    with pytest.raises(ValueError):
        report.write_report(tmp_path, [case()], [result()])
    assert (tmp_path / "reveal.json").read_text() == saved
    assert not (tmp_path / "report.md").exists()


def test_invalid_raw_completion_is_preserved_even_if_it_names_its_model(tmp_path):
    raw = f"I am {MODELS[0]}. ```invalid json```"
    report.write_report(tmp_path, [case()], [result(status="error", error_type="format", parsed=None, raw=raw)])
    blind, _, _ = outputs(tmp_path)
    assert raw in blind
    assert "**Raw completion** (unaltered):" in blind


def test_no_requests_still_reports_all_four_candidates_as_unattempted(tmp_path):
    report.write_report(tmp_path, [case()], [])
    blind, reveal, metrics = outputs(tmp_path)
    assert set(reveal["case-1"].values()) == set(MODELS)
    assert blind.count("**Not attempted:**") == 4
    for model in MODELS:
        assert metrics[model]["request_count"] == 0
        assert metrics[model]["error_count"] == 0
        assert metrics[model]["silence_count"] == 0
        assert metrics[model]["missing_result_count"] == 1
        assert metrics[model]["cost_total"] is None
