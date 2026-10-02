"""Offline cases preserve historical boundaries without touching runtime state."""

import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.model_compare import cases
from src import brain, chat_history, config, nag_detector, schedule
from src.llm import AsyncOpenRouter


BASE = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc).timestamp()


def _write_chat(home, rows):
    path = home / "data" / "CHAT.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _home(tmp_path, rows=None):
    home = tmp_path / "installed"
    for name in ("alex", "casey", "river"):
        folder = home / "friends" / name
        folder.mkdir(parents=True)
        (folder / "SOUL.md").write_text(f"{name} makes pottery. SOUL_SNAPSHOT_{name}")
        (folder / "config.yaml").write_text(
            "timezone: UTC\n"
            "schedule:\n  wake_up: '00:00'\n  sleep_at: '23:59'\n"
            "  work_start: '09:00'\n  work_end: '17:00'\n  days_off: [5, 6]\n"
            "telegram_token: DO_NOT_EXPORT_TOKEN\n"
        )
    if rows is None:
        rows = []
        for index in range(40):
            sender = ("Travis", "alex", "casey", "river")[index % 4]
            phrases = (
                "How did your appointment go",
                "I make pottery at the weekend",
                "that's good lol",
                "can you bring the blue bowl",
                "we can bring the blue bowl",
            )
            rows.append({
                "timestamp": BASE + index * 600 + (7 * 3600 if index >= 20 else 0),
                "sender": sender, "text": f"{phrases[index % len(phrases)]} EVENT_{index:03}",
                "message_id": index + 1, "reply_to": index if index % 3 == 0 else 0,
                "is_reaction": index % 11 == 10,
            })
    _write_chat(home, rows)
    memory = home / "data" / "memories" / "alex" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("MEMORY_SNAPSHOT: Travis told me about a later appointment.")
    summary = home / "data" / "CHAT_SUMMARY.md"
    summary.write_text("FUTURE_SUMMARY_SECRET")
    os.utime(summary, (BASE + 1_000_000, BASE + 1_000_000))
    history = home / "friends" / "HISTORY.md"
    history.write_text("FUTURE_SHARED_HISTORY_SECRET")
    os.utime(history, (BASE + 1_000_000, BASE + 1_000_000))
    for name in ("alex", "casey", "river"):
        news = home / "data" / "news" / f"{name}.md"
        news.parent.mkdir(exist_ok=True)
        news.write_text("FUTURE_NEWS_SECRET")
        os.utime(news, (BASE + 1_000_000, BASE + 1_000_000))
    topic_time = datetime.fromtimestamp(BASE - 600).strftime("%Y-%m-%dT%H:%M")
    future_time = datetime.fromtimestamp(BASE + 1_000_000).strftime("%Y-%m-%dT%H:%M")
    (home / "data" / "RECENT_TOPICS.md").write_text(
        f"{topic_time} [topic] alex: PRIOR_TOPIC\n"
        f"{future_time} [topic] casey: FUTURE_TOPIC_SECRET\n"
    )
    return home, rows


def _prompt(case):
    parts = []
    for message in case["messages"]:
        content = message["content"]
        parts.append(content if isinstance(content, str) else "\n".join(block["text"] for block in content))
    return "\n".join(parts)


def test_requested_count_is_balanced_varied_deterministic_and_private(tmp_path):
    home, _ = _home(tmp_path)
    first = asyncio.run(cases.build_cases(home))
    second = asyncio.run(cases.build_cases(home))
    assert first == second
    assert len(first) == 25
    assert len({case["case_id"] for case in first}) == 25
    assert Counter(case["kind"] for case in first) == {"reply": 19, "initiate": 6}
    friend_counts = Counter(case["friend"] for case in first)
    assert max(friend_counts.values()) - min(friend_counts.values()) <= 1
    replies = [case for case in first if case["kind"] == "reply"]
    assert {case["metadata"]["sender_kind"] for case in replies} == {"human", "bot"}
    assert len({case["metadata"]["source_line"] for case in replies}) == 19
    tags = {tag for case in replies for tag in case["metadata"]["text_feature_tags"]}
    assert {"question_word_or_mark", "first_person_attribution", "short_message"} <= tags
    exported = json.dumps(first)
    assert "DO_NOT_EXPORT_TOKEN" not in exported
    assert "MEMORY_SNAPSHOT" in exported
    assert all(any("hindsight" in note for note in case["metadata"]["limitations"]) for case in first)


