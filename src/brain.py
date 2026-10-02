"""The LLM-powered brain for each friend bot."""

import base64
import json
from pathlib import Path
import logging

from .llm import AsyncOpenRouter

logger = logging.getLogger(__name__)

from .config import load_friend_soul, load_friend_memory, save_friend_memory, load_history, get_friend_names
from .chat_history import get_chat_context, last_message_age_seconds, load_messages
from .echo_detector import is_echo, is_name_only, RECENT_MESSAGES_TO_CHECK
from .schedule import get_availability
from .topics import (
    get_recent_topics,
    get_recent_joke_formats,
    get_recent_complaints,
    record_topic,
    record_joke_format,
    record_complaint,
)
from .news import load_friend_news
from .memory_validator import validate_memory
from .nag_detector import render_overasked_block


def _describe_dials(friend_config: dict) -> str:
    """Turn numeric personality dials into prompt guidance."""
    jokiness = friend_config.get("jokiness", 0.5)
    whininess = friend_config.get("whininess", 0.3)

    if jokiness < 0.3:
        joke_line = f"Jokiness: {jokiness:.1f}/1.0 — you're dry, literal, sincere. You rarely crack jokes. When you do, it's understated."
    elif jokiness < 0.7:
        joke_line = f"Jokiness: {jokiness:.1f}/1.0 — you joke sometimes but don't perform. Earned laughs, not constant bits. Never setup-punchline comedy."
    else:
        joke_line = f"Jokiness: {jokiness:.1f}/1.0 — you're playful and quippy. BUT never setup-punchline stand-up bits. Your humor is in word choice and reactions, not formal joke structures."

    if whininess < 0.3:
        whine_line = f"Whininess: {whininess:.1f}/1.0 — you rarely complain. You tough things out or find the bright side. Complaining is out of character."
    elif whininess < 0.7:
        whine_line = f"Whininess: {whininess:.1f}/1.0 — you complain occasionally about real friction, but don't dwell and don't make it your whole personality."
    else:
        whine_line = f"Whininess: {whininess:.1f}/1.0 — you complain often, but VARY what you complain about. Don't make work your only subject. Check the recent complaints list — if you've been hitting the same well, pick something else."

    return f"{joke_line}\n{whine_line}"


_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(filename: str) -> str:
    """Load a prompt template from src/prompts/. The text lives in its own
    file so it can be edited directly without touching this module."""
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _split_cached_prompt(raw: str) -> tuple[str, str]:
    """Split a prompt on the ===RULES=== sentinel into (context, rules).

    The context half carries the per-call {placeholders} (name, soul, chat, ...)
    and is formatted per request. The rules half is byte-identical for every
    friend and every turn, so it goes in a cached `system` block — the friends
    re-send ~6K tokens of identical rules on every reply, and caching serves
    that prefix at ~10% cost instead of full price."""
    context, sep, rules = raw.partition("\n===RULES===\n")
    if not sep:
        raise ValueError("prompt is missing the ===RULES=== cache boundary")
    return context.rstrip(), rules.strip()


_DECIDE_CONTEXT, _DECIDE_RULES = _split_cached_prompt(_load_prompt("decide_and_respond.md"))


