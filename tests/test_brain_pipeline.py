"""Consumer-visible split-stage, attribution, framing and commit boundaries."""

import asyncio
from functools import wraps
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src import brain, reply, memory_validator
from src.chat_history import ChatMessage
from src.llm import LUNA_MODEL
from src.reply import PreparedReply, ReplyAtom, ReplyEffect


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def completion(text):
    return SimpleNamespace(text=text)


def metadata(effects=None, target=None, delay=30):
    return {"effects": effects or [], "reply_to_message_id": target, "delay_seconds": delay}


def effect(kind="topic", value="quiet evening", ids=(0,), texts=("good evening",),
           source="outgoing", source_id=None, subject=None, source_quote=None):
    return {"kind": kind, "value": value, "atom_ids": list(ids), "source": source,
            "source_message_id": source_id, "subject": subject,
            "source_quote": source_quote or texts[0],
            "support_quotes": [{"atom_id": i, "quote": text} for i, text in zip(ids, texts)]}


@pytest.fixture
def setup(monkeypatch):
    messages = [ChatMessage(1, "Travis", "how was your day?", 10)]
    monkeypatch.setattr(brain, "load_messages", lambda limit: list(messages))
    monkeypatch.setattr(brain, "load_friend_soul", lambda name: "A potter with quiet observational humor")
    monkeypatch.setattr(brain, "load_friend_memory", lambda name: "I enjoy making bowls")
    monkeypatch.setattr(brain, "load_history", lambda: "Old friends from school")
    monkeypatch.setattr(brain, "get_chat_context", lambda **kwargs: "\n".join(m.display() for m in messages))
    monkeypatch.setattr(brain, "get_friend_names", lambda: ["casey", "river"])
    monkeypatch.setattr(brain, "get_availability", lambda config: {"awake": True, "at_work": False, "day_off": True, "local_time": "evening"})
    monkeypatch.setattr(brain, "load_friend_news", lambda name: "Today's verified headline")
    monkeypatch.setattr(brain, "get_recent_topics", lambda: "earlier pottery")
    monkeypatch.setattr(brain, "get_recent_joke_formats", lambda: "dry reversal")
    monkeypatch.setattr(brain, "get_recent_complaints", lambda: "traffic")
    monkeypatch.setattr(brain, "render_overasked_block", AsyncMock(return_value="already nagged about music"))
    monkeypatch.setattr(brain, "validate_memory", AsyncMock(return_value=(True, "supported")))
    monkeypatch.setattr(brain, "last_message_age_seconds", lambda: 7 * 3600)
    writes = Mock()
    monkeypatch.setattr(reply, "save_friend_memory", writes)
    monkeypatch.setattr(reply, "record_topic", writes)
    monkeypatch.setattr(reply, "record_joke_format", writes)
    monkeypatch.setattr(reply, "record_complaint", writes)
    client = SimpleNamespace(decide=AsyncMock(return_value=True),
        complete=AsyncMock(return_value=completion("good evening")),
        complete_structured=AsyncMock(return_value=completion(json.dumps(metadata()))))
    return client, messages, writes


async def respond(client, **kwargs):
    return await brain.think_and_respond(client, "casey", "Travis", "how was your day?", 10, {}, **kwargs)


@run_async
async def test_silence_has_no_writer_or_metadata_request(setup):
    client, _, writes = setup
    client.decide.return_value = False
    assert await respond(client) is None
    client.complete.assert_not_awaited()
    client.complete_structured.assert_not_awaited()
    writes.assert_not_called()


@run_async
@pytest.mark.parametrize("decision", [RuntimeError("offline"), "silence"])
async def test_gate_failure_is_not_silence(setup, decision):
    client, _, writes = setup
    if isinstance(decision, Exception):
        client.decide.side_effect = decision
    else:
        client.decide.return_value = decision
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "decision"
    client.complete.assert_not_awaited()
    writes.assert_not_called()


@run_async
async def test_writer_failure_is_distinct_and_never_extracts(setup):
    client, _, writes = setup
    client.complete.side_effect = RuntimeError("writer offline")
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "writer"
    client.complete_structured.assert_not_awaited()
    writes.assert_not_called()