def test_prior_context_never_contains_following_events_or_future_snapshots(tmp_path):
    home, rows = _home(tmp_path)
    built = asyncio.run(cases.build_cases(home))
    for case in built:
        prompt = _prompt(case)
        index = case["metadata"]["source_line"] - 1
        end = index + (case["kind"] == "reply")
        for row in rows[end:]:
            assert row["text"] not in prompt
        assert "FUTURE_SUMMARY_SECRET" not in prompt
        assert "FUTURE_SHARED_HISTORY_SECRET" not in prompt
        assert "FUTURE_NEWS_SECRET" not in prompt
        assert "FUTURE_TOPIC_SECRET" not in prompt
        assert case["metadata"]["summary"] == "omitted_future_snapshot"
        assert case["metadata"]["news"] == "omitted_future_snapshot"
    assert "PRIOR_TOPIC" in _prompt(built[0])


def test_snapshot_availability_and_minute_precision_do_not_leak_into_earlier_case(tmp_path):
    rows = [
        {"timestamp": BASE + 20, "sender": "Travis", "text": "early boundary", "message_id": 1},
        {"timestamp": BASE + 80, "sender": "Travis", "text": "later boundary", "message_id": 2},
    ]
    home, _ = _home(tmp_path, rows=rows)
    topic_time = datetime.fromtimestamp(BASE).strftime("%Y-%m-%dT%H:%M")
    (home / "data" / "RECENT_TOPICS.md").write_text(f"{topic_time} [topic] alex: SAME_MINUTE_TOPIC\n")
    summary = home / "data" / "CHAT_SUMMARY.md"
    summary.write_text("AVAILABLE_SUMMARY")
    os.utime(summary, (BASE + 50, BASE + 50))
    for case in asyncio.run(cases.build_cases(home, count=6)):
        prompt = _prompt(case)
        if case["timestamp"] < BASE + 60:
            assert "SAME_MINUTE_TOPIC" not in prompt
            assert "AVAILABLE_SUMMARY" not in prompt
        else:
            assert "SAME_MINUTE_TOPIC" in prompt
            assert "AVAILABLE_SUMMARY" in prompt


def test_self_replies_and_reaction_triggers_are_excluded(tmp_path):
    home, rows = _home(tmp_path)
    built = asyncio.run(cases.build_cases(home, count=30))
    for case in built:
        if case["kind"] != "reply":
            continue
        trigger = rows[case["metadata"]["source_line"] - 1]
        assert trigger["sender"].casefold() != case["friend"].casefold()
        assert not trigger["is_reaction"]
        assert trigger["text"] in _prompt(case)


def test_initiations_use_real_gap_time_schedule_and_vary_silence(tmp_path):
    home, rows = _home(tmp_path)
    built = asyncio.run(cases.build_cases(home))
    initiations = [case for case in built if case["kind"] == "initiate"]
    assert any(case["metadata"]["silence_minutes"] >= 360 for case in initiations)
    for case in initiations:
        index = case["metadata"]["source_line"] - 1
        assert case["timestamp"] == rows[index]["timestamp"]
        assert case["metadata"]["silence_minutes"] == int((rows[index]["timestamp"] - rows[index - 1]["timestamp"]) / 60)
        prompt = _prompt(case)
        local = datetime.fromtimestamp(case["timestamp"], timezone.utc)
        assert local.strftime("%A") in prompt
        assert local.strftime("%H:%M") in prompt
        assert rows[index]["text"] not in prompt


def test_capture_has_no_writes_no_live_helpers_and_restores_patch_scopes(tmp_path, monkeypatch):
    home, _ = _home(tmp_path)
    before = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
    original_brain_reader = brain.load_friend_memory
    original_datetime = schedule.datetime
    original_history_reader = chat_history.load_messages
    original_nag_render = brain.render_overasked_block
    helper = AsyncMock(side_effect=AssertionError("live helper request"))
    monkeypatch.setattr(nag_detector, "detect_nag_pileons", helper)
    monkeypatch.setattr(AsyncOpenRouter, "complete", helper)
    monkeypatch.setattr(config, "_memory_path", lambda *args: pytest.fail("memory loader mkdir path reached"))
    original_open = Path.open

    def read_only_open(path, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in ("w", "a", "+", "x")), f"write attempted: {path}"
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as readonly:
        readonly.setattr(Path, "open", read_only_open)
        readonly.setattr(Path, "mkdir", lambda *args, **kwargs: pytest.fail("mkdir attempted"))
        built = asyncio.run(cases.build_cases(home))
    assert Counter(case["kind"] for case in built) == {"reply": 19, "initiate": 6}
    helper.assert_not_awaited()
    after = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
    assert before == after
    assert not (home / "data" / "memories" / "river").exists()
    assert brain.load_friend_memory is original_brain_reader
    assert schedule.datetime is original_datetime
    assert chat_history.load_messages is original_history_reader
    assert brain.render_overasked_block is original_nag_render


