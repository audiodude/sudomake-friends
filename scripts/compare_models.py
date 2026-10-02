"""Prepare a private offline comparison; --run explicitly authorizes paid calls."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

from dotenv import dotenv_values
import httpx

from scripts.model_compare.cases import build_cases
from scripts.model_compare.report import write_report
from scripts.model_compare.runner import MODELS, Rates, private_write, run_cases, payload_for, reserve_cost
from src.llm import BASE_URL


def validate_cases(cases: list[dict]) -> None:
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cases must be a non-empty JSON array.")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or case["case_id"] in ids:
            raise ValueError("Every case needs a unique string case_id.")
        ids.add(case["case_id"])
        if case.get("kind") not in ("reply", "initiate"):
            raise ValueError("Unknown case kind.")
        if type(case.get("max_tokens")) is not int or not 1 <= case["max_tokens"] <= 4096:
            raise ValueError("Case max_tokens must be between 1 and 4096.")
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Every case requires prompt messages.")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant"):
                raise ValueError("Invalid prompt message.")
            content = message.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or not content or any(
                not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str)
                for block in content
            ):
                raise ValueError("This budget estimator supports text-only prompts.")


async def main_async(args) -> None:
    if args.count < 1 or not math.isfinite(args.budget) or not 0 < args.budget <= 5:
        raise ValueError("Choose a positive case count and a budget greater than zero, at most $5.")
    home = args.home.expanduser().resolve()
    output = args.output.expanduser().resolve() if args.output else home / "evaluations" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for live_dir in (home / "data", home / "friends"):
        if output == live_dir or live_dir in output.parents:
            raise ValueError("Evaluation output cannot be inside live friend or runtime data directories.")
    if output.exists():
        raise ValueError("Output directory already exists; choose a new directory to avoid overwriting results.")
    cases = json.loads(args.cases_file.read_text()) if args.cases_file else await build_cases(home, args.count)
    validate_cases(cases)
    api_key = os.environ.get("OPENROUTER_API_KEY") or dotenv_values(home / ".env").get("OPENROUTER_API_KEY")
    if args.run and not api_key:
        raise ValueError("OPENROUTER_API_KEY is missing from the environment and installed .env.")
    # Parent directories may already exist; only this run directory contains data.
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    private_write(output / "cases.json", json.dumps(cases, ensure_ascii=False, indent=2) + "\n")
    headers = {"X-OpenRouter-Title": "Sudomake Friends offline comparison"}
    if args.run:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=180) as client:
        response = await client.get("models")
        response.raise_for_status()
        catalogue = {entry["id"]: entry for entry in response.json()["data"]}
        if any(model not in catalogue for model in MODELS):
            raise ValueError("A selected model is unavailable in the current OpenRouter catalogue.")
        rates = {model: Rates.from_model(catalogue[model]) for model in MODELS}
        reserve = sum(reserve_cost(payload_for(case, model, rates[model]), rates[model])
                      for case in cases for model in MODELS)
        plan = {"models": list(MODELS), "case_count": len(cases), "requests": len(cases) * len(MODELS),
                "budget": args.budget, "full_run_conservative_reservation": reserve,
                "catalogue": {model: catalogue[model] for model in MODELS},
                "reasoning": {"google/gemini-3.8-flash": "low; total output cap includes reasoning",
                              "others": "disabled"},
                "limits": "One sample per model/case; current memories are hindsight context. No Telegram or runtime writes. Missing billed costs retain full reservations. Not a transactional provider billing cap."}
        private_write(output / "plan.json", json.dumps(plan, indent=2) + "\n")
        print(f"Prepared {len(cases)} cases / {plan['requests']} requests; conservative reservation ${reserve:.4f}; allowance ${args.budget:.2f}.", flush=True)
        print(f"Private output: {output}", flush=True)
        if not args.run:
            print("No paid calls made. Use --run --cases-file <this directory>/cases.json with a new output directory to execute this snapshot.")
            return
        records, summary = await run_cases(client, cases, rates, args.budget, output)
        write_report(output, cases, records)
        print(f"Finished: {summary['attempted_requests']}/{summary['planned_requests']} requests; returned charges ${summary['known_cost']:.6f}; unknown charges {summary['unknown_cost_requests']}; {summary['stop_reason']}.")
        print(f"Blind comparison: {output / 'report.md'}")
        print(f"Interactive scorecard: {output / 'scorecard.html'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path("~/.sudomake-friends"), help="Installed friend/chat directory, read only")
    parser.add_argument("--output", type=Path, help="New private output directory (default: installed evaluations/<timestamp>)")
    parser.add_argument("--count", type=int, default=25, help="Number of frozen cases, default 25")
    parser.add_argument("--budget", type=float, default=5, help="Dollar allowance, at most 5")
    parser.add_argument("--cases-file", type=Path, help="Reuse a previously prepared cases.json instead of sampling again")
    parser.add_argument("--run", action="store_true", help="Send private context through OpenRouter and make paid calls")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # HTTP exceptions can carry request URLs but not headers; avoid all raw
        # provider response bodies and sensitive file content in diagnostics.
        parser.exit(1, f"Comparison failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