@run_async
async def test_success_preserves_original_lines_and_does_not_write(setup):
    client, _, writes = setup
    client.complete.return_value = completion("  good evening  \n\nI finally finished that bowl")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("memory", "I finished my bowl", (1,), ("I finally finished that bowl",), subject="casey")])))
    prepared = await respond(client)
    assert prepared.atoms == (ReplyAtom(0, "  good evening  "), ReplyAtom(1, "I finally finished that bowl"))
    assert prepared.delay_seconds == 30
    assert prepared.effects[0].atom_ids == (1,)
    assert client.complete.await_args.kwargs["model"] == LUNA_MODEL
    assert client.complete_structured.await_count == 1
    writes.assert_not_called()


@run_async
@pytest.mark.parametrize("draft", ["", "a\nb\nc\nd\ne", "```\nhi\n```", '{"messages":["hi"]}',
                                    "1. hello", "(1) hello", "a) hello", "- hello", "Message 1: hello", "hi\r\nthere"])
async def test_invalid_framing_is_not_reinterpreted_or_truncated(setup, draft):
    client, _, writes = setup
    client.complete.return_value = completion(draft)
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "framing"
    client.complete_structured.assert_not_awaited()
    writes.assert_not_called()


@run_async
async def test_metadata_retry_reuses_exact_draft_and_context(setup):
    client, messages, writes = setup
    calls = []
    async def extraction(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            messages.append(ChatMessage(2, "river", "new chat state", 11))
            return completion("not JSON")
        return completion(json.dumps(metadata()))
    client.complete_structured.side_effect = extraction
    prepared = await respond(client)
    assert prepared is not None
    assert calls[0] == calls[1]
    assert client.complete.await_count == 1
    assert client.decide.await_count == 1
    writes.assert_not_called()


@run_async
@pytest.mark.parametrize("payload", [metadata(delay=9), metadata(delay=181), metadata(delay=True),
    metadata(target=999), metadata(target=10), {"effects": []},
    {**metadata(), "extra": "ignored?"}, metadata(delay=float("nan"))])
async def test_bad_metadata_exhausts_retry_without_delivery_or_defaults(setup, payload):
    client, _, writes = setup
    client.complete_structured.return_value = completion(json.dumps(payload))
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "extraction"
    assert client.complete_structured.await_count == 2
    writes.assert_not_called()


@run_async
async def test_reply_targets_must_be_known_not_self_or_answered(setup):
    client, messages, _ = setup
    messages.extend([ChatMessage(0, "river", "older question", 8), ChatMessage(0, "casey", "own thing", 9)])
    client.complete_structured.return_value = completion(json.dumps(metadata(target=8)))
    assert (await respond(client)).reply_to_message_id == 8
    client.complete_structured.return_value = completion(json.dumps(metadata(target=9)))
    with pytest.raises(brain.BrainStageError):
        await respond(client)
    messages.append(ChatMessage(1, "casey", "already answered", 12, reply_to=8))
    client.complete_structured.return_value = completion(json.dumps(metadata(target=8)))
    with pytest.raises(brain.BrainStageError):
        await respond(client)


@run_async
@pytest.mark.parametrize("bad_effect", [
    effect(ids=(3,)), effect(ids=(0, 0), texts=("good evening", "good evening")),
    effect(texts=("not the draft",)), effect(source="incoming", source_id=99),
    effect("memory", "I own a synth", subject="river"),
    effect("memory", "bought a synth", subject="casey"),
])
async def test_invalid_atom_source_and_person_ownership_cannot_escape(setup, bad_effect):
    client, _, writes = setup
    client.complete_structured.return_value = completion(json.dumps(metadata([bad_effect])))
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "extraction"
    writes.assert_not_called()


@run_async
async def test_generic_question_cannot_become_an_invented_memory(monkeypatch, setup):
    client, _, writes = setup
    client.helper_model = "helper/model"
    client.complete.side_effect = [
        completion("I own a synth"),
        completion('{"valid": false, "reason": "The question attributes no possession"}'),
        completion('{"valid": false, "reason": "The question attributes no possession"}'),
    ]
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("memory", "I own a synth", texts=("I own a synth",), source="incoming",
               source_id=10, subject="casey", source_quote="how was your day?")])))
    monkeypatch.setattr(brain, "validate_memory", memory_validator.validate_memory)
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "extraction"
    writes.assert_not_called()


