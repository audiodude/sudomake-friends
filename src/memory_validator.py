"""Validate that proposed memory entries don't contaminate a friend's identity."""

import json
import logging
import re

import anthropic

logger = logging.getLogger(__name__)

VALIDATOR_MODEL = "claude-haiku-4-5-20251001"

VALIDATOR_PROMPT = """You are checking a memory entry that {friend_name} is about to save about themselves.

{friend_name}'s profile:
{soul}

Other people in {friend_name}'s life (by name only): {other_names}

Proposed memory to save:
"{memory}"

A memory is VALID if:
- It's plausibly about {friend_name} based on their profile
- It's clearly third-person about someone else (e.g. "alex is sick this week")
- It describes an interaction {friend_name} had

A memory is INVALID if:
- It implicitly claims {friend_name} owns something, does a hobby, or has a trait that contradicts their profile (e.g. {friend_name} is a potter, but the memory says "need to use my synth")
- It's ambiguous first-person that would make {friend_name} think they did something another person actually did

Respond with JSON only:
{{"valid": true/false, "reason": "one short sentence"}}
"""


async def validate_memory(
    client: anthropic.AsyncAnthropic,
    friend_name: str,
    soul: str,
    proposed_memory: str,
    other_names: list[str] | None = None,
) -> tuple[bool, str]:
    """Check whether a proposed memory update is consistent with the friend's identity.

    Fails open — on any error, returns (True, "...") so a validator glitch
    never drops a legitimate memory.
    """
    prompt = VALIDATOR_PROMPT.format(
        friend_name=friend_name,
        soul=soul,
        other_names=", ".join(other_names) if other_names else "(none listed)",
        memory=proposed_memory,
    )

    try:
        response = await client.messages.create(
            model=VALIDATOR_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"[{friend_name}] memory validator call failed, allowing write: {e}")
        return True, "validator call failed"

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
                return True, "validator parse error"
        else:
            return True, "validator parse error"

    return bool(result.get("valid", True)), str(result.get("reason", ""))
