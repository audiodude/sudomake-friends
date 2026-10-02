"""Small sync/async clients for OpenRouter's non-streaming chat API."""

import os
from dataclasses import dataclass

import httpx

from .usage import log_usage

BASE_URL = "https://openrouter.ai/api/v1/"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_HELPER_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_GENERATION_MODEL = "anthropic/claude-opus-4.8"


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

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
