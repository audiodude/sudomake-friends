"""Consumer-visible budget, billing, and response-validation boundaries."""

import asyncio
import json

import httpx
import pytest

from evaluation.model_compare.runner import MODELS, Rates, parse_output, payload_for, request_one, reserve_cost, run_cases
from evaluation.compare_models import validate_cases


def case(kind="reply", case_id="one"):
    return {"case_id": case_id, "kind": kind, "friend": "alex", "timestamp": 1.0,
            "input_excerpt": "hello", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 512}


def test_budget_does_not_start_partial_four_way_case(tmp_path):
    calls = []
    rates = {model: Rates(0.00001, 0.00001) for model in MODELS}
    snapshot = case()
    reservation = sum(reserve_cost(payload_for(snapshot, model, rates[model]), rates[model]) for model in MODELS)
    async def scenario():
        async with httpx.AsyncClient(base_url="https://gateway.invalid/", transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
            return await run_cases(client, [snapshot], rates, reservation - 0.00001, tmp_path)
    records, summary = asyncio.run(scenario())
    assert calls == []
    assert records == []
    assert summary["stop_reason"] == "budget allowance exhausted before next complete case"


def test_unknown_billed_cost_retains_full_reservation(tmp_path):
    rates = {model: Rates(0.00001, 0.00001) for model in MODELS}
    snapshot = case()
    reserved = sum(reserve_cost(payload_for(snapshot, model, rates[model]), rates[model]) for model in MODELS)
    def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"respond":false}'}}], "usage": {"prompt_tokens": 5}})
    async def scenario():
        async with httpx.AsyncClient(base_url="https://gateway.invalid/", transport=httpx.MockTransport(respond)) as client:
            return await run_cases(client, [snapshot, case(case_id="two")], rates, reserved + 0.00001, tmp_path)
    records, summary = asyncio.run(scenario())
    assert len(records) == 4
    assert summary["known_cost"] == 0
    assert summary["unknown_cost_requests"] == 4
    assert summary["accounted_cost"] == pytest.approx(reserved)
    assert all(record["cost"] is None for record in records)
    assert summary["attempted_requests"] == 4


def test_billed_truncation_preserves_raw_usage_and_charge():
    def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"respond":'}}], "usage": {"cost": 0.021, "completion_tokens": 512}})
    async def scenario():
        async with httpx.AsyncClient(base_url="https://gateway.invalid/", transport=httpx.MockTransport(respond)) as client:
            return await request_one(client, case(), payload_for(case(), MODELS[0], Rates(.000002, .00001)))
    result = asyncio.run(scenario())
    assert result["status"] == "error"
    assert result["parsed"] is None
    assert result["raw"] == '{"respond":'
    assert result["cost"] == 0.021
    assert result["finish_reason"] == "length"


def test_unexpected_charge_stops_before_another_request(tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"respond":false}'}}], "usage": {"cost": 0.5}})
    async def scenario():
        async with httpx.AsyncClient(base_url="https://gateway.invalid/", transport=httpx.MockTransport(respond)) as client:
            return await run_cases(client, [case(), case(case_id="two")], {model: Rates(.000001, .000001) for model in MODELS}, 5, tmp_path)
    records, summary = asyncio.run(scenario())
    assert len(calls) == 1
    assert summary["known_cost"] == 0.5
    assert summary["stop_reason"] == "returned charge exceeded reservation; stopped for billing safety"


@pytest.mark.parametrize("raw", ['{"respond":"false"}', '{"respond":true,"messages":[]}', '{"respond":true,"messages":[3]}', '{"send":false}', '[]'])
def test_invalid_contract_is_not_a_silent_decision(raw):
    with pytest.raises(ValueError):
        parse_output(raw, "reply")


def test_fenced_silent_decision_is_valid():
    assert parse_output('```json\n{"send": false, "messages": null}\n```', "initiate") == {"send": False, "messages": None}


def test_binary_input_is_rejected_before_budget_estimation():
    snapshot = case()
    snapshot["messages"][0]["content"] = [{"type": "image_url", "image_url": {"url": "https://example.invalid/photo.png"}}]
    with pytest.raises(ValueError, match="text-only"):
        validate_cases([snapshot])


def test_tier_and_cache_write_price_ceiling_is_not_lowest_advertised_price():
    rates = Rates.from_model({"pricing": {"prompt": "0.000002", "completion": "0.00001", "input_cache_write": "0.0000025",
                                        "overrides": [{"prompt": "0.000004", "completion": "0.000015", "input_cache_write": "0.000005"}]}})
    assert rates.prompt == 0.00001
    assert rates.completion == 0.00003
