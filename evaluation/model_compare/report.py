"""Private, per-case blinded comparisons and objective request measurements."""

import json
import math
import os
import random
import statistics
from pathlib import Path

from .runner import MODELS


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _block(value) -> str:
    """Keep completion/context text intact, even when it contains Markdown fences."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def _decision(case: dict, record: dict):
    if record.get("status") != "ok" or not isinstance(record.get("parsed"), dict):
        return None
    if case.get("output_contract") == "writer_plaintext_v1":
        return None
    key = "send" if case["kind"] == "initiate" else "respond"
    value = record["parsed"].get(key)
    return value if isinstance(value, bool) else None


def _writer_draft(case: dict, record: dict) -> bool:
    if case.get("output_contract") != "writer_plaintext_v1" or record.get("status") != "ok":
        return False
    parsed = record.get("parsed")
    messages = parsed.get("messages") if isinstance(parsed, dict) else None
    return (isinstance(messages, list) and 1 <= len(messages) <= 4
            and all(isinstance(text, str) and text.strip() for text in messages))


def _render_result(case: dict, record: dict | None) -> list[str]:
    if record is None:
        return ["**Not attempted:** no result record; this is not a silence decision.", ""]
    if record.get("status") != "ok":
        category = "Format error" if record.get("error_type") == "format" else "Request/completion error"
        lines = [
            f"**{category}:** no valid output; this is not silence.",
            "Error details are withheld from the blind report to avoid identifying the provider or model.",
            "",
        ]
        if record.get("raw") or record.get("error_type") == "format":
            lines += ["**Raw completion** (unaltered):", _block(record.get("raw", "")), ""]
        return lines
    if _writer_draft(case, record):
        return ["**Writer draft:** Jev acceptance is assumed; no social decision was evaluated.", "",
                "**Proposed messages** (not sent):", _block(record["parsed"]["messages"]), ""]
    parsed = record.get("parsed")
    decision = _decision(case, record)
    if decision is None:
        lines = ["**Format error:** no valid output under this case's contract; this is not silence.", ""]
    elif decision:
        lines = [f"**Decision:** {'initiate' if case['kind'] == 'initiate' else 'reply'}.", ""]
    else:
        lines = ["**Decision:** silence (explicit false).", ""]
    if isinstance(parsed, dict):
        messages = parsed.get("messages")
        if messages is None and "message" in parsed:
            messages = parsed["message"]
        lines += ["**Proposed messages** (only sent if the decision is true):", _block(messages), ""]
        reaction = parsed.get("reaction", parsed.get("react"))
        lines += ["**Proposed reaction:**", _block(reaction), ""]
        lines += ["**Proposed memory update** (not validated or saved):", _block(parsed.get("memory_update")), ""]
    if decision is None:
        lines += ["**Raw completion** (unaltered):", _block(record.get("raw", "")), ""]
    return lines


def _model_metrics(cases: list[dict], model: str, by_case: dict) -> dict:
    present = [(case, by_case.get(case["case_id"], {}).get(model)) for case in cases]
    attempts = [(case, record) for case, record in present if record is not None]
    records = [record for _, record in attempts]
    costs = [record["cost"] for record in records if _number(record.get("cost"))]
    unknown_costs = len(records) - len(costs)
    latencies = [record["latency_seconds"] for record in records if _number(record.get("latency_seconds"))]
    decisions = [_decision(case, record) for case, record in attempts]
    usage = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [record["usage"][field] for record in records
                  if isinstance(record.get("usage"), dict) and _number(record["usage"].get(field))]
        usage[field] = {
            "total": sum(values) if records and len(values) == len(records) else None,
            "known_total": sum(values) if values else None,
            "unknown_count": len(records) - len(values),
        }
    return {
        "request_count": len(records),
        "status_counts": {status: sum(record.get("status") == status for record in records)
                          for status in ("ok", "error")},
        "error_count": sum(record.get("status") != "ok" for record in records),
        "format_error_count": sum(record.get("error_type") == "format"
                                  or (record.get("status") == "ok"
                                      and _decision(case, record) is None and not _writer_draft(case, record))
                                  for case, record in attempts),
        "missing_result_count": len(cases) - len(records),
        "cost_total": sum(costs) if records and not unknown_costs else None,
        "known_cost_total": sum(costs) if costs else None,
        "cost_unknown_count": unknown_costs,
        "latency_seconds": {
            "mean": statistics.mean(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "sample_count": len(latencies),
        },
        "response_count": sum(decision is True for decision in decisions),
        "silence_count": sum(decision is False for decision in decisions),
        "writer_draft_count": sum(_writer_draft(case, record) for case, record in attempts),
        "usage": usage,
    }


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(content)
    finally:
        if fd is not None:
            os.close(fd)


def write_report(output_dir: Path, cases: list[dict], records: list[dict]) -> None:
    """Write private reports and a scorecard, preserving existing response identities."""
    models = list(MODELS)
    if any(record["model"] not in models for record in records):
        raise ValueError("Result model is not in the four-model comparison roster")
    by_case = {}
    case_ids = {case["case_id"] for case in cases}
    if len(case_ids) != len(cases):
        raise ValueError("Case IDs must be unique")
    for record in records:
        case_id = record["case_id"]
        if case_id not in case_ids:
            raise ValueError("Result refers to an unknown case")
        entries = by_case.setdefault(case_id, {})
        if record["model"] in entries:
            raise ValueError("Duplicate model result for a case")
        entries[record["model"]] = record

    lines = [
        "# Blind model comparison", "",
        "Evaluate each candidate for **naturalness**, **personality fit**, **context coherence**, "
        "**restraint** (including appropriate silence), and **memory accuracy**. "
        "Use your own judgment; there is no automated subjective quality score or AI judge.", "",
        "Letters are randomized independently for every case. The same letter does not identify "
        "the same model across cases. Keep reveal.json and metrics.json closed while comparing.", "",
        "Input excerpts below are abbreviated. For the complete context, personality, and saved "
        "memory used by each candidate, consult that case's frozen messages in cases.json.", "",
        "Cases marked writer_plaintext_v1 compare conditional writing only: Jev acceptance is "
        "assumed, metadata extraction is omitted, and drafts are not counted as social decisions. "
        "Archived combined-JSON snapshots retain their original decision contract.", "",
        "Historical chat context uses **current memories**, not historical reconstruction: "
        "later knowledge may leak into older cases. Scheduling gates, echo filters, and helper "
        "validation are not tested. Messages, reactions, and memory changes are proposals only; "
        "nothing is sent or saved to friend data.", "",
        "Costs and latencies are intentionally omitted here. Request errors, format errors, and "
        "missing results are not silence decisions. Raw invalid completions are unaltered and "
        "may coincidentally identify their model.", "",
    ]
    reveal_path = output_dir / "reveal.json"
    if reveal_path.exists():
        reveal = json.loads(reveal_path.read_text())
        if (not isinstance(reveal, dict) or set(reveal) != case_ids
                or any(not isinstance(labels, dict) or set(labels) != set("ABCD")
                       or any(not isinstance(model, str) for model in labels.values())
                       or set(labels.values()) != set(models) for labels in reveal.values())):
            raise ValueError("Existing reveal map does not match these cases; refusing to change response identities")
    else:
        reveal = {}
        rng = random.SystemRandom()
        for case in cases:
            shuffled = models.copy()
            rng.shuffle(shuffled)
            reveal[case["case_id"]] = dict(zip("ABCD", shuffled))
    for case in cases:
        labels = reveal[case["case_id"]]
        lines += [f"## Case {case['case_id']}", "",
                  f"**Friend:** {case['friend']} · **Kind:** {case['kind']}", "",
                  "**Input context / latest message:**", _block(case.get("input_excerpt", "")), ""]
        for label, model in labels.items():
            lines += [f"### {label}", ""]
            lines += _render_result(case, by_case.get(case["case_id"], {}).get(model))

    metrics = {
        "notes": "Returned costs only; unknown costs or usage are not zero. Missing result records "
                 "are not paid attempts. Counts describe decisions, not subjective quality.",
        "models": {model: _model_metrics(cases, model, by_case) for model in models},
    }
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    _write_private(output_dir / "report.md", "\n".join(lines) + "\n")
    _write_private(output_dir / "reveal.json", json.dumps(reveal, ensure_ascii=False, indent=2) + "\n")
    _write_private(output_dir / "metrics.json", json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    from .scorecard import write_scorecard
    write_scorecard(output_dir, cases, records, reveal)
