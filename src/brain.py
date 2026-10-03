"""Jev social decisions, Luna texts, and validated delivery-owned metadata."""

import base64
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .llm import AsyncOpenRouter, LUNA_MODEL
from .config import load_friend_soul, load_friend_memory, load_history, get_friend_names
from .chat_history import ChatMessage, get_chat_context, last_message_age_seconds, load_messages
from .echo_detector import is_echo, is_name_only, RECENT_MESSAGES_TO_CHECK
from .schedule import get_availability
from .topics import get_recent_topics, get_recent_joke_formats, get_recent_complaints
from .news import load_friend_news
from .memory_validator import validate_memory
from .nag_detector import render_overasked_block
from .reply import PreparedReply, ReplyAtom, ReplyEffect

logger = logging.getLogger(__name__)


class BrainStageError(RuntimeError):
    """A failed opportunity, never a deliberate decision to stay silent."""

    def __init__(self, stage: str, friend_name: str, detail: str):
        self.stage = stage
        self.friend_name = friend_name
        super().__init__(f"{stage} failed for {friend_name}: {detail}")


def _describe_dials(friend_config: dict) -> str:
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
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _split_cached_prompt(raw: str) -> tuple[str, str]:
    context, sep, rules = raw.partition("\n===RULES===\n")
    if not sep:
        raise ValueError("prompt is missing the ===RULES=== cache boundary")
    return context.rstrip(), rules.strip()


_CONTEXT, _WRITE_RULES = _split_cached_prompt(_load_prompt("write.md"))
_EXTRACT_RULES = _load_prompt("extract.md")
_INITIATE_OPPORTUNITY = _load_prompt("initiate.md")

# All objects forbid unknown fields. Explicit evidence enables independent local
# identity/source checks rather than trusting a schema-valid attribution label.
_QUOTE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["atom_id", "quote"],
    "properties": {"atom_id": {"type": "integer", "minimum": 0, "maximum": 3},
                   "quote": {"type": "string", "minLength": 1}},
}
_EFFECT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "value", "atom_ids", "source", "source_message_id", "subject", "source_quote", "support_quotes"],
    "properties": {
        "kind": {"type": "string", "enum": ["memory", "topic", "joke_format", "complaint_topic"]},
        "value": {"type": "string", "minLength": 1, "maxLength": 1000},
        "atom_ids": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "integer", "minimum": 0, "maximum": 3}},
        "source": {"type": "string", "enum": ["incoming", "outgoing"]},
        "source_message_id": {"type": ["integer", "null"]},
        "subject": {"type": ["string", "null"]},
        "source_quote": {"type": "string", "minLength": 1},
        "support_quotes": {"type": "array", "minItems": 1, "maxItems": 4, "items": _QUOTE_SCHEMA},
    },
}
METADATA_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["effects", "reply_to_message_id", "delay_seconds"],
    "properties": {
        "effects": {"type": "array", "maxItems": 16, "items": _EFFECT_SCHEMA},
        "reply_to_message_id": {"type": ["integer", "null"]},
        "delay_seconds": {"type": "number", "minimum": 10, "maximum": 180},
    },
}


def _image_content(text: str, image_bytes: bytes | None, media_type: str | None):
    if image_bytes is None:
        return text
    if not image_bytes or not media_type:
        raise ValueError("Photo bytes and media type are both required")
    return [{"type": "image_url", "image_url": {"url": f"data:{media_type};base64," + base64.standard_b64encode(image_bytes).decode("ascii"), "detail": "high"}},
            {"type": "text", "text": text}]


async def describe_photo(client: AsyncOpenRouter, image_bytes: bytes, image_media_type: str) -> str:
    try:
        response = await client.complete(
            model=LUNA_MODEL, max_tokens=600, label="photo_description",
            messages=[{"role": "user", "content": _image_content(
                "Describe what is visibly present in this photo for a text-only social decision. Include visible text and uncertainty. Do not invent sender intent, identity, ownership, or memories.",
                image_bytes, image_media_type)}],
        )
        if not isinstance(response.text, str) or not response.text.strip():
            raise ValueError("Empty photo description")
        return response.text
    except Exception as exc:
        logger.warning("Photo prerequisite failed: %s", exc)
        raise BrainStageError("photo", "shared", str(exc)) from exc


