"""Read-only historical inputs captured through the real runtime brain."""

from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from unittest.mock import patch

import yaml

from src import brain, chat_history, config, schedule
from src.chat_history import ChatMessage
from src.topics import TTL_HOURS


DEFAULT_HOME = Path.home() / ".sudomake-friends"
_CONFIG_KEYS = ("timezone", "chattiness", "jokiness", "whininess", "work_type", "bot_reply_chance")
_SCHEDULE_KEYS = ("wake_up", "sleep_at", "work_start", "work_end", "days_off")


@dataclass(frozen=True)
class _TextSnapshot:
    text: str | None = None
    mtime: float | None = None

    def exists(self) -> bool:
        return self.text is not None

    def read_text(self) -> str:
        if self.text is None:
            raise FileNotFoundError("No text in this frozen snapshot")
        return self.text

    def at(self, timestamp: float) -> "_TextSnapshot":
        if self.mtime is not None and self.mtime <= timestamp:
            return self
        return _TextSnapshot()

    def state_at(self, timestamp: float) -> str:
        if self.text is None:
            return "absent"
        return "included_by_snapshot_mtime" if self.at(timestamp).exists() else "omitted_future_snapshot"


@dataclass(frozen=True)
class _Friend:
    name: str
    soul: str
    memory: str
    settings: dict
    news: _TextSnapshot


@dataclass(frozen=True)
class _Snapshot:
    friends: tuple[_Friend, ...]
    messages: tuple[ChatMessage, ...]
    source_lines: tuple[int, ...]
    summary: _TextSnapshot
    history: _TextSnapshot
    topics: tuple[tuple[float, float, str, str, str], ...]


@dataclass(frozen=True)
class _Scenario:
    friend: _Friend
    index: int
    kind: str
    timestamp: float
    silence_minutes: int | None = None


class _PromptCaptured(BaseException):
    """Stop before parsing a model response or reaching any runtime mutation."""

    def __init__(self, messages: list[dict], max_tokens: int):
        self.messages = messages
        self.max_tokens = max_tokens


class _CaptureClient:
    def __init__(self, expected_label: str):
        self.expected_label = expected_label

    async def complete(self, *, messages: list[dict], max_tokens: int, label: str, **kwargs):
        if label != self.expected_label:
            raise RuntimeError(f"Unexpected helper request during capture: {label}")
        raise _PromptCaptured(messages, max_tokens)


def _read_text(path: Path, *, required: bool = False) -> _TextSnapshot:
    try:
        stat = path.stat()
        return _TextSnapshot(path.read_text(encoding="utf-8"), stat.st_mtime)
    except FileNotFoundError:
        if required:
            raise ValueError(f"Missing {path}; initialize friends and retain data/CHAT.jsonl before comparing.") from None
        return _TextSnapshot()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read {path}: {type(exc).__name__}. Check file access and UTF-8 encoding.") from None


