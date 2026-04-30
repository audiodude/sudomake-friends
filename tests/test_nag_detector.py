"""Tests for the nag detector — must catch the chapter-3 pile-on pattern
without false-flagging normal conversation."""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def chat_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a fresh tmp dir before any module imports it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("src."):
            del sys.modules[mod]
    return tmp_path


def _write_chat(chat_dir: Path, messages: list[dict]):
    chat_path = chat_dir / "CHAT.jsonl"
    with chat_path.open("w") as f:
        for m in messages:
            base = {
                "timestamp": time.time(),
                "sender": "alex",
                "text": "",
                "message_id": 0,
                "reply_to": 0,
                "is_reaction": False,
            }
            base.update(m)
            f.write(json.dumps(base) + "\n")


BOT_NAMES = {"alex", "casey", "emery", "river"}


def _mock_client(nags: list[dict]):
    """Build a mock Anthropic client that returns the given nags."""
    client = AsyncMock()
    response = MagicMock()
    response.content = [MagicMock(text=json.dumps({"nags": nags}))]
    client.messages.create.return_value = response
    return client


class TestNagDetector:
    def test_empty_chat_returns_empty(self, chat_dir):
        from src.nag_detector import detect_nag_pileons
        client = _mock_client([])
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert result == []
        client.messages.create.assert_not_called()

    def test_few_messages_skips_llm_call(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "did anyone watch the game?"},
            {"sender": "casey", "text": "yeah it was great"},
        ])
        from src.nag_detector import detect_nag_pileons
        client = _mock_client([])
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert result == []
        client.messages.create.assert_not_called()

    def test_pile_on_detected(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "did anyone explain chapter 3 yet?"},
            {"sender": "casey", "text": "wait did alex ever explain chapter 3?"},
            {"sender": "emery", "text": "still wondering about chapter 3"},
            {"sender": "river", "text": "yeah did we ever get an answer about chapter 3?"},
        ])
        from src.nag_detector import detect_nag_pileons
        client = _mock_client([
            {"topic": "chapter 3 explanation", "speakers": ["alex", "casey", "emery", "river"], "count": 4}
        ])
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert len(result) == 1
        assert result[0]["topic"] == "chapter 3 explanation"
        assert len(result[0]["speakers"]) >= 2

    def test_old_messages_outside_window_ignored(self, chat_dir):
        old = time.time() - 7200
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "wait did anyone explain chapter 3?", "timestamp": old},
            {"sender": "casey", "text": "did we ever get an answer about chapter 3?", "timestamp": old},
            {"sender": "emery", "text": "still wondering about chapter 3?", "timestamp": old},
        ])
        from src.nag_detector import detect_nag_pileons
        client = _mock_client([])
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert result == []
        client.messages.create.assert_not_called()

    def test_human_messages_excluded(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "Travis", "text": "what about chapter 3?"},
            {"sender": "Travis", "text": "seriously chapter 3?"},
            {"sender": "Travis", "text": "hello? chapter 3?"},
        ])
        from src.nag_detector import detect_nag_pileons
        client = _mock_client([])
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert result == []
        client.messages.create.assert_not_called()

    def test_single_speaker_nag_filtered_out(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "what about chapter 3?"},
            {"sender": "casey", "text": "nice weather today"},
            {"sender": "emery", "text": "anyone want coffee"},
        ])
        from src.nag_detector import detect_nag_pileons
        client = _mock_client([
            {"topic": "chapter 3", "speakers": ["alex"], "count": 1}
        ])
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert result == []

    def test_llm_error_fails_open(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "what about chapter 3?"},
            {"sender": "casey", "text": "yeah chapter 3?"},
            {"sender": "emery", "text": "chapter 3 anyone?"},
        ])
        from src.nag_detector import detect_nag_pileons
        client = AsyncMock()
        client.messages.create.side_effect = Exception("API down")
        result = asyncio.run(detect_nag_pileons(client, BOT_NAMES))
        assert result == []

    def test_render_block_empty_when_no_nags(self, chat_dir):
        from src.nag_detector import render_overasked_block
        client = _mock_client([])
        block = asyncio.run(render_overasked_block(client, BOT_NAMES))
        assert block == ""

    def test_render_block_formats_nags(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "what about chapter 3?"},
            {"sender": "casey", "text": "yeah chapter 3?"},
            {"sender": "emery", "text": "chapter 3 anyone?"},
        ])
        from src.nag_detector import render_overasked_block
        client = _mock_client([
            {"topic": "chapter 3 explanation", "speakers": ["alex", "casey"], "count": 3}
        ])
        block = asyncio.run(render_overasked_block(client, BOT_NAMES))
        assert "chapter 3 explanation" in block
        assert "alex" in block and "casey" in block
        assert "3x" in block