@run_async
async def test_direct_address_improv_is_validated_with_actual_source(monkeypatch, setup):
    client, messages, writes = setup
    messages[0] = ChatMessage(1, "Travis", "casey remember your synth?", 10)
    client.complete.return_value = completion("I need to turn my synth on")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("memory", "I own a synth", texts=("my synth",), source="incoming", source_id=10,
               subject="casey", source_quote="casey remember your synth?")])))
    validator = AsyncMock(return_value=(True, "direct attribution"))
    monkeypatch.setattr(brain, "validate_memory", validator)
    assert await respond(client) is not None
    assert "casey remember your synth?" in validator.await_args.kwargs["attribution_context"]
    writes.assert_not_called()


@run_async
async def test_reply_thread_is_unambiguous_direct_attribution(setup):
    client, messages, writes = setup
    messages.append(ChatMessage(0, "casey", "what was I working on?", 7))
    messages[0] = ChatMessage(1, "Travis", "your synth", 10, reply_to=7)
    client.complete.return_value = completion("I need to turn my synth on")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("memory", "I own a synth", texts=("my synth",), source="incoming", source_id=10,
               subject="casey", source_quote="your synth")])))
    assert await respond(client) is not None
    writes.assert_not_called()


@run_async
async def test_unthreaded_direct_attribution_keeps_improv_after_semantic_validation(setup):
    client, messages, writes = setup
    messages.insert(0, ChatMessage(0, "casey", "we used to go to so many shows", 7))
    messages[1] = ChatMessage(1, "Travis", "remember when we went to that concert?", 10)
    client.complete.return_value = completion("I still remember that concert")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("memory", "I went to that concert with Travis", texts=("I still remember that concert",),
               source="incoming", source_id=10, subject="casey",
               source_quote="remember when we went to that concert?")])))
    prepared = await respond(client)
    assert prepared.effects == (ReplyEffect("memory", "I went to that concert with Travis",
                                           (0,), "incoming", 10),)
    writes.assert_not_called()


@run_async
async def test_memory_validator_unavailable_rejects_whole_stage(monkeypatch, setup):
    client, _, writes = setup
    client.complete.return_value = completion("I finished my bowl")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("memory", "I finished my bowl", texts=("I finished my bowl",), subject="casey")])))
    monkeypatch.setattr(brain, "validate_memory", AsyncMock(return_value=(False, "validator call failed")))
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "extraction"
    assert client.complete_structured.await_count == 2
    writes.assert_not_called()


@run_async
async def test_filter_retains_atom_indices_and_removes_all_dependent_effects(setup):
    client, _, writes = setup
    client.complete.return_value = completion("river\nI finished my bowl")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect(ids=(0,), texts=("river",)),
        effect("complaint_topic", "slow day", ids=(0, 1), texts=("river", "my bowl")),
        effect("memory", "I finished my bowl", ids=(1,), texts=("my bowl",), subject="casey")])))
    prepared = await respond(client)
    assert prepared.atoms == (ReplyAtom(1, "I finished my bowl"),)
    assert [e.kind for e in prepared.effects] == ["memory"]
    writes.assert_not_called()
    with pytest.raises(ValueError):
        prepared.commit_atom("casey", 0)


@run_async
async def test_photo_description_is_auxiliary_gate_context_and_actual_writer_image(setup):
    client, _, _ = setup
    description = "A bowl on a table"
    await respond(client, image_bytes=b"image", image_media_type="image/png", photo_description=description)
    state = client.decide.await_args.kwargs["state"]
    assert description in state["context"]
    writer = client.complete.await_args.kwargs["messages"][1]["content"]
    assert writer[0]["type"] == "image_url"
    assert writer[0]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client, image_bytes=b"image", image_media_type="image/png")
    assert failure.value.stage == "photo"


