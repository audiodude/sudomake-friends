"""Track recently discussed topics, joke formats, and complaint topics to prevent repetition."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

logger = logging.getLogger(__name__)

TOPICS_PATH = DATA_DIR / "RECENT_TOPICS.md"
TTL_HOURS = 24


def _append(kind: str, sender: str, value: str):
    _read_fresh(prune=True)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    line = f"{timestamp} [{kind}] {sender}: {value}\n"
    with open(TOPICS_PATH, "a") as f:
        f.write(line)
    logger.debug(f"Recorded {kind}: {sender} — {value}")


def record_topic(sender: str, topic: str):
    _append("topic", sender, topic)


def record_joke_format(sender: str, fmt: str):
    _append("joke", sender, fmt)


def record_complaint(sender: str, complaint: str):
    _append("complaint", sender, complaint)


def _read_fresh(*, prune: bool = False) -> list[tuple[str, str, str]]:
    """Return fresh (kind, sender, value) entries; prune only when committing."""
    if not TOPICS_PATH.exists():
        return []

    lines = [l for l in TOPICS_PATH.read_text().strip().split("\n") if l.strip()]
    if not lines:
        return []

    cutoff = datetime.now() - timedelta(hours=TTL_HOURS)
    fresh_lines = []
    parsed = []
    for line in lines:
        try:
            ts_str, rest = line.split(" ", 1)
            ts = datetime.fromisoformat(ts_str)
            if ts <= cutoff:
                continue
            fresh_lines.append(line)
            # rest looks like "[kind] sender: value"
            if rest.startswith("[") and "]" in rest:
                kind, after = rest[1:].split("]", 1)
                after = after.strip()
                if ":" in after:
                    sender, value = after.split(":", 1)
                    parsed.append((kind.strip(), sender.strip(), value.strip()))
            else:
                # Legacy entries without [kind] tag — treat as topic
                if ":" in rest:
                    sender, value = rest.split(":", 1)
                    parsed.append(("topic", sender.strip(), value.strip()))
        except (ValueError, IndexError):
            continue

    if prune and len(fresh_lines) != len(lines):
        TOPICS_PATH.write_text("\n".join(fresh_lines) + "\n" if fresh_lines else "")

    return parsed


def get_recent_topics(*, prune: bool = False) -> str:
    entries = [f"- {s}: {v}" for k, s, v in _read_fresh(prune=prune) if k == "topic"]
    return "\n".join(entries)


def get_recent_joke_formats(*, prune: bool = False) -> str:
    entries = [f"- {s}: {v}" for k, s, v in _read_fresh(prune=prune) if k == "joke"]
    return "\n".join(entries)


def get_recent_complaints(*, prune: bool = False) -> str:
    entries = [f"- {s}: {v}" for k, s, v in _read_fresh(prune=prune) if k == "complaint"]
    return "\n".join(entries)