def _load_snapshot(home: Path) -> _Snapshot:
    friends_dir = home / "friends"
    if not friends_dir.is_dir():
        raise ValueError(f"Missing friends directory {friends_dir}; run the setup wizard or pass --home.")
    friends = []
    for folder in sorted(friends_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if not (folder / "SOUL.md").exists() and not (folder / "config.yaml").exists():
            continue
        soul = _read_text(folder / "SOUL.md", required=True).read_text()
        config_path = folder / "config.yaml"
        config_text = _read_text(config_path, required=True).read_text()
        try:
            raw = yaml.safe_load(config_text)
        except yaml.YAMLError:
            raise ValueError(f"Invalid YAML in {config_path}; fix the friend configuration before comparing.") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("schedule", {}), dict):
            raise ValueError(f"Expected a mapping and schedule mapping in {config_path}.")
        # Never resolve or retain Telegram credentials, even in the in-memory settings.
        settings = {key: raw[key] for key in _CONFIG_KEYS if key in raw}
        settings["schedule"] = {key: raw["schedule"][key] for key in _SCHEDULE_KEYS if key in raw.get("schedule", {})}
        settings = config._resolve_dict(settings)
        friends.append(_Friend(
            folder.name, soul,
            _read_text(home / "data" / "memories" / folder.name / "MEMORY.md").text or "",
            settings, _read_text(home / "data" / "news" / f"{folder.name}.md"),
        ))
    if not friends:
        raise ValueError(f"No configured friends in {friends_dir}; initialize at least one friend first.")

    chat_path = home / "data" / "CHAT.jsonl"
    chat = _read_text(chat_path, required=True).read_text()
    rows = []
    for line_number, line in enumerate(chat.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            timestamp = float(row["timestamp"])
            if not math.isfinite(timestamp) or timestamp <= 0:
                raise ValueError("invalid timestamp")
            if not isinstance(row["sender"], str) or not row["sender"].strip() or not isinstance(row["text"], str):
                raise ValueError("invalid sender or text")
            if not isinstance(row.get("is_reaction", False), bool):
                raise ValueError("invalid reaction flag")
            message = ChatMessage(
                timestamp, row["sender"], row["text"],
                int(row.get("message_id", 0)), int(row.get("reply_to", 0)), row.get("is_reaction", False),
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ValueError(f"Invalid chat record at {chat_path}:{line_number}; repair this JSONL row before comparing.") from None
        rows.append((message, line_number))
    rows.sort(key=lambda item: (item[0].timestamp, item[1]))
    if not any(not message.is_reaction and message.text.strip() for message, _ in rows):
        raise ValueError(f"No text messages in {chat_path}; retain real conversation history before comparing.")

    topics = []
    topic_text = _read_text(home / "data" / "RECENT_TOPICS.md").text or ""
    for line in topic_text.splitlines():
        try:
            timestamp_text, rest = line.split(" ", 1)
            timestamp = datetime.fromisoformat(timestamp_text).timestamp()
            kind = "topic"
            if rest.startswith("["):
                kind, rest = rest[1:].split("]", 1)
            sender, value = rest.strip().split(":", 1)
            # Runtime topic timestamps have minute precision. Defer the whole minute
            # rather than leak a later addition into an earlier message in that minute.
            available_at = timestamp + (60 if len(timestamp_text) == 16 else 0)
            topics.append((timestamp, available_at, kind.strip(), sender.strip(), value.strip()))
        except (ValueError, OverflowError, OSError):
            continue
    return _Snapshot(
        tuple(friends), tuple(message for message, _ in rows), tuple(line for _, line in rows),
        _read_text(home / "data" / "CHAT_SUMMARY.md"),
        _read_text(friends_dir / "HISTORY.md"), tuple(topics),
    )


def _frozen_datetime(timestamp: float):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(timestamp, tz)
    return FrozenDateTime


def _availability(friend: _Friend, timestamp: float) -> dict:
    try:
        with patch.object(schedule, "datetime", _frozen_datetime(timestamp)):
            return schedule.get_availability(friend.settings)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise ValueError(f"Invalid schedule/timezone/personality settings for friend {friend.name}: {type(exc).__name__}.") from None


def _quotas(capacities: dict[str, int], count: int, initial: dict[str, int] | None = None) -> dict[str, int]:
    assigned = dict.fromkeys(capacities, 0)
    initial = initial or {}
    for _ in range(min(count, sum(capacities.values()))):
        available = [key for key in capacities if assigned[key] < capacities[key]]
        key = min(available, key=lambda name: (assigned[name] + initial.get(name, 0), name))
        assigned[key] += 1
    return assigned


def _spread(items: list, count: int) -> list:
    if not count:
        return []
    return [items[((2 * index + 1) * len(items)) // (2 * count)] for index in range(count)]


def _grouped_sample(items: list[_Scenario], count: int, group) -> list[_Scenario]:
    groups = defaultdict(list)
    for item in items:
        groups[group(item)].append(item)
    quotas = _quotas({key: len(values) for key, values in groups.items()}, count)
    selected = [item for key, values in groups.items() for item in _spread(values, quotas[key])]
    return sorted(selected, key=lambda item: (item.timestamp, item.index))


def _reply_tags(snapshot: _Snapshot, index: int) -> list[str]:
    """Describe observable text features, never semantic quality or nag results."""
    message = snapshot.messages[index]
    words = re.findall(r"[\w']+", message.text.casefold())
    tags = []
    if "?" in message.text or re.match(r"^(who|what|when|where|why|how|did|do|does|can|could|is|are|have|has)\b", message.text.casefold()):
        tags.append("question_word_or_mark")
    if re.search(r"\b(i|i'm|i've|my|mine)\b", message.text.casefold()):
        tags.append("first_person_attribution")
    if len(words) <= 8:
        tags.append("short_message")
    if message.reply_to:
        tags.append("reply_thread")
    if re.search(r"\b(lol|haha|lmao)\b|😂|🤣", message.text.casefold()):
        tags.append("humor_marker")
    if '"' in message.text:
        tags.append("quoted_phrase")
    phrases = {tuple(words[offset:offset + 4]) for offset in range(len(words) - 3)}
    if phrases:
        for earlier in reversed(snapshot.messages[:index]):
            if earlier.timestamp < message.timestamp - 3600:
                break
            if earlier.is_reaction or earlier.sender == message.sender:
                continue
            old_words = re.findall(r"[\w']+", earlier.text.casefold())
            old_phrases = {tuple(old_words[offset:offset + 4]) for offset in range(len(old_words) - 3)}
            if phrases & old_phrases:
                tags.append("repeated_four_word_phrase_across_speakers")
                break
    return tags


def _diverse_replies(
    snapshot: _Snapshot, items: list[_Scenario], count: int,
    used_indices: set[int], covered_tags: set[str],
) -> list[_Scenario]:
    names = {friend.name.casefold() for friend in snapshot.friends}
    groups = defaultdict(list)
    for item in items:
        kind = "bot" if snapshot.messages[item.index].sender.casefold() in names else "human"
        groups[kind].append(item)
    quotas = _quotas({key: len(values) for key, values in groups.items()}, count)
    selected = []
    for key, candidates in groups.items():
        remaining = list(enumerate(candidates))
        for slot in range(quotas[key]):
            target = ((2 * slot + 1) * len(candidates)) / (2 * quotas[key])
            position, chosen = max(
                remaining,
                key=lambda pair: (
                    pair[1].index not in used_indices,
                    len(set(_reply_tags(snapshot, pair[1].index)) - covered_tags),
                    -abs(pair[0] - target),
                    -pair[1].index,
                ),
            )
            remaining.remove((position, chosen))
            selected.append(chosen)
            used_indices.add(chosen.index)
            covered_tags.update(_reply_tags(snapshot, chosen.index))
    return sorted(selected, key=lambda item: (item.timestamp, item.index))


def _select(snapshot: _Snapshot, count: int) -> list[_Scenario]:
    replies = {friend.name: [] for friend in snapshot.friends}
    initiations = {friend.name: [] for friend in snapshot.friends}
    for index, message in enumerate(snapshot.messages):
        if not message.is_reaction and message.text.strip():
            for friend in snapshot.friends:
                if friend.name.casefold() != message.sender.casefold():
                    replies[friend.name].append(_Scenario(friend, index, "reply", message.timestamp))
        if index == 0:
            continue
        previous = snapshot.messages[index - 1]
        silence = int((message.timestamp - previous.timestamp) / 60)
        if silence < 5:
            continue
        for friend in snapshot.friends:
            if _availability(friend, message.timestamp)["awake"]:
                initiations[friend.name].append(_Scenario(friend, index, "initiate", message.timestamp, silence))

    initiation_goal = (count * 6 + 12) // 25
    init_quotas = _quotas({key: len(values) for key, values in initiations.items()}, initiation_goal)
    selected_inits = {}
    for name, candidates in initiations.items():
        selected_inits[name] = _grouped_sample(
            candidates, init_quotas[name],
            lambda item: "short" if item.silence_minutes < 60 else "medium" if item.silence_minutes < 360 else "stale",
        )
    reply_goal = count - sum(init_quotas.values())
    reply_quotas = _quotas({key: len(values) for key, values in replies.items()}, reply_goal, init_quotas)
    selected_replies = {}
    used_reply_indices = set()
    covered_tags = set()
    for name, candidates in replies.items():
        selected_replies[name] = _diverse_replies(
            snapshot, candidates, reply_quotas[name], used_reply_indices, covered_tags,
        )
    missing = count - sum(reply_quotas.values()) - sum(init_quotas.values())
    if missing:
        # A tiny log can still supply distinct, real initiation opportunities.
        used = {(item.friend.name, item.index) for values in selected_inits.values() for item in values}
        extras = {name: [item for item in values if (name, item.index) not in used] for name, values in initiations.items()}
        extra_quotas = _quotas(
            {key: len(values) for key, values in extras.items()}, missing,
            {name: reply_quotas[name] + init_quotas[name] for name in replies},
        )
        for name, values in extras.items():
            selected_inits[name].extend(_spread(values, extra_quotas[name]))
        missing -= sum(extra_quotas.values())
    if missing:
        available = count - missing
        suggestion = f"request --count {available} or retain more chat history" if available else "retain more non-self text messages or awake quiet gaps"
        raise ValueError(f"Only {available} distinct eligible real-data cases are available; {suggestion}.")
    # Interleave friends, with replies first and initiation opportunities last.
    result = []
    for selected in (selected_replies, selected_inits):
        for slot in range(max(map(len, selected.values()), default=0)):
            result.extend(values[slot] for values in selected.values() if slot < len(values))
    return result


def _forbid_mutation(*args, **kwargs):
    raise RuntimeError("Runtime mutation is forbidden during offline prompt capture")


async def _no_nag_classifier(*args, **kwargs) -> str:
    return ""


async def _forbid_helper(*args, **kwargs):
    raise RuntimeError("Post-response helper is forbidden during offline prompt capture")


async def _capture(snapshot: _Snapshot, scenario: _Scenario) -> dict:
    friend = scenario.friend
    trigger = snapshot.messages[scenario.index]
    end = scenario.index + (scenario.kind == "reply")
    prior = snapshot.messages[:end]
    timestamp = scenario.timestamp
    current_topics = defaultdict(list)
    for recorded_at, available_at, kind, sender, value in snapshot.topics:
        if timestamp - TTL_HOURS * 3600 < recorded_at and available_at <= timestamp:
            current_topics[kind].append(f"- {sender}: {value}")
    frozen_datetime = _frozen_datetime(timestamp)
    label = f"{'decide' if scenario.kind == 'reply' else 'initiate'}:{friend.name}"
    dependencies = {
        "load_friend_soul": lambda name: friend.soul,
        "load_friend_memory": lambda name: friend.memory,
        "load_history": lambda: snapshot.history.at(timestamp).text or "",
        "load_friend_news": lambda name: friend.news.at(timestamp).text or "",
        "get_friend_names": lambda: [item.name for item in snapshot.friends],
        "get_recent_topics": lambda: "\n".join(current_topics["topic"]),
        "get_recent_joke_formats": lambda: "\n".join(current_topics["joke"]),
        "get_recent_complaints": lambda: "\n".join(current_topics["complaint"]),
        "render_overasked_block": _no_nag_classifier,
        "last_message_age_seconds": lambda: timestamp - prior[-1].timestamp if prior else None,
        "load_messages": lambda limit=100: list(prior[-limit:]),
        "validate_memory": _forbid_helper,
        "save_friend_memory": _forbid_mutation,
        "_update_memory": _forbid_mutation,
        "record_topic": _forbid_mutation,
        "record_joke_format": _forbid_mutation,
        "record_complaint": _forbid_mutation,
    }
    with ExitStack() as stack:
        for name, replacement in dependencies.items():
            stack.enter_context(patch.object(brain, name, replacement))
        stack.enter_context(patch.object(chat_history, "load_messages", lambda limit=100: list(prior[-limit:])))
        stack.enter_context(patch.object(chat_history, "CHAT_SUMMARY_PATH", snapshot.summary.at(timestamp)))
        stack.enter_context(patch.object(schedule, "datetime", frozen_datetime))
        # maybe_initiate imports datetime inside the function, unlike schedule.
        stack.enter_context(patch("datetime.datetime", frozen_datetime))
        context_excerpt = chat_history.get_chat_context(limit=12)
        client = _CaptureClient(label)
        try:
            if scenario.kind == "reply":
                await brain.think_and_respond(
                    client, "offline-capture", friend.name, trigger.sender,
                    trigger.text, trigger.message_id, friend.settings,
                )
            else:
                await brain.maybe_initiate(
                    client, "offline-capture", friend.name, friend.settings, scenario.silence_minutes,
                )
        except _PromptCaptured as captured:
            messages = captured.messages
            max_tokens = captured.max_tokens
        else:
            raise ValueError(f"No prompt produced for {scenario.kind} case for {friend.name}; check the historical schedule.")

    identity = f"{scenario.kind}\0{friend.name}\0{timestamp!r}\0{trigger.message_id}\0{trigger.sender}\0{trigger.text}"
    case_id = f"{scenario.kind}-{friend.name}-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
    if scenario.kind == "reply":
        excerpt = f"{context_excerpt}\n\nLatest message from {trigger.sender} [msg:{trigger.message_id}]: {trigger.text}"
    else:
        excerpt = f"Initiation opportunity after {scenario.silence_minutes} minutes of silence.\n\n{context_excerpt}"
    metadata = {
        "chat_source": "data/CHAT.jsonl",
        "source_line": snapshot.source_lines[scenario.index],
        "sender": trigger.sender if scenario.kind == "reply" else None,
        "sender_kind": ("bot" if trigger.sender.casefold() in {item.name.casefold() for item in snapshot.friends} else "human") if scenario.kind == "reply" else None,
        "message_id": trigger.message_id if scenario.kind == "reply" else None,
        "silence_minutes": scenario.silence_minutes,
        "prior_message_count": len(prior),
        "text_feature_tags": _reply_tags(snapshot, scenario.index) if scenario.kind == "reply" else ["quiet_gap"],
        "tag_method": "Observable lexical features only; repeated phrases are exact four-word overlaps across speakers in the prior hour, not semantic nag/joke classification.",
        "summary": snapshot.summary.state_at(timestamp),
        "news": friend.news.state_at(timestamp),
        "shared_history": snapshot.history.state_at(timestamp),
        "limitations": [
            "Current saved SOUL.md, config and memory are reused, not historical versions; memory can contain hindsight from after this excerpt.",
            "Summary, shared history and news use only the current snapshot when its filesystem mtime is not after the case; earlier versions cannot be reconstructed and mtimes are only availability proxies.",
            "Topics include only the preceding rolling window; minute-resolution entries are deferred until that minute ends. Naive topic timestamps use the runner's local timezone; previously pruned entries cannot be restored.",
            "Nag-classifier context is omitted for every model: no historical classifier output is stored and no helper calls are made.",
            "Only retained CHAT.jsonl text is replayed; images and fetched link previews are not reconstructed. Reactions are context, never reply triggers.",
            "Initiations are opportunities at the end of real logged quiet gaps, immediately before the next event; they are not claims that a bot actually attempted an initiation then.",
            "Probability gates are not replayed; these cases compare the runtime LLM decision prompts, not end-to-end chat frequency.",
        ],
    }
    # Check serializability and detach mutable request blocks from runtime locals.
    return json.loads(json.dumps({
        "case_id": case_id, "kind": scenario.kind, "friend": friend.name,
        "timestamp": timestamp, "input_excerpt": excerpt,
        "messages": messages, "max_tokens": max_tokens, "metadata": metadata,
    }, allow_nan=False))


async def build_cases(home: Path = DEFAULT_HOME, count: int = 25) -> list[dict]:
    """Capture balanced deterministic cases without clients, helpers or data writes."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Case count must be a positive integer.")
    snapshot = _load_snapshot(Path(home).expanduser())
    scenarios = _select(snapshot, count)
    return [await _capture(snapshot, scenario) for scenario in scenarios]