def _parse_atoms(raw: str) -> tuple[ReplyAtom, ...]:
    if not isinstance(raw, str):
        raise ValueError("Writer returned no text")
    if raw.strip().startswith('"'):
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            pass
        else:
            raise ValueError("Writer returned a JSON string rather than plain text")
    if "\r" in raw or any(c in raw for c in ("\v", "\f", "\u0085", "\u2028", "\u2029")):
        raise ValueError("Writer returned non-newline atom separators")
    lines = [line for line in raw.split("\n") if line.strip()]
    if not 1 <= len(lines) <= 4:
        raise ValueError("Writer must produce one through four nonempty lines")
    for line in lines:
        stripped = line.lstrip()
        if ("```" in line or stripped.startswith(("{", "[", "}", "]", "~~~"))
                or re.match(r"(?:\(?\d+[.)\]:](?=\s|$)|[a-z][.)]\s|[-*+]\s|#{1,6}\s|(?:message|text|atom)\s*\d*\s*:)", stripped, re.I)):
            raise ValueError("Writer returned JSON, fences, numbering, or other framing")
    # Keep exact original wording and whitespace; blank lines create no identities.
    return tuple(ReplyAtom(index, line) for index, line in enumerate(lines))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Metadata contains duplicate JSON fields")
        result[key] = value
    return result


def _check_schema(value, schema: dict) -> None:
    """Check the bounded extraction schema without coercion or JSON fallbacks."""
    types = schema["type"]
    types = [types] if isinstance(types, str) else types
    matches = {"null": value is None, "object": type(value) is dict,
               "array": type(value) is list, "string": type(value) is str,
               "integer": type(value) is int,
               "number": type(value) in (int, float) and math.isfinite(value)}
    if not any(matches[t] for t in types):
        raise ValueError("Metadata field has an invalid type")
    if value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Metadata field has an invalid enum")
    if type(value) is dict:
        if set(value) != set(schema["required"]):
            raise ValueError("Metadata fields missing or unexpected")
        for key, child in value.items():
            _check_schema(child, schema["properties"][key])
    elif type(value) is list:
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise ValueError("Invalid metadata array size")
        for child in value:
            _check_schema(child, schema["items"])
    elif type(value) is str:
        if not value.strip() or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            raise ValueError("Empty or oversized metadata string")
        if "\n" in value or "\r" in value:
            raise ValueError("Metadata must not inject persistence lines")
    elif type(value) in (int, float):
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise ValueError("Metadata number outside bounds")


async def _validate_metadata(client, data, atoms, state, sources, allowed_targets):
    _check_schema(data, METADATA_SCHEMA)
    target = data["reply_to_message_id"]
    if target is not None and target not in allowed_targets:
        raise ValueError("Unknown, self, or already-answered reply target")
    by_atom = {atom.index: atom.text for atom in atoms}
    effects = []
    seen = set()
    name = state["friend_name"]
    other_names = [n for n in state["participant_names"] if n != name]
    for effect in data["effects"]:
        ids = effect["atom_ids"]
        if len(set(ids)) != len(ids) or not set(ids) <= set(by_atom):
            raise ValueError("Effect refers to absent or repeated atoms")
        quotes = effect["support_quotes"]
        if len(quotes) != len(ids) or {q["atom_id"] for q in quotes} != set(ids):
            raise ValueError("Effect lacks exact support for every atom")
        if any(q["quote"] not in by_atom[q["atom_id"]] for q in quotes):
            raise ValueError("Effect support is not in exact writer text")
        source_id = effect["source_message_id"]
        if effect["source"] == "outgoing":
            if source_id is not None or not any(effect["source_quote"] in by_atom[i] for i in ids):
                raise ValueError("Outgoing attribution is not supported by draft")
        else:
            if source_id is None or source_id <= 0 or source_id not in sources:
                raise ValueError("Incoming attribution lacks actual message ID")
            if effect["source_quote"] not in sources[source_id].text:
                raise ValueError("Incoming source quote is not in attributed message")
        if effect["kind"] != "memory":
            if effect["source"] != "outgoing" or effect["subject"] is not None:
                raise ValueError("Repetition effects must describe delivered outgoing text")
        else:
            subject = effect["subject"]
            first_person = bool(re.search(r"\b(?:I|my|me|mine|myself)\b", effect["value"], re.I))
            if subject == name:
                if not first_person:
                    raise ValueError("Own memory must have explicit first-person subject")
                if effect["source"] == "incoming":
                    if sources[source_id].sender == name:
                        raise ValueError("An incoming attribution must come from another speaker")
                    # Direct address can be unthreaded and omit the friend's name.
                    # The fail-closed semantic validator checks actual conversation
                    # evidence rather than treating name/thread syntax as proof.
            elif subject not in other_names or first_person or not re.search(rf"\b{re.escape(subject or '')}\b", effect["value"], re.I):
                raise ValueError("Memory has ambiguous or wrong-person subject")
            evidence = json.dumps({"effect": effect, "atoms": [by_atom[i] for i in ids],
                                   "source_message": vars(sources[source_id]) if source_id in sources else None,
                                   "conversation": state["context"]}, ensure_ascii=False)
            valid, reason = await validate_memory(client, name, state["soul"], effect["value"],
                                                  other_names=other_names, attribution_context=evidence)
            if not valid:
                raise ValueError(f"Memory validation failed: {reason}")
        prepared = ReplyEffect(effect["kind"], effect["value"], tuple(ids), effect["source"], source_id)
        if prepared in seen:
            raise ValueError("Duplicate effect")
        seen.add(prepared)
        effects.append(prepared)
    return tuple(effects)


