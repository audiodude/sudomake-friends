"""Compaction model selection, helper failures, and LLM shutdown lifecycle."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import bot, chat_history, config, main, memory_validator
from src.llm import DEFAULT_HELPER_MODEL, DEFAULT_MODEL


@pytest.fixture
def runtime_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_HELPER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return tmp_path / "config.yaml"


def test_model_environment_overrides_config(runtime_config, monkeypatch):
    runtime_config.write_text(
        'model: "anthropic/claude-sonnet-5"\n'
        'helper_model: "anthropic/claude-haiku-4.5"\n'
    )
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-5")
    monkeypatch.setenv("OPENROUTER_HELPER_MODEL", "openai/gpt-5-mini")
    loaded = config.load_config()
    assert loaded["model"] == "openai/gpt-5"
    assert loaded["helper_model"] == "openai/gpt-5-mini"


def test_config_models_take_precedence_over_defaults(runtime_config):
    runtime_config.write_text(
        'model: "openai/gpt-5"\nhelper_model: "openai/gpt-5-mini"\n'
    )
    loaded = config.load_config()
    assert loaded["model"] == "openai/gpt-5"
    assert loaded["helper_model"] == "openai/gpt-5-mini"




def test_old_credentials_are_not_accepted(monkeypatch):
    monkeypatch.setattr(bot, "load_config", lambda: {"anthropic_api_key": "old-key"})
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        bot.FriendGroup()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('{"valid": false, "reason": "belongs to another friend"}',
         (False, "belongs to another friend")),
        ('{"valid": true, "reason": "shared experience"}',
         (True, "shared experience")),
        ('```json\n{"valid": true, "reason": "shared experience"}\n```',
         (False, "validator parse error")),
        ("not JSON", (False, "validator parse error")),
        ('{"valid": "false", "reason": "wrong type"}',
         (False, "validator malformed response")),
    ],
)
def test_memory_validator_rejects_invalid_or_unusable_decisions(response, expected):
    client = SimpleNamespace(
        helper_model=DEFAULT_HELPER_MODEL,
        complete=AsyncMock(return_value=SimpleNamespace(text=response)),
    )
    result = asyncio.run(memory_validator.validate_memory(
        client, "alex", "Alex is a potter.", "I own a synth.", ["river"]
    ))
    assert result == expected


def test_memory_validator_failure_does_not_authorize_write():
    client = SimpleNamespace(
        helper_model=DEFAULT_HELPER_MODEL,
        complete=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )
    result = asyncio.run(memory_validator.validate_memory(
        client, "alex", "Alex is a potter.", "I made a bowl."
    ))
    assert result == (False, "validator call failed")


def test_compaction_preserves_recent_chat_and_persists_completion(tmp_path, monkeypatch):
    chat = tmp_path / "CHAT.jsonl"
    summary = tmp_path / "CHAT_SUMMARY.md"
    rows = [
        {"timestamp": float(i), "sender": "alex", "text": f"message {i}",
         "message_id": i, "reply_to": 0, "is_reaction": False}
        for i in range(1, 5)
    ]
    chat.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(chat_history, "CHAT_PATH", chat)
    monkeypatch.setattr(chat_history, "CHAT_SUMMARY_PATH", summary)
    client = SimpleNamespace(complete=AsyncMock(
        return_value=SimpleNamespace(text="Alex finished the bowl.")
    ))
    asyncio.run(chat_history.maybe_compact(client, DEFAULT_MODEL, max_messages=3, compact_to=2))
    assert summary.read_text() == "Alex finished the bowl."
    assert [json.loads(line) for line in chat.read_text().splitlines()] == rows[-2:]


def test_compaction_failure_leaves_chat_and_summary_unchanged(tmp_path, monkeypatch):
    chat = tmp_path / "CHAT.jsonl"
    summary = tmp_path / "CHAT_SUMMARY.md"
    original = "\n".join(json.dumps({
        "timestamp": float(i), "sender": "alex", "text": "hello", "message_id": i
    }) for i in range(4)) + "\n"
    chat.write_text(original)
    summary.write_text("Existing summary")
    monkeypatch.setattr(chat_history, "CHAT_PATH", chat)
    monkeypatch.setattr(chat_history, "CHAT_SUMMARY_PATH", summary)
    client = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError("API down")))
    with pytest.raises(RuntimeError, match="API down"):
        asyncio.run(chat_history.maybe_compact(client, DEFAULT_MODEL, max_messages=3, compact_to=2))
    assert chat.read_text() == original
    assert summary.read_text() == "Existing summary"


@pytest.mark.parametrize("exit_path", ["setup_error", "no_bots", "poll_cancelled"])
def test_entrypoint_closes_client_on_every_exit(monkeypatch, exit_path):
    group = bot.FriendGroup.__new__(bot.FriendGroup)
    group.llm = SimpleNamespace(aclose=AsyncMock())
    group._active_tasks = {}
    group._response_tasks = set()
    group.bots = {} if exit_path == "no_bots" else {"alex": object()}
    group.setup = AsyncMock(side_effect=RuntimeError("setup failed") if exit_path == "setup_error" else None)
    group.poll_and_respond = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(main, "FriendGroup", lambda: group)
    error = {"setup_error": RuntimeError, "no_bots": SystemExit,
             "poll_cancelled": asyncio.CancelledError}[exit_path]
    with pytest.raises(error):
        asyncio.run(main.run())
    group.llm.aclose.assert_awaited_once()


def test_shutdown_cancels_responses_before_closing_client():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_response():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def close_client():
            assert cancelled.is_set()

        group = bot.FriendGroup.__new__(bot.FriendGroup)
        group.llm = SimpleNamespace(aclose=close_client)
        response = asyncio.create_task(pending_response())
        group._active_tasks = {"alex": response, "river": response}
        group._response_tasks = {response}
        await started.wait()
        await group.aclose()
        assert response.cancelled()

    asyncio.run(scenario())


def test_shutdown_drains_active_speaking_turn_without_starting_next(monkeypatch):
    async def scenario():
        started = set()
        stopped = set()
        first_started = asyncio.Event()

        async def think(**kwargs):
            name = kwargs["friend_name"]
            started.add(name)
            first_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(name)

        async def close_client():
            assert stopped == {"alex"}

        monkeypatch.setattr(bot, "think_and_respond", think)
        monkeypatch.setattr(bot.random, "shuffle", lambda order: None)
        group = bot.FriendGroup.__new__(bot.FriendGroup)
        group.llm = SimpleNamespace(aclose=close_client)
        group.model = DEFAULT_MODEL
        group._engagement = {}
        responders = [("alex", None, {}), ("river", None, {})]
        response = asyncio.create_task(
            group._staggered_responses(responders, "user", "hello", 1)
        )
        group._active_tasks = {"alex": response, "river": response}
        group._response_tasks = {response}
        await first_started.wait()
        await group.aclose()
        assert response.done()
        assert group._active_tasks == {}
        assert started == {"alex"}

    asyncio.run(scenario())
