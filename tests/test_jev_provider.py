"""Typed decision and native-schema boundaries exercised through mock HTTP."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm import (
    AsyncOpenRouter, BASE_URL, JEV_MODEL, OpenRouterError,
)

SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string"}},
    "required": ["topic"],
    "additionalProperties": False,
}
MESSAGES = [{"role": "user", "content": "Exact draft and context."}]


def decision_body(choice="speak"):
    return {
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": {"speak": {"type": "choice", "choice": choice}},
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }


def metadata_body(text='{"topic":"coffee"}'):
    return {
        "model": "openai/gpt-6-luna-20260922",
        "provider": "OpenAI",
        "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 8, "cost": 0.0003},
    }


def call(handler, *, structured=False, state=None):
    async def request():
        client = AsyncOpenRouter("test-key", helper_model="helper/custom", generation_model="writer/custom")
        await client._http.aclose()
        client._http = httpx.AsyncClient(
            base_url=BASE_URL,
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer test-key"},
        )
        async with client:
            if structured:
                return await client.complete_structured(
                    messages=MESSAGES, schema=SCHEMA, max_tokens=256, label="metadata:alex",
                )
            return await client.decide(state=state or {"identity": "Alex", "history": "Exact history"})
    return asyncio.run(request())


def respond(body, status=200):
    return lambda request: httpx.Response(status, content=json.dumps(body), headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("choice,expected", [("speak", True), ("silence", False)])
def test_documented_choice_outcome(choice, expected):
    assert call(respond(decision_body(choice))) is expected


@pytest.mark.parametrize("confidence", [0, 0.001, 0.5, 1])
def test_valid_confidence_never_overrides_typed_choice(confidence):
    body = decision_body()
    body["answers"]["speak"].update(confidence=confidence, probabilities={"speak": 0.01, "silence": 0.99})
    assert call(respond(body)) is True


@pytest.mark.parametrize("answer", [
    None, [], "silence", {}, {"type": "noul", "noul": 0.9},
    {"type": "choice"}, {"type": "choice", "choice": None},
    {"type": "choice", "choice": False}, {"type": "choice", "choice": []},
    {"type": "choice", "choice": "maybe"},
    {"type": "choice", "choice": "SPEAK"},
    {"type": "choice", "choice": "silence", "confidence": True},
    {"type": "choice", "choice": "silence", "confidence": "0.9"},
    {"type": "choice", "choice": "silence", "confidence": -0.01},
    {"type": "choice", "choice": "silence", "confidence": 1.01},
    {"type": "choice", "choice": "silence", "probabilities": None},
    {"type": "choice", "choice": "silence", "probabilities": []},
    {"type": "choice", "choice": "silence", "probabilities": {"silence": 1}},
    {"type": "choice", "choice": "silence", "probabilities": {"speak": True, "silence": 0}},
    {"type": "choice", "choice": "silence", "probabilities": {"speak": -0.1, "silence": 1}},
])
def test_unusable_choice_is_error_not_silence(answer):
    body = decision_body("silence")
    body["answers"]["speak"] = answer
    with pytest.raises(OpenRouterError):
        call(respond(body))


@pytest.mark.parametrize("field,value", [
    ("answers", None), ("answers", []), ("answers", {}),
    ("answers", {"other": {"type": "choice", "choice": "silence"}}),
    ("model", None), ("model", []), ("model", "typesafe/jev-router"),
    ("usage", None), ("usage", []), ("usage", {}),
    ("usage", {"input_tokens": True, "output_tokens": 0}),
    ("usage", {"input_tokens": 1, "output_tokens": -1}),
    ("usage", {"input_tokens": 1, "output_tokens": 0, "cost": "free"}),
])
def test_malformed_decision_envelope(field, value):
    body = decision_body()
    body[field] = value
    with pytest.raises(OpenRouterError):
        call(respond(body))


@pytest.mark.parametrize("field", ["confidence", "probabilities", "cost"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_decision_fields_fail(field, constant):
    body = decision_body("silence")
    if field == "cost":
        body["usage"]["cost"] = "INVALID_NUMBER"
    elif field == "probabilities":
        body["answers"]["speak"]["probabilities"] = {"speak": "INVALID_NUMBER", "silence": 1}
    else:
        body["answers"]["speak"]["confidence"] = "INVALID_NUMBER"
    raw = json.dumps(body).replace('"INVALID_NUMBER"', constant)
    with pytest.raises(OpenRouterError):
        call(lambda request: httpx.Response(200, text=raw))


def test_pinned_response_model_and_optional_cost_are_accepted(caplog):
    body = decision_body("silence")
    body["model"] = JEV_MODEL
    del body["usage"]["cost"]
    with caplog.at_level(logging.INFO, logger="src.usage"):
        assert call(respond(body)) is False
    assert "cost=unknown" in caplog.records[-1].getMessage()


@pytest.mark.parametrize("body", [None, [], "silence", 1])
@pytest.mark.parametrize("structured", [False, True])
def test_nonobject_http_json_is_stage_error(body, structured):
    with pytest.raises(OpenRouterError, match="response object"):
        call(respond(body), structured=structured)


@pytest.mark.parametrize("structured", [False, True])
def test_nonjson_http_body_is_stage_error(structured):
    with pytest.raises(OpenRouterError, match="invalid JSON"):
        call(lambda request: httpx.Response(200, text="upstream HTML"), structured=structured)


@pytest.mark.parametrize("status", [200, 400, 402, 413, 429, 503])
def test_billed_decision_error_stays_visible(status, caplog):
    body = decision_body()
    body["error"] = {"code": status if status != 200 else 502, "message": "upstream failed"}
    with caplog.at_level(logging.INFO, logger="src.usage"):
        with pytest.raises(OpenRouterError, match="failed"):
            call(respond(body, status))
    message = caplog.records[-1].getMessage()
    assert "in=476" in message
    assert "out=70" in message
    assert "cost=$0.000020" in message


def test_billed_malformed_decision_is_logged_before_validation(caplog):
    body = decision_body()
    body["answers"] = None
    with caplog.at_level(logging.INFO, logger="src.usage"):
        with pytest.raises(OpenRouterError, match="answers"):
            call(respond(body))
    assert "cost=$0.000020" in caplog.records[-1].getMessage()


def test_context_overflow_is_explicit_and_context_is_never_truncated():
    state = {"identity": "do not drop", "history": "context " * 50000, "incoming": "direct question"}
    def handler(request):
        assert json.loads(request.content)["state"] == state
        return httpx.Response(400, json={"error": {"code": 400, "message": "maximum context length is 32000 tokens"}})
    with pytest.raises(OpenRouterError, match="maximum context length"):
        call(handler, state=state)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failure_is_a_stage_error(structured, error_type):
    def handler(request):
        raise error_type("transport failed", request=request)
    with pytest.raises(OpenRouterError, match="request failed"):
        call(handler, structured=structured)




@pytest.mark.parametrize("text", [
    None, "", "  ", [], {"topic": "coffee"},
    "reasoning only", '```json\n{"topic":"coffee"}\n```',
    'Here is JSON: {"topic":"coffee"}', '[]', 'null', '"coffee"',
    '{"topic":"coffee"} trailing', '{"topic":NaN}', '{"topic":Infinity}',
    '{"topic":-Infinity}', '{"topic":1e999}',
    '{"topic":"first","topic":"second"}',
])
def test_unusable_metadata_content_is_failure_without_prose_repair(text):
    with pytest.raises(OpenRouterError):
        call(respond(metadata_body(text)), structured=True)


@pytest.mark.parametrize("finish_reason", [None, "length", "error", "content_filter", "tool_calls", "unknown"])
def test_metadata_requires_a_complete_stop(finish_reason, caplog):
    body = metadata_body()
    body["choices"][0]["finish_reason"] = finish_reason
    with caplog.at_level(logging.INFO, logger="src.usage"):
        with pytest.raises(OpenRouterError, match="stopped"):
            call(respond(body), structured=True)
    assert "cost=$0.000300" in caplog.records[-1].getMessage()


@pytest.mark.parametrize("field,value", [
    ("choices", None), ("choices", {}), ("choices", []), ("choices", [None]),
    ("choices", [{}, {}]), ("provider", "Azure"), ("provider", "Other"),
    ("model", "openai/gpt-6-sol"), ("usage", []),
])
def test_metadata_malformed_envelopes_are_errors(field, value):
    body = metadata_body()
    body[field] = value
    with pytest.raises(OpenRouterError):
        call(respond(body), structured=True)


@pytest.mark.parametrize("message", [
    None, [], "text", {"reasoning": "internal"},
    {"content": '{"topic":"coffee"}', "refusal": "cannot comply"},
    {"content": '{"topic":"coffee"}', "tool_calls": [{"id": "tool"}]},
])
def test_refusal_and_noncontent_metadata_are_errors(message):
    body = metadata_body()
    body["choices"][0]["message"] = message
    with pytest.raises(OpenRouterError):
        call(respond(body), structured=True)


def test_metadata_http_failure_logs_bill(caplog):
    body = metadata_body()
    body["error"] = {"code": 503, "message": "provider unavailable"}
    with caplog.at_level(logging.INFO, logger="src.usage"):
        with pytest.raises(OpenRouterError, match="503"):
            call(respond(body, 503), structured=True)
    assert "cost=$0.000300" in caplog.records[-1].getMessage()