def test_single_snapshot_is_used_if_saved_data_changes_during_capture(tmp_path, monkeypatch):
    home, _ = _home(tmp_path)
    original_complete = cases._CaptureClient.complete
    changed = False

    async def change_saved_data_once(client, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            (home / "friends" / "alex" / "SOUL.md").write_text("CHANGED_SOUL_AFTER_SNAPSHOT")
            (home / "data" / "memories" / "alex" / "MEMORY.md").write_text("CHANGED_MEMORY_AFTER_SNAPSHOT")
            (home / "data" / "CHAT.jsonl").write_text("not JSON anymore")
        return await original_complete(client, **kwargs)

    monkeypatch.setattr(cases._CaptureClient, "complete", change_saved_data_once)
    built = asyncio.run(cases.build_cases(home))
    for case in built:
        if case["friend"] == "alex":
            assert "SOUL_SNAPSHOT_alex" in _prompt(case)
            assert "MEMORY_SNAPSHOT" in _prompt(case)
        assert "CHANGED_SOUL_AFTER_SNAPSHOT" not in _prompt(case)
        assert "CHANGED_MEMORY_AFTER_SNAPSHOT" not in _prompt(case)


def test_capture_failure_restores_readers_and_clock(tmp_path, monkeypatch):
    home, _ = _home(tmp_path)
    original_datetime = datetime
    original_schedule_datetime = schedule.datetime
    original_reader = brain.load_friend_memory

    async def fail_capture(*args, **kwargs):
        raise RuntimeError("capture interrupted")

    monkeypatch.setattr(cases._CaptureClient, "complete", fail_capture)
    with pytest.raises(RuntimeError, match="capture interrupted"):
        asyncio.run(cases.build_cases(home))
    import datetime as datetime_module
    assert datetime_module.datetime is original_datetime
    assert schedule.datetime is original_schedule_datetime
    assert brain.load_friend_memory is original_reader


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_case_count_is_actionable(tmp_path, count):
    with pytest.raises(ValueError, match="positive integer"):
        asyncio.run(cases.build_cases(tmp_path, count=count))


def test_missing_friends_is_actionable(tmp_path):
    with pytest.raises(ValueError, match="Missing friends directory.*setup wizard"):
        asyncio.run(cases.build_cases(tmp_path))


def test_missing_chat_is_actionable(tmp_path):
    home, _ = _home(tmp_path)
    (home / "data" / "CHAT.jsonl").unlink()
    with pytest.raises(ValueError, match="Missing .*CHAT.jsonl"):
        asyncio.run(cases.build_cases(home))


def test_reaction_only_chat_is_actionable(tmp_path):
    home, _ = _home(tmp_path, rows=[{
        "timestamp": BASE, "sender": "Travis", "text": "👍", "is_reaction": True,
    }])
    with pytest.raises(ValueError, match="No text messages"):
        asyncio.run(cases.build_cases(home))


def test_malformed_chat_error_does_not_echo_private_content(tmp_path):
    home, _ = _home(tmp_path)
    (home / "data" / "CHAT.jsonl").write_text('{"sender": "PRIVATE_SENTENCE", broken}\n')
    with pytest.raises(ValueError, match="Invalid chat record.*:1") as error:
        asyncio.run(cases.build_cases(home))
    assert "PRIVATE_SENTENCE" not in str(error.value)


def test_requested_count_cannot_duplicate_available_response_slots(tmp_path):
    home, _ = _home(tmp_path, rows=[{
        "timestamp": BASE, "sender": "Travis", "text": "hello", "message_id": 1,
    }])
    available = asyncio.run(cases.build_cases(home, count=3))
    assert len({item["case_id"] for item in available}) == 3
    with pytest.raises(ValueError):
        asyncio.run(cases.build_cases(home, count=4))
