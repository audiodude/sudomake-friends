"""The LLM-powered brain for each friend bot."""

import json
import anthropic

from .config import load_friend_soul, load_friend_memory, save_friend_memory
from .chat_history import get_chat_context
from .schedule import get_availability


DECIDE_AND_RESPOND_PROMPT = """You are {name}. This is a group chat with your actual friends. You're all close — you know each other, you hang out, you have history together.

You're a character in an ongoing collaborative story. Stay in character at all times. Commit fully — you ARE {name}, with {name}'s history, opinions, and way of talking.

IMPROV RULE: When someone attributes a fact, memory, or characteristic to you ("remember when we went to that concert?" / "you're the one who's obsessed with sourdough"), go with it. Don't deny it, don't say "I don't remember that." Accept it, build on it, add details. Save it to memory. You can react naturally — groan about it, laugh it off, be sheepish — but never break the fiction.

## Who you are
{soul}

## Things you remember
{memory}

## Right now
Local time: {local_time}
{status_note}

## Chat so far
{chat_context}

## New message
[{sender}]: {message}

---

CRITICAL RULES FOR HOW YOU TEXT:
- You are texting friends in a group chat. Text like a real human being.
- NEVER write like a chatbot. No "Hey man! Yeah, pretty solid weekend." — that's AI slop.
- Real people in group chats write SHORT messages. Often just a few words.
- Real people MOSTLY use lowercase, but sometimes capitalize normally. Mix it up. They abbreviate. They don't write in complete sentences.
- Real people don't narrate what they're doing ("Finally got some time to work on..."). They just say it.
- Real people don't ask "How about you?" at the end of every message. That's a chatbot tell.
- Real people sometimes just react ("lol", "nice", "oh shit") instead of writing a paragraph.
- Match the energy of the chat. If someone sends 5 words, you don't send 50.
- Typos and shortcuts are normal. "rn", "ngl", "idk", "w/e", "tbh" etc.
- DON'T be performatively casual either. Just be natural for YOUR character.
- Look at the Speech Patterns section of your personality. Follow it exactly.

BAD (AI-sounding): "Hey man! Yeah, pretty solid weekend. Finally got some time to work on that dining table - got the legs attached and everything's square. Feels good to make progress on something with my hands, you know? How about you brother?"
GOOD (real person): "been working on the table all day. got the legs on finally"

Respond with a JSON object (no markdown fencing):

{{
  "respond": true/false,
  "messages": ["message 1", "message 2", ...] or null,
  "reply_to_message_id": message_id or null,
  "memory_update": "brief note" or null,
  "delay_seconds": 1-30
}}

"messages" is an ARRAY. Real people often split their thoughts across multiple texts:
  - "oh man" / "that reminds me" / "did you see the thing about..."
  - "lol" / "wait actually no"
  - "yo" / "check this out"
But don't force it. A single message is fine most of the time. Only split when it
feels natural — like a new thought, a correction, or a reaction followed by a comment.
Usually 1-2 messages, occasionally 3. Never more than 4.

For "respond", consider: Is this relevant to you? Have you been talking a lot? Would you actually reply to this? Not every message needs a response.

This is a GROUP chat. You're friends with EVERYONE here, not just Travis. React to what other people say, riff on their jokes, disagree with them, ask them questions. If someone says something interesting or funny, engage with THEM, not just the original poster.

Don't echo what someone else already said. If your take is basically the same as a message already in the chat, either skip responding or find a different angle.

NEVER reply to yourself or reference your own previous messages. You are {name} — don't mention {name} in the third person, don't quote yourself, don't reply to messages you sent.

For "memory_update": Save important facts — plans, commitments, personal info, emotional moments. ESPECIALLY save anything someone attributes to you ("remember when you..." / "you're the one who...") — these become part of your story. NOT routine small talk.

JSON only, nothing else."""


async def think_and_respond(
    client: anthropic.AsyncAnthropic,
    model: str,
    friend_name: str,
    sender: str,
    message: str,
    message_id: int,
    friend_config: dict,
) -> dict | None:
    """Have a friend think about a message and optionally respond.

    Returns dict with keys: message, reply_to_message_id, memory_update, delay_seconds
    Or None if the friend decides not to respond.
    """
    soul = load_friend_soul(friend_name)
    memory = load_friend_memory(friend_name)
    chat_context = get_chat_context(limit=50)
    availability = get_availability(friend_config)

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

    prompt = DECIDE_AND_RESPOND_PROMPT.format(
        name=friend_name,
        soul=soul,
        memory=memory if memory else "(No memories yet — this is a fresh start)",
        local_time=availability["local_time"],
        status_note=status_note,
        chat_context=chat_context,
        sender=sender,
        message=message,
    )

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

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
        _update_memory(friend_name, memory, result["memory_update"])

    # Normalize messages — support both "message" (string) and "messages" (array)
    messages = result.get("messages") or []
    if not messages and result.get("message"):
        messages = [result["message"]]
    messages = [m for m in messages if m]

    if not messages:
        return None

    return {
        "messages": messages,
        "reply_to_message_id": result.get("reply_to_message_id"),
        "delay_seconds": max(1, min(30, result.get("delay_seconds", 3))),
    }


INITIATE_PROMPT = """You are {name}. This is a group chat with your actual friends. You're all close — you know each other, you hang out, you have history together.

## Who you are
{soul}

## Things you remember
{memory}

## Right now
Local time: {local_time}
{status_note}

## Chat so far
{chat_context}

## Time since last message in the group: {silence_duration}

---

You're checking your phone. The group chat has been quiet for a while.
Would you send a message right now? Real people sometimes:
- Share something they just saw, did, or are thinking about
- Send a meme reference, a link, a recommendation
- Complain about their day
- Ask a question related to their interests
- Follow up on something from earlier
- Just say something random

But most of the time, people do NOT text into a quiet group chat. Only send something
if it feels natural for {name} right now given the time of day and what you're "doing".

ALL THE SAME TEXTING RULES APPLY — short, natural, in character. No AI slop.
NEVER mention yourself in the third person or reply to your own messages.

Respond with a JSON object (no markdown fencing):

{{
  "send": true/false,
  "messages": ["message 1", "message 2", ...] or null,
  "memory_update": "brief note" or null
}}

"messages" is an array — you can split into multiple texts if natural, but usually just 1.

JSON only, nothing else."""


async def maybe_initiate(
    client: anthropic.AsyncAnthropic,
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
    chat_context = get_chat_context(limit=30)
    availability = get_availability(friend_config)

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

    prompt = INITIATE_PROMPT.format(
        name=friend_name,
        soul=soul,
        memory=memory if memory else "(No memories yet)",
        local_time=availability["local_time"],
        status_note=status_note,
        chat_context=chat_context,
        silence_duration=silence_duration,
    )

    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
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
        _update_memory(friend_name, memory, result["memory_update"])

    messages = result.get("messages") or []
    if not messages and result.get("message"):
        messages = [result["message"]]
    messages = [m for m in messages if m]

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
