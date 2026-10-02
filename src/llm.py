"""Small OpenRouter clients, including typed decisions and strict metadata."""

import json
import math
import os
from dataclasses import dataclass

import httpx

from .usage import log_usage

BASE_URL = "https://openrouter.ai/api/v1/"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_HELPER_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_GENERATION_MODEL = "anthropic/claude-opus-4.8"
JEV_MODEL = "typesafe/jev-1.13"
LUNA_MODEL = "openai/gpt-6-luna"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: dict
    model: str


class OpenRouterError(RuntimeError):
    """The gateway did not return a complete text response."""


def _headers(api_key: str) -> dict[str, str]:
    if not api_key or not api_key.strip():
        raise ValueError("OpenRouter API key missing. Set OPENROUTER_API_KEY.")
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "X-OpenRouter-Title": "Sudomake Friends",
    }


def _payload(model: str, messages: list[dict], max_tokens: int) -> dict:
    if "/" not in model:
        raise ValueError("Use an OpenRouter model ID with its provider prefix (provider/model).")
    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "reasoning": {"enabled": False},
    }


def _completion(response: httpx.Response, model: str, label: str) -> Completion:
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned an invalid JSON response.") from exc
    if not isinstance(body, dict):
        raise OpenRouterError("OpenRouter returned an invalid response object.")
    usage = body.get("usage") or {}
    returned_model = body.get("model") or model
    log_usage(label, returned_model, usage)
    if body.get("error"):
        error = body["error"]
        code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
        raise OpenRouterError(f"OpenRouter generation failed (code {code}).")
    choices = body.get("choices")
    if not choices:
        raise OpenRouterError("OpenRouter returned no completion.")
    choice = choices[0]
    if choice.get("finish_reason") in ("length", "error", "content_filter"):
        raise OpenRouterError(f"OpenRouter completion stopped: {choice['finish_reason']}.")
    text = (choice.get("message") or {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise OpenRouterError("OpenRouter returned no text content.")
    return Completion(text=text, usage=usage, model=returned_model)


def _number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _stage_body(response: httpx.Response, model: str, label: str) -> tuple[dict, dict, str]:
    """Log any reported bill before rejecting a failed or unusable stage."""
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenRouterError(f"OpenRouter {label} returned invalid JSON (HTTP {response.status_code}).") from exc
    if not isinstance(body, dict):
        raise OpenRouterError(f"OpenRouter {label} returned an invalid response object.")
    usage = body.get("usage")
    returned_model = body.get("model")
    logged_model = returned_model if isinstance(returned_model, str) else model
    # Decisions use input/output_tokens, unlike the ordinary chat endpoint.
    # Malformed usage must not hide a valid charge or crash the usage logger.
    logged_usage = {}
    if isinstance(usage, dict):
        cost = usage.get("cost")
        if _number(cost) and cost >= 0:
            logged_usage["cost"] = cost
        for target, alternate in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
            count = usage.get(target, usage.get(alternate))
            if type(count) is int and count >= 0:
                logged_usage[target] = count
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            logged_usage["prompt_tokens_details"] = {
                key: value for key, value in details.items()
                if key in ("cached_tokens", "cache_write_tokens")
                and type(value) is int and value >= 0
            }
    log_usage(label, logged_model, logged_usage)
    if response.is_error or body.get("error") is not None:
        error = body.get("error")
        code = error.get("code", response.status_code) if isinstance(error, dict) else response.status_code
        detail = error.get("message") if isinstance(error, dict) else None
        raise OpenRouterError(f"OpenRouter {label} failed (code {code})" + (f": {detail}" if isinstance(detail, str) else "."))
    if not response.is_success:
        raise OpenRouterError(f"OpenRouter {label} failed (HTTP {response.status_code}).")
    if not isinstance(usage, dict):
        raise OpenRouterError(f"OpenRouter {label} returned invalid usage.")
    if "cost" in usage and (not _number(usage["cost"]) or usage["cost"] < 0):
        raise OpenRouterError(f"OpenRouter {label} returned invalid usage cost.")
    if not isinstance(returned_model, str) or not (
        returned_model == model or returned_model.startswith(model + "-")
    ):
        raise OpenRouterError(f"OpenRouter {label} returned an unexpected model.")
    return body, usage, returned_model


def _decision(response: httpx.Response, label: str) -> bool:
    body, usage, _ = _stage_body(response, JEV_MODEL, label)
    for field in ("input_tokens", "output_tokens"):
        if type(usage.get(field)) is not int or usage[field] < 0:
            raise OpenRouterError("OpenRouter decision returned invalid token usage.")
    answers = body.get("answers")
    if not isinstance(answers, dict) or set(answers) != {"speak"}:
        raise OpenRouterError("OpenRouter decision returned invalid answers.")
    answer = answers["speak"]
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise OpenRouterError("OpenRouter decision returned no typed choice.")
    choice = answer.get("choice")
    if choice not in ("speak", "silence"):
        raise OpenRouterError("OpenRouter decision returned an unknown choice.")
    if "confidence" in answer:
        confidence = answer["confidence"]
        if not _number(confidence) or not 0 <= confidence <= 1:
            raise OpenRouterError("OpenRouter decision returned invalid confidence.")
    if "probabilities" in answer:
        probabilities = answer["probabilities"]
        if not isinstance(probabilities, dict) or set(probabilities) != {"speak", "silence"}:
            raise OpenRouterError("OpenRouter decision returned invalid probabilities.")
        if any(not _number(value) or not 0 <= value <= 1 for value in probabilities.values()):
            raise OpenRouterError("OpenRouter decision returned invalid probabilities.")
    return choice == "speak"


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is not representable as a finite float.")
    return parsed


def _structured_completion(response: httpx.Response, label: str) -> Completion:
    body, usage, model = _stage_body(response, LUNA_MODEL, label)
    # The routing request is the enforcement boundary. The returned provider,
    # when present, must not contradict the native-enforcing route.
    if body.get("provider") not in (None, "OpenAI"):
        raise OpenRouterError("OpenRouter metadata returned an unexpected provider.")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise OpenRouterError("OpenRouter metadata returned invalid choices.")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise OpenRouterError(f"OpenRouter metadata stopped: {choice.get('finish_reason')}.")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise OpenRouterError("OpenRouter metadata returned an invalid message.")
    if message.get("refusal") not in (None, "") or message.get("tool_calls"):
        raise OpenRouterError("OpenRouter metadata refused or returned tool calls.")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise OpenRouterError("OpenRouter metadata returned no text content.")
    try:
        metadata = json.loads(
            text, object_pairs_hook=_json_object,
            parse_constant=_json_constant, parse_float=_json_float,
        )
    except ValueError as exc:
        raise OpenRouterError("OpenRouter metadata returned invalid JSON content.") from exc
    if not isinstance(metadata, dict):
        raise OpenRouterError("OpenRouter metadata returned no metadata object.")
    return Completion(text=text, usage=usage, model=model)


class OpenRouter:
    def __init__(self, api_key: str, *, helper_model: str | None = None,
                 generation_model: str | None = None):
        self.helper_model = helper_model or os.environ.get("OPENROUTER_HELPER_MODEL") or DEFAULT_HELPER_MODEL
        self.generation_model = generation_model or os.environ.get("OPENROUTER_GENERATION_MODEL") or DEFAULT_GENERATION_MODEL
        self._http = httpx.Client(base_url=BASE_URL, headers=_headers(api_key), timeout=120)

    def complete(self, *, model: str, messages: list[dict], max_tokens: int,
                 label: str = "llm") -> Completion:
        response = self._http.post("chat/completions", json=_payload(model, messages, max_tokens))
        return _completion(response, model, label)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AsyncOpenRouter:
    def __init__(self, api_key: str, *, helper_model: str | None = None,
                 generation_model: str | None = None):
        self.helper_model = helper_model or os.environ.get("OPENROUTER_HELPER_MODEL") or DEFAULT_HELPER_MODEL
        self.generation_model = generation_model or os.environ.get("OPENROUTER_GENERATION_MODEL") or DEFAULT_GENERATION_MODEL
        self._http = httpx.AsyncClient(base_url=BASE_URL, headers=_headers(api_key), timeout=120)

    async def complete(self, *, model: str, messages: list[dict], max_tokens: int,
                       label: str = "llm") -> Completion:
        response = await self._http.post("chat/completions", json=_payload(model, messages, max_tokens))
        return _completion(response, model, label)

    async def decide(self, *, state: dict, label: str = "decision") -> bool:
        """Return a typed speak/silence choice; failure is never silence.

        The full state is sent unchanged. Context overflow is an explicit API
        failure, not an opportunity to drop identity or conversation context.
        """
        payload = {
            "model": JEV_MODEL,
            "state": state,
            "questions": {
                "speak": {
                    "type": "choice",
                    "instructions": (
                        "Should this friend speak now or stay silent? Use the full "
                        "identity, memories, availability, conversation, incoming "
                        "message, and repetition constraints in state. Favor restraint "
                        "when ambiguous, without ignoring direct questions, emotional "
                        "support, or clear conversational invitations."
                    ),
                    "criteria": {
                        "speak": "A natural, useful contribution fits this friend and the current conversation.",
                        "silence": "The friend has nothing useful to add, should not interrupt, or would repeat or pile on.",
                    },
                },
            },
        }
        try:
            response = await self._http.post(DECISIONS_URL, json=payload)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter {label} request failed: {exc}") from exc
        return _decision(response, label)

    async def complete_structured(self, *, messages: list[dict], schema: dict,
                                  max_tokens: int, label: str) -> Completion:
        """Extract metadata using Luna's native OpenAI strict-schema endpoint.

        No provider fallback or JSON-repair plugin is allowed. The caller
        validates domain semantics and schema fields before using metadata.
        """
        payload = _payload(LUNA_MODEL, messages, max_tokens)
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "friend_metadata", "strict": True, "schema": schema},
        }
        payload["provider"] = {
            "only": ["openai"],
            "order": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        try:
            response = await self._http.post("chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter {label} request failed: {exc}") from exc
        return _structured_completion(response, label)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
