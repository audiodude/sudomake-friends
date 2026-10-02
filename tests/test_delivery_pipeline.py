"""Consumer-visible delivery, freshness, media prerequisites, and cancellation."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from src import bot, chat_history, config, topics
from src.chat_history import ChatMessage
from src.reply import PreparedReply, ReplyAtom, ReplyEffect


REAL_SLEEP = asyncio.sleep


def prepared(texts=("A delivered text",), *, effects=(), indexes=None,
             reply_to=1, delay=17):
    indexes = range(len(texts)) if indexes is None else indexes
    return PreparedReply(
        atoms=tuple(ReplyAtom(index, text) for index, text in zip(indexes, texts)),
        effects=tuple(effects),
        reply_to_message_id=reply_to,
        delay_seconds=delay,
    )


def effect(kind, value, atom_ids):
    return ReplyEffect(kind, value, tuple(atom_ids), "outgoing", None)


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(chat_history, "CHAT_PATH", tmp_path / "CHAT.jsonl")
    monkeypatch.setattr(chat_history, "CHAT_SUMMARY_PATH", tmp_path / "SUMMARY.md")
    monkeypatch.setattr(topics, "TOPICS_PATH", tmp_path / "RECENT_TOPICS.md")
    monkeypatch.setattr(bot, "maybe_compact", AsyncMock())
    monkeypatch.setattr(bot, "load_friend_config", lambda name: {
        "name": name, "chattiness": 1.0,
    })
    monkeypatch.setattr(bot, "get_availability", lambda _: {
        "awake": True, "at_work": False,
    })
    monkeypatch.setattr(bot, "get_friend_names", lambda: ["alex", "river"])
    monkeypatch.setattr(bot, "get_activity_config", lambda: {
        "reply": {"human_dampener": 1.0, "bot_dampener": 1.0},
        "initiation": {
            "check_min_minutes": 1, "check_max_minutes": 1,
            "silence_threshold_minutes": 0, "decay_floor": 1.0,
            "decay_divisor_hours": 1.0, "dampener": 1.0,
        },
    })
    monkeypatch.setattr(bot, "should_respond", lambda *args, **kwargs: False)
    monkeypatch.setattr(bot, "fetch_previews", lambda _: "shared link preview")
    monkeypatch.setattr(bot.random, "shuffle", lambda _: None)
    monkeypatch.setattr(bot.random, "uniform", lambda *_: 5.0)
    monkeypatch.setattr(bot.random, "gauss", lambda *_: 7.0)
    monkeypatch.setattr(bot.random, "randint", lambda lower, upper: lower)
    monkeypatch.setattr(bot.random, "random", lambda: 0.0)
    monkeypatch.setattr(bot.random, "choice", lambda values: values[0])
    delays = []

    async def fast_sleep(delay):
        delays.append(delay)
        await REAL_SLEEP(0)

    monkeypatch.setattr(bot.asyncio, "sleep", fast_sleep)
    group = bot.FriendGroup.__new__(bot.FriendGroup)
    group.global_config = {}
    group.model = "existing-compaction-model"
    group.llm = SimpleNamespace(aclose=AsyncMock())
    group.bots = {}
    group._bot_user_ids = set()
    group._active_tasks = {}
    group._response_tasks = set()
    group._pending_mentions = []
    group._engagement = {}
    next_id = 1000

    def add_friend(name):
        nonlocal next_id
        friend = bot.FriendBot.__new__(bot.FriendBot)
        friend.name = name
        friend.group_chat_id = 42
        friend._bot_user_id = len(group.bots) + 100
        friend._bot_username = f"{name}_bot"

        async def send(**kwargs):
            nonlocal next_id
            next_id += 1
            return SimpleNamespace(message_id=next_id)

        friend.bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=send),
            get_file=AsyncMock(return_value=SimpleNamespace(
                download_as_bytearray=AsyncMock(return_value=bytearray(b"jpeg image")),
            )),
        )
        group.bots[name] = friend
        group._bot_user_ids.add(friend.user_id)
        return friend

    chat_history.append_message(ChatMessage(
        timestamp=time.time(), sender="human", text="Original message", message_id=1,
    ))
    return SimpleNamespace(group=group, add_friend=add_friend, delays=delays,
                           root=tmp_path)


def incoming(*, text="A new human message", sender_id=1, message_id=2,
             photo=False, caption=None):
    return SimpleNamespace(
        text=text, caption=caption,
        photo=[SimpleNamespace(file_id="largest-photo")] if photo else [],
        from_user=SimpleNamespace(id=sender_id, first_name="human", username=None),
        message_id=message_id, reply_to_message=None,
    )


def test_shuffled_speakers_generate_once_with_current_committed_state(delivery, monkeypatch):
    group = delivery.group
    alex = delivery.add_friend("alex")
    river = delivery.add_friend("river")
    calls = []
    monkeypatch.setattr(bot.random, "shuffle", lambda values: values.reverse())

    async def think(**kwargs):
        name = kwargs["friend_name"]
        assert "model" not in kwargs
        calls.append(name)
        if name == "alex":
            assert [message.sender for message in chat_history.load_messages()] == ["human"]
            assert topics.get_recent_topics() == ""
            return prepared(("I finished my bowl",), effects=(
                effect("memory", "I finished my bowl", (0,)),
                effect("topic", "finished pottery", (0,)),
            ))
        assert [message.sender for message in chat_history.load_messages()] == ["human", "alex"]
        assert "finished pottery" in topics.get_recent_topics()
        assert "I finished my bowl" in config.load_friend_memory("alex")
        return prepared(("Nice glaze",))

    monkeypatch.setattr(bot, "think_and_respond", think)
    asyncio.run(group._staggered_responses(
        [("river", river, {}), ("alex", alex, {})], "human", "Original message", 1,
    ))
    assert calls == ["alex", "river"]
    assert [message.text for message in chat_history.load_messages()][1:] == [
        "I finished my bowl", "Nice glaze",
    ]
    assert delivery.delays == [17, 22]
    assert group._engagement["alex"]["streak"] == 1
    assert group._engagement["river"]["streak"] == 1


@pytest.mark.parametrize("first_result", [None, RuntimeError("decision transport failed")])
def test_silence_or_failure_does_not_prevent_next_fresh_speaker(
    delivery, monkeypatch, caplog, first_result,
):
    group = delivery.group
    alex = delivery.add_friend("alex")
    river = delivery.add_friend("river")
    calls = []

    async def think(**kwargs):
        calls.append(kwargs["friend_name"])
        if kwargs["friend_name"] == "alex":
            if isinstance(first_result, Exception):
                raise first_result
            return first_result
        assert [message.sender for message in chat_history.load_messages()] == ["human"]
        return prepared(("Still here",))

    monkeypatch.setattr(bot, "think_and_respond", think)
    asyncio.run(group._staggered_responses(
        [("alex", alex, {}), ("river", river, {})], "human", "Original message", 1,
    ))
    assert calls == ["alex", "river"]
    assert "alex" not in group._engagement
    assert delivery.delays == [17]
    assert [message.sender for message in chat_history.load_messages()] == ["human", "river"]
    assert ("Response pipeline failed for alex" in caplog.text) == isinstance(first_result, Exception)


def test_filtered_indexes_and_multi_atom_effects_commit_before_history(delivery, monkeypatch):
    group = delivery.group
    alex = delivery.add_friend("alex")
    reply = prepared(("The first surviving text", "The third original text"), indexes=(0, 2), effects=(
        effect("topic", "first visible topic", (0,)),
        effect("memory", "I made an unsent promise", (1,)),
        effect("joke_format", "filtered joke", (1,)),
        effect("memory", "I delivered both parts", (0, 2)),
        effect("complaint_topic", "visible third complaint", (2,)),
    ))
    observations = []

    def append(message):
        memory = config.load_friend_memory("alex")
        observations.append((message.text, memory, topics.get_recent_topics()))
        assert "first visible topic" in topics.get_recent_topics()
        assert "unsent promise" not in memory
        if len(observations) == 1:
            assert "delivered both parts" not in memory
        else:
            assert "delivered both parts" in memory
            assert "visible third complaint" in topics.get_recent_complaints()
        chat_history.append_message(message)

    monkeypatch.setattr(bot, "append_message", append)
    sent = asyncio.run(group._send_messages(alex, "alex", reply, reply_to_message_id=1))
    assert [message.text for message in sent] == [atom.text for atom in reply.atoms]
    assert [call.kwargs.get("reply_to_message_id") for call in alex.bot.send_message.call_args_list] == [1, None]
    assert "filtered joke" not in topics.get_recent_joke_formats()
    assert len(observations) == 2
    assert group._engagement["alex"]["streak"] == 1
    reply.commit_atom("alex", 2)
    assert config.load_friend_memory("alex").count("I delivered both parts") == 1


def test_telegram_failure_commits_only_confirmed_partial_delivery(delivery):
    group = delivery.group
    alex = delivery.add_friend("alex")
    alex.bot.send_message.side_effect = [
        SimpleNamespace(message_id=10), TelegramError("Telegram rejected the second text"),
    ]
    reply = prepared(("I made a bowl", "I will buy a kiln"), effects=(
        effect("memory", "I made a bowl", (0,)),
        effect("topic", "pottery", (0,)),
        effect("memory", "I will buy a kiln", (1,)),
        effect("joke_format", "kiln joke", (1,)),
        effect("complaint_topic", "both parts complaint", (0, 1)),
    ))
    sent = asyncio.run(group._send_messages(alex, "alex", reply))
    assert [message.text for message in sent] == ["I made a bowl"]
    assert "I made a bowl" in config.load_friend_memory("alex")
    assert "I will buy a kiln" not in config.load_friend_memory("alex")
    assert "pottery" in topics.get_recent_topics()
    assert topics.get_recent_joke_formats() == ""
    assert topics.get_recent_complaints() == ""
    assert group._engagement["alex"]["streak"] == 1
    assert [message.text for message in chat_history.load_messages()][1:] == ["I made a bowl"]


def test_no_confirmed_telegram_delivery_has_no_tracking_effects(delivery):
    group = delivery.group
    alex = delivery.add_friend("alex")
    alex.bot.send_message.side_effect = TelegramError("Telegram unavailable")
    reply = prepared(effects=(
        effect("memory", "I promised something", (0,)),
        effect("topic", "unsent topic", (0,)),
    ))
    assert asyncio.run(group._send_messages(alex, "alex", reply)) == []
    assert config.load_friend_memory("alex") == ""
    assert topics.get_recent_topics() == ""
    assert group._engagement == {}
    assert len(chat_history.load_messages()) == 1


@pytest.mark.parametrize("interrupt_at", ["typing", "next_delivery"])
def test_human_cancellation_preserves_first_atom_effects_and_engagement(
    delivery, monkeypatch, interrupt_at,
):
    async def scenario():
        group = delivery.group
        alex = delivery.add_friend("alex")
        typing = asyncio.Event()

        async def wait_between_atoms(_):
            if interrupt_at == "typing":
                typing.set()
                await asyncio.Event().wait()
            await REAL_SLEEP(0)

        async def send_until_second(**kwargs):
            if kwargs["text"] == "Already visible":
                return SimpleNamespace(message_id=10)
            typing.set()
            await asyncio.Event().wait()

        if interrupt_at == "next_delivery":
            alex.bot.send_message.side_effect = send_until_second

        monkeypatch.setattr(bot.asyncio, "sleep", wait_between_atoms)
        reply = prepared(("Already visible", "An unsent commitment"), effects=(
            effect("memory", "I already said this", (0,)),
            effect("topic", "visible topic", (0,)),
            effect("memory", "I committed to the unsent plan", (1,)),
            effect("joke_format", "unsent joke", (1,)),
            effect("complaint_topic", "partial joint complaint", (0, 1)),
        ))
        task = group._start_response_task(["alex"], group._send_messages(alex, "alex", reply))
        await asyncio.wait_for(typing.wait(), 2)
        assert "I already said this" in config.load_friend_memory("alex")
        assert group._engagement["alex"]["streak"] == 1
        await group._handle_message(incoming())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "unsent plan" not in config.load_friend_memory("alex")
        assert topics.get_recent_joke_formats() == ""
        assert topics.get_recent_complaints() == ""
        assert [message.text for message in chat_history.load_messages()] == [
            "Original message", "Already visible", "A new human message",
        ]
        assert group._active_tasks == {}
        assert group._response_tasks == set()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["normal", "initiation", "catchup"])
@pytest.mark.parametrize("stage", ["generation", "extraction", "delay", "delivery"])
def test_human_cancels_each_stage_without_stopping_periodic_owner(
    delivery, monkeypatch, path, stage,
):
    async def scenario():
        group = delivery.group
        alex = delivery.add_friend("alex")
        reached = asyncio.Event()
        resumed = asyncio.Event()
        periodic_sleeps = 0
        reply = prepared(effects=(
            effect("memory", "I made an unsent promise", (0,)),
            effect("topic", "unsent topic", (0,)),
        ))

        async def controlled_sleep(delay):
            nonlocal periodic_sleeps
            if (path == "initiation" and delay == 60) or (path == "catchup" and delay == 300):
                periodic_sleeps += 1
                first_pass = 2 if path == "initiation" else 1
                if periodic_sleeps > first_pass:
                    resumed.set()
                    await asyncio.Event().wait()
                await REAL_SLEEP(0)
            elif stage == "delay":
                reached.set()
                await asyncio.Event().wait()
            else:
                await REAL_SLEEP(0)

        async def generate(**kwargs):
            assert "model" not in kwargs
            if stage in ("generation", "extraction"):
                reached.set()
                await asyncio.Event().wait()
            return reply

        async def blocked_send(**kwargs):
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(bot.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(bot, "think_and_respond", generate)
        monkeypatch.setattr(bot, "maybe_initiate", generate)
        if stage == "delivery":
            alex.bot.send_message.side_effect = blocked_send
        if path == "normal":
            owner = group._start_response_task(["alex"], group._staggered_responses(
                [("alex", alex, {})], "human", "Original message", 1,
            ))
        elif path == "initiation":
            owner = asyncio.create_task(group._initiation_loop())
        else:
            group._pending_mentions.append(bot.PendingMention(
                "alex", "human", "Original message", 1, time.time(), True,
            ))
            owner = asyncio.create_task(group._catchup_loop())
        try:
            await asyncio.wait_for(reached.wait(), 2)
            operation = group._active_tasks["alex"]
            await group._handle_message(incoming())
            with pytest.raises(asyncio.CancelledError):
                await operation
            if path != "normal":
                await asyncio.wait_for(resumed.wait(), 2)
                assert not owner.done()
            assert group._active_tasks == {}
            assert config.load_friend_memory("alex") == ""
            assert topics.get_recent_topics() == ""
            assert "alex" not in group._engagement
            assert [message.sender for message in chat_history.load_messages()] == ["human", "human"]
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(scenario())


def test_old_cancelled_flow_cannot_unregister_new_replacement(delivery, monkeypatch):
    async def scenario():
        group = delivery.group
        alex = delivery.add_friend("alex")
        old_started = asyncio.Event()
        cancelling = asyncio.Event()
        release_cleanup = asyncio.Event()
        new_started = asyncio.Event()
        calls = 0

        async def think(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                old_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelling.set()
                    await release_cleanup.wait()
                    raise
            new_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(bot, "think_and_respond", think)
        old = group._start_response_task(["alex"], group._staggered_responses(
            [("alex", alex, {})], "human", "Original message", 1,
        ))
        await asyncio.wait_for(old_started.wait(), 2)
        monkeypatch.setattr(bot, "should_respond", lambda *args, **kwargs: True)
        await group._handle_message(incoming())
        newer = group._active_tasks["alex"]
        await asyncio.wait_for(cancelling.wait(), 2)
        await asyncio.wait_for(new_started.wait(), 2)
        assert old in group._response_tasks
        release_cleanup.set()
        await asyncio.gather(old, return_exceptions=True)
        assert group._active_tasks["alex"] is newer
        assert newer in group._response_tasks
        newer.cancel()
        await asyncio.gather(newer, return_exceptions=True)
        assert group._active_tasks == {}

    asyncio.run(scenario())


def test_shared_photo_is_described_once_and_retained_for_queued_catchup(delivery, monkeypatch):
    async def scenario():
        group = delivery.group
        alex = delivery.add_friend("alex")
        river = delivery.add_friend("river")
        casey = delivery.add_friend("casey")
        description = AsyncMock(return_value="Auxiliary description of the pottery photo")
        monkeypatch.setattr(bot, "describe_photo", description)
        monkeypatch.setattr(bot, "should_respond", lambda friend_config, **kwargs: friend_config["name"] != "river")
        previews = []
        monkeypatch.setattr(bot, "fetch_previews", lambda caption: previews.append(caption) or "shared preview")
        calls = []

        async def think(**kwargs):
            calls.append(kwargs)
            return prepared((f"{kwargs['friend_name']} saw the image",), reply_to=2)

        monkeypatch.setattr(bot, "think_and_respond", think)
        await group._handle_message(incoming(
            text=None, photo=True, caption="alex river look at https://example.test",
        ))
        await asyncio.gather(*set(group._active_tasks.values()))
        assert len(group._pending_mentions) == 1
        mention = group._pending_mentions[0]
        assert mention.friend_name == "river"
        catchup = group._start_response_task(["river"], group._catch_up(mention, river, {}))
        await catchup
        description.assert_awaited_once_with(group.llm, b"jpeg image", "image/jpeg")
        alex.bot.get_file.assert_awaited_once_with("largest-photo")
        river.bot.get_file.assert_not_awaited()
        casey.bot.get_file.assert_not_awaited()
        assert len(previews) == 1
        assert [call["friend_name"] for call in calls] == ["alex", "casey", "river"]
        for call in calls:
            assert call["image_bytes"] == b"jpeg image"
            assert call["image_media_type"] == "image/jpeg"
            assert call["photo_description"] == "Auxiliary description of the pottery photo"
            assert call["link_previews"] == "shared preview"
        assert "Auxiliary description" not in config.load_friend_memory("alex")
        assert "Auxiliary description" not in config.load_friend_memory("river")
        assert [call.kwargs.get("reply_to_message_id") for call in river.bot.send_message.call_args_list] == [2]
        assert delivery.delays == [17, 22, 3]  # catchup retains its path-specific timing

    asyncio.run(scenario())


def test_photo_with_no_eligible_speaker_does_not_request_visual_work(delivery, monkeypatch):
    group = delivery.group
    alex = delivery.add_friend("alex")
    description = AsyncMock()
    think = AsyncMock()
    monkeypatch.setattr(bot, "describe_photo", description)
    monkeypatch.setattr(bot, "think_and_respond", think)
    asyncio.run(group._handle_message(incoming(text=None, photo=True, caption="alex look")))
    description.assert_not_awaited()
    alex.bot.get_file.assert_not_awaited()
    think.assert_not_awaited()
    assert group._pending_mentions[0].media.photo_file_id == "largest-photo"


@pytest.mark.parametrize("path", ["normal", "catchup"])
@pytest.mark.parametrize("failure", ["download", "description", "empty_description"])
def test_missing_photo_prerequisite_never_becomes_text_reply(
    delivery, monkeypatch, caplog, path, failure,
):
    group = delivery.group
    alex = delivery.add_friend("alex")
    description = AsyncMock(return_value="A visual description")
    if failure == "download":
        alex.bot.get_file.side_effect = TelegramError("photo unavailable")
    elif failure == "description":
        description.side_effect = RuntimeError("vision unavailable")
    else:
        description.return_value = ""
    think = AsyncMock(return_value=prepared())
    monkeypatch.setattr(bot, "describe_photo", description)
    monkeypatch.setattr(bot, "think_and_respond", think)
    media = bot.IncomingMedia(photo_file_id="largest-photo")
    if path == "normal":
        operation = group._staggered_responses(
            [("alex", alex, {})], "human", "(photo) alex look", 1, media=media,
        )
    else:
        mention = bot.PendingMention("alex", "human", "(photo) alex look", 1, time.time(), True, media)
        operation = group._catch_up(mention, alex, {})
    asyncio.run(operation)
    think.assert_not_awaited()
    alex.bot.send_message.assert_not_awaited()
    assert "media prerequisite failed" in caplog.text
    assert len(chat_history.load_messages()) == 1
    assert group._engagement == {}
    assert topics.get_recent_topics() == ""


def test_human_message_cancels_shared_photo_description(delivery, monkeypatch):
    async def scenario():
        group = delivery.group
        delivery.add_friend("alex")
        describing = asyncio.Event()
        think = AsyncMock()

        async def describe(*args):
            describing.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(bot, "describe_photo", describe)
        monkeypatch.setattr(bot, "think_and_respond", think)
        monkeypatch.setattr(bot, "should_respond", lambda *args, **kwargs: True)
        await group._handle_message(incoming(text=None, photo=True, caption="look"))
        await asyncio.wait_for(describing.wait(), 2)
        task = group._active_tasks["alex"]
        monkeypatch.setattr(bot, "should_respond", lambda *args, **kwargs: False)
        await group._handle_message(incoming(message_id=3))
        with pytest.raises(asyncio.CancelledError):
            await task
        think.assert_not_awaited()
        assert group._active_tasks == {}
        assert [message.sender for message in chat_history.load_messages()] == ["human", "human", "human"]

    asyncio.run(scenario())


def test_bot_message_during_flow_is_visible_but_does_not_cancel_or_redraft(delivery, monkeypatch):
    async def scenario():
        group = delivery.group
        alex = delivery.add_friend("alex")
        river = delivery.add_friend("river")
        started = asyncio.Event()
        calls = []

        async def think(**kwargs):
            calls.append(kwargs["friend_name"])
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(bot, "think_and_respond", think)
        task = group._start_response_task(["alex"], group._staggered_responses(
            [("alex", alex, {})], "human", "Original message", 1,
        ))
        await asyncio.wait_for(started.wait(), 2)
        await group._handle_message(incoming(text="River chimed in", sender_id=river.user_id))
        assert group._active_tasks["alex"] is task
        assert not task.done()
        assert calls == ["alex"]
        assert chat_history.load_messages()[-1].sender == "river"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("reply_to", [999, 5])
def test_delivery_rechecks_unknown_and_self_reply_targets(delivery, monkeypatch, reply_to):
    group = delivery.group
    alex = delivery.add_friend("alex")
    chat_history.append_message(ChatMessage(
        timestamp=time.time(), sender="alex", text="Earlier bot text", message_id=5,
    ))
    monkeypatch.setattr(bot, "think_and_respond", AsyncMock(return_value=prepared(reply_to=reply_to)))
    asyncio.run(group._staggered_responses(
        [("alex", alex, {})], "human", "Original message", 1,
    ))
    assert "reply_to_message_id" not in alex.bot.send_message.call_args.kwargs


def test_initiation_uses_prepared_reply_and_triggers_fresh_bot_responses(delivery, monkeypatch):
    async def scenario():
        group = delivery.group
        alex = delivery.add_friend("alex")
        river = delivery.add_friend("river")
        initiated = prepared(("I finished the bowl",), effects=(
            effect("memory", "I finished the bowl", (0,)),
            effect("topic", "pottery progress", (0,)),
        ), reply_to=None, delay=180)
        initiation = AsyncMock(return_value=initiated)
        monkeypatch.setattr(bot, "maybe_initiate", initiation)
        monkeypatch.setattr(bot, "should_respond", lambda *args, **kwargs: True)

        async def think(**kwargs):
            assert kwargs["friend_name"] == "river"
            assert kwargs["sender"] == "alex"
            assert kwargs["message"] == "I finished the bowl"
            assert [message.sender for message in chat_history.load_messages()] == ["human", "alex"]
            assert "pottery progress" in topics.get_recent_topics()
            assert "I finished the bowl" in config.load_friend_memory("alex")
            return prepared(("I want to see the glaze",), reply_to=kwargs["message_id"])

        monkeypatch.setattr(bot, "think_and_respond", think)
        task = group._start_response_task(["alex"], group._initiate("alex", alex, {}, 12))
        await task
        if group._active_tasks:
            await asyncio.gather(*set(group._active_tasks.values()))
        assert "model" not in initiation.call_args.kwargs
        assert delivery.delays == [2, 17]
        assert [message.sender for message in chat_history.load_messages()] == ["human", "alex", "river"]
        assert river.bot.send_message.call_args.kwargs["reply_to_message_id"] == 1001
        assert group._engagement["alex"]["streak"] == 1
        assert group._engagement["river"]["streak"] == 1
        assert group._active_tasks == {}

    asyncio.run(scenario())


def test_failed_first_atom_does_not_transfer_its_effects_or_threading_to_second(delivery):
    group = delivery.group
    alex = delivery.add_friend("alex")
    alex.bot.send_message.side_effect = [
        TelegramError("first atom rejected"), SimpleNamespace(message_id=10),
    ]
    reply = prepared(("An unsent promise", "A visible observation"), effects=(
        effect("memory", "I promised an unsent plan", (0,)),
        effect("topic", "visible observation", (1,)),
        effect("complaint_topic", "requires both texts", (0, 1)),
    ))
    sent = asyncio.run(group._send_messages(alex, "alex", reply, reply_to_message_id=1))
    assert [message.text for message in sent] == ["A visible observation"]
    assert sent[0].reply_to == 0
    assert config.load_friend_memory("alex") == ""
    assert "visible observation" in topics.get_recent_topics()
    assert topics.get_recent_complaints() == ""
    assert group._engagement["alex"]["streak"] == 1


def test_catchup_retains_known_thread_after_recent_history_compaction(delivery, monkeypatch):
    group = delivery.group
    alex = delivery.add_friend("alex")
    # The original incoming mention is no longer in the current chat window.
    mention = bot.PendingMention("alex", "human", "The earlier mention", 500, time.time(), True)
    assert all(message.message_id != 500 for message in chat_history.load_messages())
    monkeypatch.setattr(bot, "think_and_respond", AsyncMock(return_value=prepared(effects=(
        effect("memory", "I caught up on the earlier plan", (0,)),
    ))))
    asyncio.run(group._catch_up(mention, alex, {}))
    assert alex.bot.send_message.call_args.kwargs["reply_to_message_id"] == 500
    assert "I caught up on the earlier plan" in config.load_friend_memory("alex")
    assert delivery.delays == [3]
