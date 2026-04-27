"""Tests for the nag detector — must catch the chapter-3 pile-on pattern
without false-flagging normal conversation."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def chat_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a fresh tmp dir before any module imports it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force reimport so DATA_DIR re-evaluates against the patched env
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


class TestNagDetector:
    def test_empty_chat_returns_empty(self, chat_dir):
        from src.nag_detector import get_overasked_terms
        assert get_overasked_terms() == []

    def test_single_question_not_flagged(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "did anyone watch the game?"},
        ])
        from src.nag_detector import get_overasked_terms
        assert get_overasked_terms() == []

    def test_pile_on_is_flagged(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "did anyone explain chapter 3 yet?"},
            {"sender": "casey", "text": "wait did alex ever explain chapter 3?"},
            {"sender": "emery", "text": "still wondering about chapter 3"},
            {"sender": "river", "text": "yeah did we ever get an answer about chapter 3?"},
        ])
        from src.nag_detector import get_overasked_terms
        results = get_overasked_terms()
        terms = {t for t, _, _ in results}
        assert "chapter" in terms
        chapter_entry = next(r for r in results if r[0] == "chapter")
        # ≥3 distinct askers, ≥4 total mentions
        assert len(chapter_entry[2]) >= 3
        assert chapter_entry[1] >= 4

    def test_old_messages_outside_window_ignored(self, chat_dir):
        old = time.time() - 7200  # 2 hours ago
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "wait did anyone explain chapter 3?", "timestamp": old},
            {"sender": "casey", "text": "did we ever get an answer about chapter 3?", "timestamp": old},
            {"sender": "emery", "text": "still wondering about chapter 3?", "timestamp": old},
        ])
        from src.nag_detector import get_overasked_terms
        assert get_overasked_terms() == []

    def test_non_question_messages_not_counted(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "chapter 3 is going great"},
            {"sender": "casey", "text": "love that chapter 3 vibe"},
            {"sender": "emery", "text": "chapter 3 is the best"},
        ])
        from src.nag_detector import get_overasked_terms
        assert get_overasked_terms() == []

    def test_reactions_excluded(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "❤️", "is_reaction": True, "reply_to": 1},
            {"sender": "casey", "text": "wait did we get chapter 3?", "is_reaction": False},
        ])
        from src.nag_detector import get_overasked_terms
        assert get_overasked_terms() == []  # only one asker

    def test_render_block_empty_when_no_overasked(self, chat_dir):
        from src.nag_detector import render_overasked_block
        assert render_overasked_block() == ""

    def test_render_block_lists_terms(self, chat_dir):
        _write_chat(chat_dir, [
            {"sender": "alex", "text": "wait did anyone watch the chapter?"},
            {"sender": "casey", "text": "did we ever cover the chapter?"},
        ])
        from src.nag_detector import render_overasked_block
        block = render_overasked_block()
        assert "chapter" in block
        assert "alex" in block and "casey" in block
        assert "2x" in block  # asked twice
