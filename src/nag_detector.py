"""Detect threads being beaten to death by multiple bots — pile-on prevention.

Uses a fast LLM call (Haiku) to semantically cluster recent bot questions and
find topics being nagged about by multiple speakers.
"""

import json
import logging
import re
import time

import anthropic

from .chat_history import load_messages

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

NAG_WINDOW_SECONDS = 60 * 60

CLASSIFY_PROMPT = """Look at these recent group chat messages from bots. Identify any QUESTIONS or REQUESTS where multiple different speakers are asking about the same thing — even if worded differently.

Messages (last hour, bots only):
{messages}

Find clusters where 2+ different speakers are asking/nagging about the same unanswered question or topic. "did alex explain chapter 3" and "what actually happens in chapter 3" are the same nag. Only flag questions/requests, not general conversation on a shared topic.

Respond with JSON only (no markdown fencing):
{{"nags": [{{"topic": "short description", "speakers": ["name1", "name2"], "count": 3}}]}}

If no repeated nags, return {{"nags": []}}"""


async def detect_nag_pileons(
    client: anthropic.AsyncAnthropic,
    bot_names: set[str],
) -> list[dict]:
    """Find topics being nagged about by multiple bots.

    Returns list of {"topic": str, "speakers": list[str], "count": int}.
    Fails open — returns [] on any error.
    """
    messages = load_messages(limit=100)
    cutoff = time.time() - NAG_WINDOW_SECONDS

    bot_questions = [
        m for m in messages
        if m.timestamp >= cutoff
        and m.sender in bot_names
        and not m.is_reaction
    ]

    if len(bot_questions) < 3:
        return []

    formatted = "\n".join(
        f"[{msg.sender}]: {msg.text}" for msg in bot_questions
    )

    try:
        response = await client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(messages=formatted)}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"nag detector call failed, skipping: {e}")
        return []

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    nags = result.get("nags", [])
    return [n for n in nags if n.get("count", 0) >= 2 and len(n.get("speakers", [])) >= 2]


async def render_overasked_block(
    client: anthropic.AsyncAnthropic,
    bot_names: set[str],
) -> str:
    """Format detected nag pile-ons for prompt injection. Empty string if none."""
    nags = await detect_nag_pileons(client, bot_names)
    if not nags:
        return ""

    lines = []
    for nag in nags:
        speakers = ", ".join(nag["speakers"])
        lines.append(f"- \"{nag['topic']}\" — asked {nag['count']}x by {speakers}")
    return "\n".join(lines)
