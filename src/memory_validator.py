"""Validate that proposed memory entries don't contaminate a friend's identity."""

import json
import logging

from .llm import AsyncOpenRouter

logger = logging.getLogger(__name__)

VALIDATOR_PROMPT = """You are checking a memory entry that {friend_name} is about to save about themselves.

{friend_name}'s profile:
{soul}

Other people in {friend_name}'s life (by name only): {other_names}

Proposed memory to save:
"{memory}"

Actual attribution and delivery context (when supplied):
{attribution_context}
Check that the proposed fact is semantically supported by the quoted source and
the outgoing atoms, with the right subject. Exact substring evidence alone does
not prove ownership. Another person's first-person story is about THEM.
Accept new facts explicitly attributed to this friend by name or unambiguous
direct address as collaborative improv, even if not yet in their profile.
Direct address can be an ordinary unthreaded exchange without names: assess the
whole conversation. Do not accept facts aimed at another friend or ambiguous
pronouns, and do not infer an attribution merely from a generic question.

A memory is VALID if:
- It is supported by the supplied outgoing atoms and source, and is plausibly about {friend_name} based on their profile or an unambiguous direct attribution
- It is clearly third-person about someone else (e.g. "alex is sick this week"), supported by that person's actual words rather than stolen first-person phrasing
- It describes an interaction {friend_name} actually had in the supplied context

A memory is INVALID if:
- It implicitly claims {friend_name} owns something, does a hobby, or has a trait that contradicts their profile WITHOUT an explicit direct attribution to {friend_name} in the supplied context (e.g. another friend owns a synth and the potter copies "need to use my synth")
- It's ambiguous first-person that would make {friend_name} think they did something another person actually did

Respond with JSON only:
{{"valid": true/false, "reason": "one short sentence"}}
"""


async def validate_memory(
    client: AsyncOpenRouter,
    friend_name: str,
    soul: str,
    proposed_memory: str,
    other_names: list[str] | None = None,
    attribution_context: str = "",
) -> tuple[bool, str]:
    """Fail closed on unavailable, malformed, or identity-unsafe validation."""
    prompt = VALIDATOR_PROMPT.format(
        friend_name=friend_name,
        soul=soul,
        other_names=", ".join(other_names) if other_names else "(none listed)",
        memory=proposed_memory,
        attribution_context=attribution_context or "(No attribution context supplied)",
    )

    try:
        response = await client.complete(
            model=client.helper_model,
            max_tokens=200,
            label="memory_validate",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.text.strip()
    except Exception as e:
        logger.warning(f"[{friend_name}] memory validator call failed, rejecting write: {e}")
        return False, "validator call failed"

    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False, "validator parse error"
    if (type(result) is not dict or set(result) != {"valid", "reason"}
            or type(result["valid"]) is not bool or type(result["reason"]) is not str):
        return False, "validator malformed response"
    return result["valid"], result["reason"]