async def _context(client, name, config, opportunity, triggering_message=None):
    availability = get_availability(config)
    if not availability["awake"]:
        status = "You're asleep (phone might wake you for important stuff)"
    elif availability["at_work"]:
        status = "You're at work right now — might be slower to respond"
    elif availability["day_off"]:
        status = "It's your day off — you're relaxed and available"
    else:
        status = "You're free right now"
    soul = load_friend_soul(name)
    bot_names = set(get_friend_names())
    overasked = await render_overasked_block(client, bot_names)
    messages = load_messages(50)
    # Compaction may remove a normal or catchup opportunity during the helper.
    # Retain only its actual source, not the unrelated pre-await chat window.
    if triggering_message is not None and not any(
            m.message_id == triggering_message.message_id for m in messages):
        messages.insert(0, triggering_message)
    context = _CONTEXT.format(
        name=name, soul=soul, personality_dials=_describe_dials(config),
        history=load_history() or "(No shared history yet)",
        memory=load_friend_memory(name) or "(No memories yet)",
        local_time=availability["local_time"], status_note=status,
        news=load_friend_news(name) or "(Nothing loaded yet)",
        recent_topics=get_recent_topics() or "(None yet)",
        recent_jokes=get_recent_joke_formats() or "(None yet)",
        recent_complaints=get_recent_complaints() or "(None yet)",
        overasked_block=(f"\n## Threads being beaten to death (DO NOT touch these)\n{overasked}\n" if overasked else ""),
        chat_context=get_chat_context(messages=messages), opportunity=opportunity,
    )
    sources = {m.message_id: m for m in messages if m.message_id > 0 and not m.is_reaction}
    answered = {m.reply_to for m in messages if m.sender == name and m.reply_to}
    targets = {mid for mid, msg in sources.items() if msg.sender != name and mid not in answered}
    state = {"friend_name": name, "soul": soul, "context": context,
             "texting_rules": _WRITE_RULES,
             "participant_names": sorted(bot_names | {m.sender for m in messages}),
             "decision_policy": "Speak only if this friend naturally has something to add. Favor restraint when ambiguous, but direct questions, genuine invitations, and emotional support deserve engagement. No echo, repeated answer, self reply, own-thread nag, or pile-on. Accept unambiguous direct attributions to this friend as improv, never another friend's attribution. Initiations usually stay silent; use own life and freshness constraints.",
             "allowed_reply_targets": sorted(targets)}
    return state, sources, targets


async def _prepare(client, name, state, sources, targets, image_bytes=None, image_media_type=None):
    try:
        decision = await client.decide(state=state, label=f"decision:{name}")
        if type(decision) is not bool:
            raise ValueError("Jev returned an unusable decision")
    except Exception as exc:
        logger.warning("[%s] Decision stage failed: %s", name, exc)
        raise BrainStageError("decision", name, str(exc)) from exc
    if not decision:
        logger.info("[%s] Jev deliberately chose silence", name)
        return None
    try:
        response = await client.complete(
            model=LUNA_MODEL, max_tokens=1024, label=f"write:{name}",
            messages=[{"role": "system", "content": [{"type": "text", "text": _WRITE_RULES, "cache_control": {"type": "ephemeral"}}]},
                      {"role": "user", "content": _image_content(state["context"], image_bytes, image_media_type)}],
        )
    except Exception as exc:
        logger.warning("[%s] Writer stage failed: %s", name, exc)
        raise BrainStageError("writer", name, str(exc)) from exc
    try:
        atoms = _parse_atoms(response.text)
    except ValueError as exc:
        logger.warning("[%s] Framing stage failed: %s", name, exc)
        raise BrainStageError("framing", name, str(exc)) from exc
    # Build once: retries use byte-identical draft, immutable context and schema.
    extraction_messages = [
        {"role": "system", "content": _EXTRACT_RULES},
        {"role": "user", "content": _image_content(json.dumps(
            {"generation_context": state, "draft": response.text,
             "atoms": [{"index": atom.index, "text": atom.text} for atom in atoms],
             "source_messages": [vars(m) for m in sources.values()]}, ensure_ascii=False),
            image_bytes, image_media_type)},
    ]
    for attempt in range(2):
        try:
            response = await client.complete_structured(messages=extraction_messages, schema=METADATA_SCHEMA,
                                                        max_tokens=2400, label=f"extract:{name}")
            data = json.loads(response.text, object_pairs_hook=_unique_object)
            effects = await _validate_metadata(client, data, atoms, state, sources, targets)
            break
        except Exception as exc:
            logger.warning("[%s] Extraction stage attempt %s failed: %s", name, attempt + 1, exc)
            if attempt == 1:
                raise BrainStageError("extraction", name, str(exc)) from exc
    recent = [m for m in load_messages(RECENT_MESSAGES_TO_CHECK) if not m.is_reaction]
    texts = [m.text for m in recent]
    names = set(state["participant_names"]) | {m.sender for m in recent}
    kept = []
    for atom in atoms:
        if is_echo(atom.text, texts) or is_name_only(atom.text, names):
            logger.warning("[%s] Dropped echo/name-only atom %s", name, atom.index)
        else:
            kept.append(atom)
    if not kept:
        return None
    kept_ids = {atom.index for atom in kept}
    return PreparedReply(tuple(kept), tuple(e for e in effects if set(e.atom_ids) <= kept_ids),
                         data["reply_to_message_id"], float(data["delay_seconds"]), owner_name=name)