async def think_and_respond(
    client: AsyncOpenRouter,
    model: str,
    friend_name: str,
    sender: str,
    message: str,
    message_id: int,
    friend_config: dict,
    image_bytes: bytes | None = None,
    image_media_type: str | None = None,
    link_previews: str = "",
) -> dict | None:
    """Have a friend think about a message and optionally respond.

    Returns dict with keys: message, reply_to_message_id, memory_update, delay_seconds
    Or None if the friend decides not to respond.
    """
    soul = load_friend_soul(friend_name)
    memory = load_friend_memory(friend_name)
    history = load_history()
    chat_context = get_chat_context(limit=50)
    availability = get_availability(friend_config)
    news = load_friend_news(friend_name)
    recent_topics = get_recent_topics()
    recent_jokes = get_recent_joke_formats()
    recent_complaints = get_recent_complaints()
    bot_names = set(get_friend_names())
    overasked = await render_overasked_block(client, bot_names)
    overasked_block = (
        f"\n## Threads being beaten to death (DO NOT ask about ANY of these — the room is exhausted)\n{overasked}\n"
        if overasked else ""
    )

    status_parts = []
    if not availability["awake"]:
        status_parts.append("You're currently asleep (phone might wake you for important stuff)")
    elif availability["at_work"]:
        status_parts.append("You're at work right now — might be slower to respond")
    elif availability["day_off"]:
        status_parts.append("It's your day off — you're relaxed and available")
    else:
        status_parts.append("You're free right now")

    status_note = ". ".join(status_parts)

    if link_previews:
        link_preview_block = (
            "\n## Links shared in that message (fetched preview — use if relevant)\n"
            + link_previews
            + "\nYou can reference what the link actually says. Don't pretend you didn't see it, but also don't summarize it like a book report."
        )
    else:
        link_preview_block = ""

    prompt = _DECIDE_CONTEXT.format(
        name=friend_name,
        soul=soul,
        personality_dials=_describe_dials(friend_config),
        history=history if history else "(No shared history yet)",
        memory=memory if memory else "(No memories yet — this is a fresh start)",
        local_time=availability["local_time"],
        status_note=status_note,
        news=news if news else "(Nothing loaded yet)",
        recent_topics=recent_topics if recent_topics else "(None yet)",
        recent_jokes=recent_jokes if recent_jokes else "(None yet)",
        recent_complaints=recent_complaints if recent_complaints else "(None yet)",
        overasked_block=overasked_block,
        chat_context=chat_context,
        sender=sender,
        message=message,
        message_id=message_id,
        link_preview_block=link_preview_block,
    )

    if image_bytes and image_media_type:
        content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{image_media_type};base64,"
                        + base64.standard_b64encode(image_bytes).decode("ascii")
                    ),
                },
            },
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    response = await client.complete(
        model=model,
        max_tokens=1024,
        label=f"decide:{friend_name}",
        messages=[
            {
                "role": "system",
                "content": [{"type": "text", "text": _DECIDE_RULES,
                             "cache_control": {"type": "ephemeral"}}],
            },
            {"role": "user", "content": content},
        ],
    )

    raw = response.text.strip()

    # Parse JSON — handle potential markdown fencing
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # LLM sometimes wraps in extra text; try to extract JSON
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            return None

    if not result.get("respond"):
        return None

    # Handle memory update
    if result.get("memory_update"):
        proposed = result["memory_update"]
        other_names = [n for n in get_friend_names() if n != friend_name]
        valid, reason = await validate_memory(
            client, friend_name, soul, proposed, other_names=other_names
        )
        if valid:
            logger.info(f"[{friend_name}] Saving memory: {proposed[:80]}")
            _update_memory(friend_name, memory, proposed)
        else:
            logger.warning(f"[{friend_name}] Rejected memory: {proposed[:80]} — {reason}")

    # Handle topic tracking
    if result.get("topic"):
        record_topic(friend_name, result["topic"])
    if result.get("joke_format"):
        record_joke_format(friend_name, result["joke_format"])
    if result.get("complaint_topic"):
        record_complaint(friend_name, result["complaint_topic"])

    # Normalize messages — support both "message" (string) and "messages" (array)
    messages = result.get("messages") or []
    if not messages and result.get("message"):
        messages = [result["message"]]
    messages = [m for m in messages if m]

    # Echo filter: drop messages that parrot phrasing from recent chat.
    # Name-only filter: drop messages that are nothing but a participant's name.
    recent_msgs = [m for m in load_messages(RECENT_MESSAGES_TO_CHECK) if not m.is_reaction]
    recent_texts = [m.text for m in recent_msgs]
    participant_names = bot_names | {m.sender for m in recent_msgs}
    filtered = []
    for m in messages:
        if is_echo(m, recent_texts):
            logger.warning(f"[{friend_name}] Dropped echo: {m[:80]}")
        elif is_name_only(m, participant_names):
            logger.warning(f"[{friend_name}] Dropped name-only message: {m[:80]}")
        else:
            filtered.append(m)
    messages = filtered

    if not messages:
        return None

    return {
        "messages": messages,
        "reply_to_message_id": result.get("reply_to_message_id"),
        "delay_seconds": max(10, min(180, result.get("delay_seconds", 30))),
    }


INITIATE_PROMPT = _load_prompt("initiate.md")


