"""Manage rolling chat history with compaction."""

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import anthropic

from .config import DATA_DIR

CHAT_PATH = DATA_DIR / "CHAT.jsonl"
CHAT_SUMMARY_PATH = DATA_DIR / "CHAT_SUMMARY.md"


@dataclass
class ChatMessage:
    timestamp: float
    sender: str          # friend name or human sender name
    text: str
    message_id: int = 0  # telegram message id
    reply_to: int = 0    # telegram message id being replied to
    is_reaction: bool = False  # emoji reaction, not a text message

    def display(self) -> str:
        if self.is_reaction:
            return f"{self.sender} reacted {self.text} to msg:{self.reply_to}"
        id_tag = f"[msg:{self.message_id}]" if self.message_id else ""
        prefix = f"{id_tag}[{self.sender}]"
        if self.reply_to:
            prefix += f" (replying to msg:{self.reply_to})"
        return f"{prefix}: {self.text}"


def append_message(msg: ChatMessage):
    """Append a message to the chat log."""
    with open(CHAT_PATH, "a") as f:
        f.write(json.dumps(asdict(msg)) + "\n")


def load_messages(limit: int = 100) -> list[ChatMessage]:
    """Load recent messages from the chat log."""
    if not CHAT_PATH.exists():
        return []
    lines = CHAT_PATH.read_text().strip().split("\n")
    lines = [l for l in lines if l.strip()]
    recent = lines[-limit:]
    return [ChatMessage(**json.loads(l)) for l in recent]


def last_message_age_seconds() -> float | None:
    """Age of the most recent chat message, in seconds. None if no messages."""
    if not CHAT_PATH.exists():
        return None
    last_ts = 0.0
    with open(CHAT_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last_ts = max(last_ts, float(json.loads(line).get("timestamp", 0)))
            except (ValueError, json.JSONDecodeError):
                continue
    if last_ts == 0.0:
        return None
    return time.time() - last_ts


def get_chat_context(limit: int = 50) -> str:
    """Build a chat context string for the LLM, including summary if available."""
    parts = []

    if CHAT_SUMMARY_PATH.exists():
        summary = CHAT_SUMMARY_PATH.read_text().strip()
        if summary:
            parts.append(f"## Earlier conversation summary\n{summary}\n")

    messages = load_messages(limit)
    if messages:
        parts.append("## Recent messages")
        for msg in messages:
            parts.append(msg.display())

    return "\n".join(parts) if parts else "(No chat history yet)"


async def maybe_compact(client: anthropic.AsyncAnthropic, model: str,
                        max_messages: int = 100, compact_to: int = 30):
    """If chat log exceeds max_messages, summarize old messages and trim."""
    if not CHAT_PATH.exists():
        return

    lines = CHAT_PATH.read_text().strip().split("\n")
    lines = [l for l in lines if l.strip()]

    if len(lines) <= max_messages:
        return

    # Split into old (to summarize) and recent (to keep)
    split_at = len(lines) - compact_to
    old_lines = lines[:split_at]
    recent_lines = lines[split_at:]

    old_messages = [ChatMessage(**json.loads(l)) for l in old_lines]
    old_text = "\n".join(m.display() for m in old_messages)

    # Load existing summary to incorporate
    existing_summary = ""
    if CHAT_SUMMARY_PATH.exists():
        existing_summary = CHAT_SUMMARY_PATH.read_text().strip()

    prompt = f"""Summarize this group chat history into a concise summary that captures:
- Key topics discussed
- Important facts shared by anyone
- Any plans, promises, or commitments made
- Emotional tone and relationship dynamics

Previous summary to incorporate:
{existing_summary}

New messages to summarize:
{old_text}

Write a concise summary (max 500 words). Focus on what would be useful context for future conversations."""

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    summary = response.content[0].text
    CHAT_SUMMARY_PATH.write_text(summary)

    # Rewrite chat log with only recent messages
    with open(CHAT_PATH, "w") as f:
        for line in recent_lines:
            f.write(line + "\n")