async def think_and_respond(client: AsyncOpenRouter, friend_name: str, sender: str,
                            message: str, message_id: int, friend_config: dict,
                            image_bytes: bytes | None = None, image_media_type: str | None = None,
                            link_previews: str = "", photo_description: str = "") -> PreparedReply | None:
    messages = load_messages(50)
    if sender == friend_name or (message_id > 0 and any(
            m.sender == friend_name and m.reply_to == message_id for m in messages)):
        return None
    if image_bytes is not None or image_media_type is not None:
        if not image_bytes or not image_media_type or not photo_description.strip():
            raise BrainStageError("photo", friend_name, "Missing shared photo prerequisite")
    opportunity = f"New message [msg:{message_id}][{sender}]: {message}"
    if link_previews:
        opportunity += "\nFetched link previews (auxiliary context, not sender assertions):\n" + link_previews + "\nReference relevant contents naturally, not as a book report."
    if photo_description:
        opportunity += "\nAuxiliary model-produced photo description (not a sender assertion or memory):\n" + photo_description
    triggering_message = None
    if message_id > 0:
        triggering_message = next((m for m in messages if m.message_id == message_id), None)
        if triggering_message is None:
            triggering_message = ChatMessage(0, sender, message, message_id)
    try:
        state, sources, targets = await _context(client, friend_name, friend_config, opportunity, triggering_message)
    except Exception as exc:
        raise BrainStageError("context", friend_name, str(exc)) from exc
    # The newest message needs no explicit threading; older known targets only.
    targets.discard(message_id)
    state["allowed_reply_targets"] = sorted(targets)
    return await _prepare(client, friend_name, state, sources, targets, image_bytes, image_media_type)


async def maybe_initiate(client: AsyncOpenRouter, friend_name: str, friend_config: dict,
                         silence_minutes: int) -> PreparedReply | None:
    if not get_availability(friend_config)["awake"]:
        return None
    age = last_message_age_seconds()
    freshness = ("STALE CHAT WARNING: The last message was hours ago. This is a fresh opening, not a continuation. No yesterday's-topic summary or still-thinking followup. Start something new or say nothing." if age is not None and age >= 6 * 3600 else "")
    now = datetime.now(ZoneInfo(friend_config.get("timezone", "UTC").replace(" ", "_")))
    if now.hour < 10:
        vibe = "Morning energy — coffee, getting started."
    elif now.hour < 13:
        vibe = "Midday — work break or lunch."
    elif now.hour < 17:
        vibe = "Afternoon — the drag or the groove."
    elif now.hour < 20:
        vibe = "Evening — winding down, plans, cooking."
    else:
        vibe = "Late night — couch mode, random thoughts, can't sleep."
    opportunity = _INITIATE_OPPORTUNITY.format(
        silence_duration=f"{silence_minutes} minutes" if silence_minutes < 60 else f"{silence_minutes / 60:.1f} hours",
        day_of_week=now.strftime("%A"), time_vibe=vibe, freshness_note=freshness)
    try:
        state, sources, targets = await _context(client, friend_name, friend_config, opportunity)
    except Exception as exc:
        raise BrainStageError("context", friend_name, str(exc)) from exc
    return await _prepare(client, friend_name, state, sources, targets)
