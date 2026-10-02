"""Bounded, sequential OpenRouter calls for frozen offline cases."""

import json
import math
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.llm import BASE_URL

MODELS = (
    "anthropic/claude-sonnet-5",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-6-luna",
    "google/gemini-3.8-flash",
)


def private_write(path: Path, text: str) -> None:
    # Exclusive creation avoids overwriting an existing result or following a symlink.
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        handle.write(text)


def valid_cost(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


@dataclass(frozen=True)
class Rates:
    prompt: float
    completion: float

    @classmethod
    def from_model(cls, metadata: dict) -> "Rates":
        pricing = metadata["pricing"]
        tiers = [pricing, *pricing.get("overrides", [])]
        # A price ceiling, not a predicted bill: include long-context tiers and
        # cache writes, then leave headroom for provider variation.
        def maximum(fields):
            values = [valid_cost(tier[field]) for tier in tiers for field in fields if field in tier]
            if not values or any(value is None for value in values):
                raise ValueError("Missing or invalid catalogue pricing.")
            return max(values) * 2
        if any(valid_cost(tier.get("request", 0)) != 0 for tier in tiers):
            raise ValueError("Per-request charges are not supported by this evaluation.")
        return cls(maximum(("prompt", "input_cache_read", "input_cache_write")),
                   maximum(("completion", "internal_reasoning")))


def payload_for(case: dict, model: str, rates: Rates) -> dict:
    reasoning = {"effort": "low", "exclude": True} if model.startswith("google/") else {"enabled": False}
    # Gemini's limit includes mandatory reasoning. This is recorded separately
    # from the unchanged runtime visible-output limit in the case snapshot.
    max_tokens = max(4096, case["max_tokens"]) if model.startswith("google/") else case["max_tokens"]
    return {
        "model": model,
        "messages": case["messages"],
        "max_tokens": max_tokens,
        "stream": False,
        "reasoning": reasoning,
        "provider": {"max_price": {"prompt": rates.prompt * 1_000_000,
                                   "completion": rates.completion * 1_000_000}},
    }


def reserve_cost(payload: dict, rates: Rates) -> float:
    # For text-only input, UTF-8 bytes deliberately overestimate token counts.
    # Include generous framing overhead; reserve full output including reasoning.
    encoded = json.dumps(payload["messages"], ensure_ascii=False).encode("utf-8")
    return (len(encoded) + 4096) * rates.prompt + payload["max_tokens"] * rates.completion


def parse_output(raw: str, kind: str) -> dict:
    text = raw.strip()
    if text.startswith("```") and "\n" in text:
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("Response is not a JSON object.") from None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            raise ValueError("Response is not a JSON object.") from None
    decision = "respond" if kind == "reply" else "send"
    if not isinstance(parsed, dict) or type(parsed.get(decision)) is not bool:
        raise ValueError(f"Missing boolean {decision} decision.")
    messages = parsed.get("messages")
    if messages is not None and (not isinstance(messages, list) or any(not isinstance(m, str) for m in messages)):
        raise ValueError("messages must be an array of strings or null.")
    if parsed.get("message") is not None and not isinstance(parsed["message"], str):
        raise ValueError("message must be a string or null.")
    if parsed.get("memory_update") is not None and not isinstance(parsed["memory_update"], str):
        raise ValueError("memory_update must be a string or null.")
    if parsed[decision] and not (messages or parsed.get("message")):
        raise ValueError("Positive decision without a message.")
    return parsed


async def request_one(client: httpx.AsyncClient, case: dict, payload: dict) -> dict:
    started = time.monotonic()
    record = {"case_id": case["case_id"], "model": payload["model"], "status": "error",
              "raw": "", "parsed": None, "usage": {}, "cost": None,
              "error": None, "error_type": "request", "finish_reason": None, "generation_id": None,
              "request_max_tokens": payload["max_tokens"], "reasoning": payload["reasoning"]}
    try:
        response = await client.post("chat/completions", json=payload)
        try:
            body = response.json()
        except ValueError:
            raise ValueError("Gateway returned invalid JSON.") from None
        if not isinstance(body, dict):
            raise ValueError("Gateway returned an invalid response object.")
        usage = body.get("usage")
        if isinstance(usage, dict):
            record["usage"] = usage
            record["cost"] = valid_cost(usage.get("cost"))
        record["generation_id"] = body.get("id")
        record["returned_model"] = body.get("model")
        if response.status_code != 200:
            raise ValueError(f"Gateway HTTP {response.status_code}.")
        if body.get("error"):
            code = body["error"].get("code", "unknown") if isinstance(body["error"], dict) else "unknown"
            raise ValueError(f"Gateway generation error ({code}).")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Gateway returned no completion.")
        choice = choices[0]
        record["finish_reason"] = choice.get("finish_reason")
        message = choice.get("message") or {}
        raw = message.get("content") if isinstance(message, dict) else None
        record["raw"] = raw if isinstance(raw, str) else ""
        if choice.get("finish_reason") in ("length", "error", "content_filter"):
            raise ValueError(f"Incomplete generation ({choice['finish_reason']}).")
        if not record["raw"].strip():
            raise ValueError("Gateway returned no text.")
        record["error_type"] = "format"
        record["parsed"] = parse_output(record["raw"], case["kind"])
        record["status"] = "ok"
        record["error_type"] = None
    except httpx.HTTPError:
        # Never retry a potentially billed generation or print credentials/context.
        record["error"] = "Gateway transport error; billing may be unknown."
    except ValueError as exc:
        record["error"] = str(exc)
    finally:
        record["latency_seconds"] = time.monotonic() - started
    return record


async def run_cases(client: httpx.AsyncClient, cases: list[dict], rates: dict[str, Rates],
                    budget: float, output_dir: Path) -> tuple[list[dict], dict]:
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("Budget must be positive and finite.")
    records = []
    accounted = 0.0
    stop_reason = "completed"
    with (output_dir / "results.jsonl").open("x", encoding="utf-8") as journal:
        (output_dir / "results.jsonl").chmod(0o600)
        for case in cases:
            requests = {model: payload_for(case, model, rates[model]) for model in MODELS}
            reservations = {model: reserve_cost(requests[model], rates[model]) for model in MODELS}
            # Start a case only if the whole four-way comparison fits.
            if accounted + sum(reservations.values()) > budget:
                stop_reason = "budget allowance exhausted before next complete case"
                break
            order = list(MODELS)
            random.SystemRandom().shuffle(order)
            for model in order:
                record = await request_one(client, case, requests[model])
                reserved = reservations[model]
                charge = record["cost"] if record["cost"] is not None else reserved
                accounted += charge
                record["reserved_cost"] = reserved
                record["accounted_cost"] = charge
                records.append(record)
                journal.write(json.dumps(record, ensure_ascii=False) + "\n")
                journal.flush()
                print(f"{len(records)}/{len(cases) * len(MODELS)} {case['case_id']} {model}: {record['status']}", flush=True)
                if charge > reserved:
                    stop_reason = "returned charge exceeded reservation; stopped for billing safety"
                    break
            if stop_reason != "completed":
                break
    known_total = sum(record["cost"] for record in records if record["cost"] is not None)
    summary = {"budget": budget, "accounted_cost": accounted, "known_cost": known_total,
               "unknown_cost_requests": sum(record["cost"] is None for record in records),
               "attempted_requests": len(records), "planned_requests": len(cases) * len(MODELS),
               "stop_reason": stop_reason}
    private_write(output_dir / "run.json", json.dumps(summary, indent=2) + "\n")
    return records, summary