@run_async
async def test_shared_photo_helper_is_pinned_and_empty_is_failure(setup):
    client, _, _ = setup
    client.complete.return_value = completion("A bowl")
    assert await brain.describe_photo(client, b"image", "image/png") == "A bowl"
    assert client.complete.await_args.kwargs["model"] == LUNA_MODEL
    client.complete.return_value = completion(" ")
    with pytest.raises(brain.BrainStageError) as failure:
        await brain.describe_photo(client, b"image", "image/png")
    assert failure.value.stage == "photo"


@run_async
async def test_initiation_uses_split_and_asleep_does_not_gate(monkeypatch, setup):
    client, _, writes = setup
    assert await brain.maybe_initiate(client, "casey", {}, 420) is not None
    assert client.decide.await_count == 1
    assert client.complete.await_count == 1
    assert client.complete_structured.await_count == 1
    writes.assert_not_called()
    monkeypatch.setattr(brain, "get_availability", lambda config: {"awake": False})
    assert await brain.maybe_initiate(client, "casey", {}, 420) is None
    assert client.decide.await_count == 1


@run_async
async def test_all_filtered_atoms_are_silent_and_no_effects_persist(setup):
    client, _, writes = setup
    client.complete.return_value = completion("river")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect(texts=("river",))])))
    assert await respond(client) is None
    writes.assert_not_called()


@run_async
async def test_duplicate_response_safeguard_skips_every_model_stage(setup):
    client, messages, writes = setup
    messages.append(ChatMessage(2, "casey", "already answered", 12, reply_to=10))
    assert await respond(client) is None
    client.decide.assert_not_awaited()
    client.complete.assert_not_awaited()
    client.complete_structured.assert_not_awaited()
    writes.assert_not_called()


@run_async
async def test_duplicate_json_fields_are_malformed_not_last_value_wins(setup):
    client, _, writes = setup
    client.complete_structured.return_value = completion(
        '{"effects":[],"reply_to_message_id":null,"delay_seconds":999,"delay_seconds":30}')
    with pytest.raises(brain.BrainStageError) as failure:
        await respond(client)
    assert failure.value.stage == "extraction"
    assert client.complete_structured.await_count == 2
    writes.assert_not_called()


def test_partial_delivery_effect_support_idempotence_and_current_memory(monkeypatch):
    memory = {"value": "# Memory\n- [old] previous state"}
    saved = []
    def save(name, value):
        memory["value"] = value
        saved.append(value)
    monkeypatch.setattr(reply, "load_friend_memory", lambda name: memory["value"])
    monkeypatch.setattr(reply, "save_friend_memory", save)
    topics = Mock()
    monkeypatch.setattr(reply, "record_topic", topics)
    prepared = PreparedReply((ReplyAtom(0, "promise"), ReplyAtom(1, "detail")), (
        ReplyEffect("memory", "I promise to finish", (0,), "outgoing", None),
        ReplyEffect("topic", "plan detail", (0, 1), "outgoing", None),
        ReplyEffect("memory", "I made another promise", (1,), "outgoing", None)), None, 30, owner_name="casey")
    with pytest.raises(ValueError):
        prepared.commit_atom("river", 0)
    prepared.commit_atom("casey", 0)
    assert len(saved) == 1
    topics.assert_not_called()
    prepared.commit_atom("casey", 0)
    assert len(saved) == 1
    memory["value"] += "\n- [new] independently committed fact"
    prepared.commit_atom("casey", 1)
    prepared.commit_atom("casey", 1)
    assert len(saved) == 2
    assert "independently committed fact" in saved[-1]
    topics.assert_called_once_with("casey", "plan detail")


@run_async
async def test_real_echo_filter_removes_supported_effect_without_renumbering(setup):
    client, messages, writes = setup
    echoed = "the little blue pottery bowl is sitting beside the kitchen window"
    messages.append(ChatMessage(2, "river", echoed, 11))
    client.complete.return_value = completion(echoed + "\nthat sounds peaceful")
    client.complete_structured.return_value = completion(json.dumps(metadata([
        effect("topic", "pottery by window", ids=(0,), texts=(echoed,)),
        effect("topic", "peaceful evening", ids=(1,), texts=("that sounds peaceful",))])))
    prepared = await respond(client)
    assert prepared.atoms == (ReplyAtom(1, "that sounds peaceful"),)
    assert [e.value for e in prepared.effects] == ["peaceful evening"]
    writes.assert_not_called()