async def maybe_initiate(
    client: AsyncOpenRouter,
    model: str,
    friend_name: str,
    friend_config: dict,
    silence_minutes: int,
) -> dict | None:
    """Give a friend the chance to start a conversation.

    Returns dict with keys: message, memory_update
    Or None if they decide not to.
    """
    soul = load_friend_soul(friend_name)
    memory = load_friend_memory(friend_name)
    history = load_history()
    chat_context = get_chat_context(limit=30)
    availability = get_availability(friend_config)
    news = load_friend_news(friend_name)
    recent_topics = get_recent_topics()
    recent_jokes = get_recent_joke_formats()
    recent_complaints = get_recent_complaints()
    bot_names = set(get_friend_names())
    overasked = await render_overasked_block(client, bot_names)
    overasked_block = (
        f"\n## Threads being beaten to death (DO NOT touch ANY of these — the room is exhausted)\n{overasked}\n"
        if overasked else ""
    )

    if not availability["awake"]:
        return None

    status_parts = []
    if availability["at_work"]:
        status_parts.append("You're at work right now")
    elif availability["day_off"]:
        status_parts.append("It's your day off — you're relaxed and available")
    else:
        status_parts.append("You're free right now")
    status_note = ". ".join(status_parts)

    if silence_minutes < 60:
        silence_duration = f"{silence_minutes} minutes"
    else:
        hours = silence_minutes / 60
        silence_duration = f"{hours:.1f} hours"

    # Stale-topic gate: if chat has been dormant for 6+ hours, treat topics as yesterday's news
    last_msg_age = last_message_age_seconds()
    if last_msg_age is not None and last_msg_age >= 6 * 3600:
        freshness_note = (
            "\nSTALE CHAT WARNING: The last message was hours ago — this is a fresh "
            "opening, not a continuation. The topics above are yesterday's news. Do NOT "
            "post a \"still thinking about [yesterday's thing]\" or summary-thought "
            "followup. If you want to say something, start something NEW: what you're "
            "doing today, a fresh observation, something mundane from your morning. "
            "Better still, say nothing."
        )
    else:
        freshness_note = ""

    # Time-aware context for variety
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(friend_config.get("timezone", "UTC").replace(" ", "_"))
    now = datetime.now(tz)
    day_of_week = now.strftime("%A")
    hour = now.hour
    if hour < 10:
        time_vibe = "Morning energy — coffee, getting started."
    elif hour < 13:
        time_vibe = "Midday — could be a work break, lunch thoughts."
    elif hour < 17:
        time_vibe = "Afternoon — the drag or the groove."
    elif hour < 20:
        time_vibe = "Evening — winding down, making plans, cooking."
    else:
        time_vibe = "Late night — couch mode, random thoughts, can't sleep."

    prompt = INITIATE_PROMPT.format(
        name=friend_name,
        soul=soul,
        personality_dials=_describe_dials(friend_config),
        history=history if history else "(No shared history yet)",
        memory=memory if memory else "(No memories yet)",
        local_time=availability["local_time"],
        status_note=status_note,
        news=news if news else "(Nothing loaded yet)",
        recent_topics=recent_topics if recent_topics else "(None yet)",
        recent_jokes=recent_jokes if recent_jokes else "(None yet)",
        recent_complaints=recent_complaints if recent_complaints else "(None yet)",
        overasked_block=overasked_block,
        chat_context=chat_context,
        silence_duration=silence_duration,
        day_of_week=day_of_week,
        time_vibe=time_vibe,
        freshness_note=freshness_note,
    )

    response = await client.complete(
        model=model,
        max_tokens=512,
        label=f"initiate:{friend_name}",
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            return None

    if not result.get("send"):
        return None

    if result.get("memory_update"):
        proposed = result["memory_update"]
        other_names = [n for n in get_friend_names() if n != friend_name]
        valid, reason = await validate_memory(
            client, friend_name, soul, proposed, other_names=other_names
        )
        if valid:
            logger.info(f"[{friend_name}] Saving memory (initiate): {proposed[:80]}")
            _update_memory(friend_name, memory, proposed)
        else:
            logger.warning(f"[{friend_name}] Rejected memory (initiate): {proposed[:80]} — {reason}")

    if result.get("topic"):
        record_topic(friend_name, result["topic"])
    if result.get("joke_format"):
        record_joke_format(friend_name, result["joke_format"])
    if result.get("complaint_topic"):
        record_complaint(friend_name, result["complaint_topic"])

    messages = result.get("messages") or []
    if not messages and result.get("message"):
        messages = [result["message"]]
    messages = [m for m in messages if m]

    # Echo filter: drop messages that parrot phrasing from recent chat.
    # Name-only filter: drop messages that are nothing but a participant's name.
    recent_msgs = [m for m in load_messages(RECENT_MESSAGES_TO_CHECK) if not m.is_reaction]
    recent_texts = [m.text for m in recent_msgs]
    participant_names = set(get_friend_names()) | {m.sender for m in recent_msgs}
    filtered = []
    for m in messages:
        if is_echo(m, recent_texts):
            logger.warning(f"[{friend_name}] Dropped echo (initiate): {m[:80]}")
        elif is_name_only(m, participant_names):
            logger.warning(f"[{friend_name}] Dropped name-only message (initiate): {m[:80]}")
        else:
            filtered.append(m)
    messages = filtered

    return {"messages": messages} if messages else None


def _update_memory(friend_name: str, current_memory: str, new_note: str):
    """Append a memory note, keeping the file manageable."""
    import time
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- [{timestamp}] {new_note}"

    if current_memory.strip():
        updated = current_memory.rstrip() + "\n" + entry
    else:
        updated = f"# Memory\n{entry}"

    # Rough size check — if memory is getting huge, we'd want compaction
    # For now, just cap at ~50 entries
    lines = updated.split("\n")
    memory_lines = [l for l in lines if l.startswith("- [")]
    if len(memory_lines) > 50:
        # Keep header + last 30 memories
        header = [l for l in lines if not l.startswith("- [")]
        updated = "\n".join(header + memory_lines[-30:])

    save_friend_memory(friend_name, updated)
