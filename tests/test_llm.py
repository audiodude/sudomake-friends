"""Gateway failures must not become valid replies or persisted summaries."""

import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm import OpenRouter, OpenRouterError, _completion, _payload


def response(body, status=200):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))


@pytest.mark.parametrize("finish_reason", ["length", "error", "content_filter"])
def test_incomplete_text_is_rejected(finish_reason):
    body = {
        "choices": [{"finish_reason": finish_reason, "message": {"content": "partial summary"}}],
        "usage": {"cost": 0.0123},
    }
    with pytest.raises(OpenRouterError, match=finish_reason):
        _completion(response(body), "provider/model", "compact")


def test_billed_failure_keeps_charge_visible(caplog):
    body = {
        "choices": [{"finish_reason": "length", "message": {"content": None, "reasoning": "unfinished"}}],
        "usage": {"cost": 0.0123},
    }
    with caplog.at_level(logging.INFO, logger="src.usage"):
        with pytest.raises(OpenRouterError):
            _completion(response(body), "provider/model", "compact")
    assert "cost=$0.012300" in caplog.records[-1].getMessage()


def test_reasoning_only_response_is_not_a_reply():
    body = {"choices": [{"finish_reason": "stop", "message": {"content": None, "reasoning": "analysis"}}]}
    with pytest.raises(OpenRouterError, match="no text"):
        _completion(response(body), "provider/model", "decide:alex")


def test_gateway_error_inside_successful_http_status_is_rejected():
    with pytest.raises(OpenRouterError, match="code 502"):
        _completion(response({"error": {"code": 502, "message": "upstream failed"}}), "provider/model", "llm")


def test_http_failure_is_not_parsed_as_content():
    with pytest.raises(httpx.HTTPStatusError):
        _completion(response({"error": {"code": 401}}, status=401), "provider/model", "llm")


def test_missing_key_fails_before_creating_a_client():
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouter(" ")


def test_legacy_unqualified_model_is_rejected():
    with pytest.raises(ValueError, match="provider/model"):
        _payload("claude-sonnet-5", [{"role": "user", "content": "hello"}], 100)


def test_invalid_gateway_json_is_rejected():
    bad = httpx.Response(200, text="not JSON", request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    with pytest.raises(OpenRouterError, match="invalid JSON"):
        _completion(bad, "provider/model", "llm")
