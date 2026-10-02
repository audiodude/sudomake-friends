"""Telegram bot management — one bot per friend, all in one process."""

import asyncio
import logging
import random
import time

from .llm import AsyncOpenRouter, DEFAULT_MODEL
from telegram import Bot, Update
from telegram.error import TelegramError

from .config import (
    load_config, load_friend_config, get_friend_names, get_activity_config,
)
from .chat_history import ChatMessage, append_message, load_messages, maybe_compact
from .schedule import should_respond, get_availability
from .brain import describe_photo, think_and_respond, maybe_initiate
from .reply import PreparedReply
from .news import refresh_all_news, news_age_seconds
from .link_preview import fetch_previews

logger = logging.getLogger(__name__)


class FriendBot:
    """A single friend bot instance."""

    def __init__(self, name: str, config: dict, global_config: dict,
                 llm: AsyncOpenRouter):
        self.name = name
        self.config = config
        self.global_config = global_config
        self.llm = llm
        self.bot = Bot(token=config["telegram_token"])
        self.group_chat_id = int(global_config["group_chat_id"])
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        self._can_read_all_group_messages: bool | None = None

    async def init(self):
        """Initialize bot and get its user info."""
        me = await self.bot.get_me()
        self._bot_user_id = me.id
        self._bot_username = me.username
        self._can_read_all_group_messages = bool(me.can_read_all_group_messages)
        logger.info(f"Initialized {self.name} as @{self._bot_username} (id: {self._bot_user_id})")

    @property
    def user_id(self) -> int:
        return self._bot_user_id

    @property
    def username(self) -> str:
        return self._bot_username

    @property
    def can_read_all_group_messages(self) -> bool:
        """True if this bot's Telegram privacy mode is OFF (it can see all
        group messages, not just slash-commands and @mentions)."""
        return bool(self._can_read_all_group_messages)

    async def send_message(self, text: str, reply_to_message_id: int | None = None):
        """Send a message to the group chat."""
        kwargs = {
            "chat_id": self.group_chat_id,
            "text": text,
        }
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id
        try:
            result = await self.bot.send_message(**kwargs)
            return result
        except TelegramError as e:
            logger.error(f"{self.name} failed to send message: {e}")
            return None


class IncomingMedia:
    """Shared prerequisites for one incoming message, including queued mentions."""

    def __init__(self, caption: str = "", photo_file_id: str | None = None,
                 image_bytes: bytes | None = None,
                 image_media_type: str | None = None):
        self.caption = caption
        self.photo_file_id = photo_file_id
        self.image_bytes = image_bytes
        self.image_media_type = image_media_type
        self.photo_description = ""
        self.link_previews = ""
        self._previews_loaded = False
        self._lock = asyncio.Lock()

    @property
    def has_photo(self) -> bool:
        return self.photo_file_id is not None or self.image_bytes is not None


class PendingMention:
    """A message that mentioned a bot who wasn't available."""
    def __init__(self, friend_name: str, sender: str, text: str,
                 message_id: int, timestamp: float, was_at_mention: bool,
                 media: IncomingMedia | None = None):
        self.friend_name = friend_name
        self.sender = sender
        self.text = text
        self.message_id = message_id
        self.timestamp = timestamp
        self.was_at_mention = was_at_mention
        self.media = media


class FriendGroup:
    """Manages the group of friend bots."""

    def __init__(self):
        self.global_config = load_config()
        self.llm = AsyncOpenRouter(
            api_key=self.global_config.get("openrouter_api_key", ""),
            helper_model=self.global_config.get("helper_model"),
        )
        # This configurable model is used only for existing chat compaction.
        self.model = self.global_config.get("model") or DEFAULT_MODEL
        self.bots: dict[str, FriendBot] = {}
        self._bot_user_ids: set[int] = set()
        self._last_update_id: int = 0
        self._processing_lock = asyncio.Lock()
        self._pending_mentions: list[PendingMention] = []
        # Engagement tracking: per-bot momentum that decays over time
        # {bot_name: {"last_spoke": timestamp, "last_replied_to": timestamp, "streak": int}}
        self._engagement: dict[str, dict] = {}
        # Active response tasks per bot — cancelled when new message arrives
        self._active_tasks: dict[str, asyncio.Task] = {}
        # Includes cancelled/replaced operations until their cancellation drains.
        self._response_tasks: set[asyncio.Task] = set()

    def _cancel_responses(self):
        """Invalidate every current opportunity without stopping periodic loops."""
        for task in set(self._active_tasks.values()):
            if not task.done():
                task.cancel()
        self._active_tasks.clear()

    def _release_response_task(self, names, task):
        for name in names:
            if self._active_tasks.get(name) is task:
                self._active_tasks.pop(name)

    def _start_response_task(self, names, operation):
        task = asyncio.create_task(operation)
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)
        names = tuple(names)
        for name in names:
            self._active_tasks[name] = task
        # Also handles cancellation before the coroutine first starts.
        task.add_done_callback(lambda done: self._release_response_task(names, done))
        return task

    async def _await_response_task(self, task):
        """A human can cancel the child without cancelling its periodic owner."""
        try:
            await asyncio.shield(task)
            return True
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            return False

    async def _prepare_media(self, media: IncomingMedia | None):
        if media is None:
            return
        # Immediate responders and later catchups reuse the same visual context.
        async with media._lock:
            if media.has_photo:
                if not media.image_bytes:
                    poll_bot = next(iter(self.bots.values()))
                    tg_file = await poll_bot.bot.get_file(media.photo_file_id)
                    data = await tg_file.download_as_bytearray()
                    if not data:
                        raise ValueError("Photo download returned no image")
                    media.image_bytes = bytes(data)
                    media.image_media_type = "image/jpeg"
                if not media.image_media_type:
                    raise ValueError("Photo media type is missing")
                if not media.photo_description:
                    description = await describe_photo(
                        self.llm, media.image_bytes, media.image_media_type,
                    )
                    if not isinstance(description, str) or not description.strip():
                        raise ValueError("Photo description returned no visual context")
                    media.photo_description = description
            if not media._previews_loaded:
                if media.caption:
                    try:
                        media.link_previews = await asyncio.to_thread(
                            fetch_previews, media.caption,
                        )
                    except Exception:
                        logger.exception("Link preview fetch failed")
                media._previews_loaded = True

    def _reply_target(self, name: str, message_id: int | None) -> int | None:
        if not message_id:
            return None
        for message in load_messages(limit=50):
            if message.message_id == message_id:
                return message_id if message.sender != name else None
        return None

    def _get_engagement_modifier(self, name: str) -> float:
        """Return a multiplier (0.0-1.0+) based on how engaged this bot is
        in the current conversation. Decays over time."""
        if name not in self._engagement:
            return 0.0

        eng = self._engagement[name]
        now = time.time()

        # How recently did they speak?
        since_spoke = (now - eng.get("last_spoke", 0)) / 60  # minutes
        # How recently were they replied to?
        since_replied_to = (now - eng.get("last_replied_to", 0)) / 60
        streak = eng.get("streak", 0)

        # Decay: full effect within 1 min, fades to zero by 8 min
        def _decay(minutes: float) -> float:
            if minutes < 1:
                return 1.0
            if minutes > 8:
                return 0.0
            return 1.0 - (minutes - 1) / 7

        spoke_boost = _decay(since_spoke) * 0.067       # recently talked = small boost
        replied_boost = _decay(since_replied_to) * 0.10  # got a reply = moderate boost
        streak_boost = min(streak * 0.033, 0.10)         # back-and-forth = builds slowly

        # Streak decays too
        if since_spoke > 5:
            streak_boost = 0.0

        return spoke_boost + replied_boost + streak_boost

    def _record_spoke(self, name: str):
        """Record that a bot sent a message."""
        eng = self._engagement.setdefault(name, {})
        eng["last_spoke"] = time.time()
        eng["streak"] = eng.get("streak", 0) + 1

    def _record_replied_to(self, name: str):
        """Record that someone replied to or followed up on this bot's message."""
        eng = self._engagement.setdefault(name, {})
        eng["last_replied_to"] = time.time()

    async def _send_messages(self, bot: FriendBot, name: str,
                             reply: PreparedReply,
                             reply_to_message_id: int | None = None) -> list[ChatMessage]:
        """Commit each confirmed atom before any subsequent await or history work."""
        sent_msgs = []
        for i, atom in enumerate(reply.atoms):
            # First surviving atom gets reply_to, subsequent ones don't.
            reply_to = reply_to_message_id if i == 0 else None
            sent = await bot.send_message(atom.text, reply_to_message_id=reply_to)
            if sent:
                reply.commit_atom(name, atom.index)
                if not sent_msgs:
                    self._record_spoke(name)
                msg = ChatMessage(
                    timestamp=time.time(),
                    sender=name,
                    text=atom.text,
                    message_id=sent.message_id,
                    reply_to=reply_to or 0,
                )
                append_message(msg)
                sent_msgs.append(msg)
            # Delay between split messages (simulate typing).
            if i < len(reply.atoms) - 1:
                await asyncio.sleep(max(2.0, min(12.0, random.gauss(7.0, 2.5))))
        return sent_msgs

    async def setup(self):
        """Initialize all friend bots."""
        friend_names = get_friend_names()
        logger.info(f"Setting up {len(friend_names)} friends: {friend_names}")

        for name in friend_names:
            config = load_friend_config(name)
            if not config.get("telegram_token"):
                logger.warning(f"Skipping {name} — no telegram token configured")
                continue
            bot = FriendBot(name, config, self.global_config, self.llm)
            await bot.init()
            self.bots[name] = bot
            self._bot_user_ids.add(bot.user_id)

        logger.info(f"Ready with {len(self.bots)} friends")

    async def aclose(self):
        """Stop in-flight responses before closing the shared LLM client."""
        tasks = self._response_tasks | set(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.llm.aclose()

    def _select_poll_bot(self) -> "FriendBot":
        """Pick the single bot that reads all group messages for everyone.

        It MUST have Telegram privacy mode OFF, or it only receives slash-commands
        and @mentions — ordinary chat never reaches it and the friends look dead
        while `/test` still works. Prefer a privacy-off friend; if none qualify,
        fall back to the first and warn loudly with the fix.
        """
        readers = [b for b in self.bots.values() if b.can_read_all_group_messages]
        if readers:
            chosen = readers[0]
        else:
            chosen = next(iter(self.bots.values()))
            logger.warning(
                "No friend has Telegram privacy mode OFF — poll bot @%s will only "
                "receive slash-commands, NOT normal chat, so the friends will look "
                "silent. Fix: BotFather /setprivacy -> Disable, then REMOVE and "
                "RE-ADD @%s to the group (privacy is bound at join time).",
                chosen.username, chosen.username,
            )
        logger.info(
            f"Poll bot: {chosen.name} (@{chosen.username}), "
            f"privacy mode {'OFF' if chosen.can_read_all_group_messages else 'ON'}"
        )
        return chosen

    async def poll_and_respond(self):
        """Main loop: poll for messages + periodically let bots initiate."""
        poll_bot = self._select_poll_bot()
        poll_interval = self.global_config.get("poll_interval", 2)

        logger.info("Starting message polling...")

        # Run polling, initiation, catchup, and news concurrently
        tasks = [
            asyncio.create_task(self._poll_loop(poll_bot, poll_interval)),
            asyncio.create_task(self._initiation_loop()),
            asyncio.create_task(self._catchup_loop()),
            asyncio.create_task(self._news_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_loop(self, poll_bot, poll_interval):
        """Poll Telegram for new messages."""
        while True:
            try:
                updates = await poll_bot.bot.get_updates(
                    offset=self._last_update_id + 1,
                    timeout=30,
                    allowed_updates=["message", "message_reaction"],
                )

                for update in updates:
                    self._last_update_id = update.update_id
                    if update.message and update.message.chat.id == poll_bot.group_chat_id:
                        await self._handle_message(update.message)
                    elif update.message_reaction and update.message_reaction.chat.id == poll_bot.group_chat_id:
                        self._handle_reaction(update.message_reaction)

            except TelegramError as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.exception(f"Unexpected error in poll loop: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(poll_interval)

    async def _initiation_loop(self):
        """Periodically give bots a chance to start conversations."""
        import random

        # Wait a bit before first check so polling can start
        await asyncio.sleep(60)

        while True:
            # Re-read tuning each iteration so config.yaml edits apply live.
            init_cfg = get_activity_config()["initiation"]
            # Check every N-M minutes (randomized to feel natural). Frequent
            # checks keep the group chat alive instead of letting it flatline.
            wait = random.randint(
                int(init_cfg["check_min_minutes"] * 60),
                int(init_cfg["check_max_minutes"] * 60),
            )
            await asyncio.sleep(wait)

            try:
                # How long has the chat been quiet?
                messages = load_messages(limit=1)
                if messages:
                    silence_minutes = int((time.time() - messages[-1].timestamp) / 60)
                else:
                    silence_minutes = 999

                # Only try to initiate once the chat's been quiet this long —
                # low enough that the group revives conversation on its own quickly.
                if silence_minutes < init_cfg["silence_threshold_minutes"]:
                    continue

                # Mild decay as time passes since the last human message, but with
                # a high floor so the friends keep the chat lively on their own even
                # when the human is away — they should always have something going.
                bot_names = set(get_friend_names())
                recent = load_messages(limit=50)
                last_human_ts = None
                for msg in reversed(recent):
                    if msg.sender not in bot_names:
                        last_human_ts = msg.timestamp
                        break
                if last_human_ts:
                    hours_since_human = (time.time() - last_human_ts) / 3600
                else:
                    hours_since_human = 24
                decay = max(init_cfg["decay_floor"],
                            1.0 / (1 + hours_since_human / init_cfg["decay_divisor_hours"]))

                # Pick one random bot to consider initiating
                name = random.choice(list(self.bots.keys()))
                bot = self.bots[name]
                friend_config = load_friend_config(name)
                availability = get_availability(friend_config)

                if not availability["awake"]:
                    continue

                # Chattier friends initiate more. The dampener (config) scales the
                # whole group's initiation rate; the anti-repetition guardrails in
                # the prompt keep higher volume from turning into noise.
                chattiness = friend_config.get("chattiness", 0.5)
                if random.random() > chattiness * init_cfg["dampener"] * decay:
                    continue

                logger.info(f"{name} considering starting a conversation (quiet for {silence_minutes}min, {hours_since_human:.1f}h since human, decay={decay:.2f})...")

                if name in self._active_tasks and not self._active_tasks[name].done():
                    continue
                task = self._start_response_task(
                    [name],
                    self._initiate(name, bot, friend_config, silence_minutes),
                )
                await self._await_response_task(task)

            except Exception as e:
                logger.exception(f"Error in initiation loop: {e}")

    async def _initiate(self, name, bot, friend_config, silence_minutes):
        try:
            reply = await maybe_initiate(
                client=self.llm,
                friend_name=name,
                friend_config=friend_config,
                silence_minutes=silence_minutes,
            )
            if reply is None or not reply.atoms:
                return
            await asyncio.sleep(random.randint(2, 10))
            sent = await self._send_messages(
                bot, name, reply,
                reply_to_message_id=self._reply_target(name, reply.reply_to_message_id),
            )
            if not sent:
                return
            logger.info(f"{name} initiated ({len(sent)} msgs): {sent[0].text[:50]}...")

            # Polling may not see the initiating bot's own sends.
            reply_cfg = get_activity_config()["reply"]
            responders = []
            for other_name, other_bot in self.bots.items():
                if other_name == name:
                    continue
                active = self._active_tasks.get(other_name)
                if active is not None and not active.done():
                    continue
                other_config = load_friend_config(other_name)
                engagement = self._get_engagement_modifier(other_name)
                if should_respond(
                    other_config, is_bot_message=True,
                    engagement_modifier=engagement,
                    human_dampener=reply_cfg["human_dampener"],
                    bot_dampener=reply_cfg["bot_dampener"],
                ):
                    responders.append((other_name, other_bot, other_config))
            if responders:
                self._start_response_task(
                    [n for n, _, _ in responders],
                    self._staggered_responses(
                        responders, name, sent[0].text, sent[0].message_id,
                    ),
                )
        except asyncio.CancelledError:
            logger.info("%s's initiation cancelled — new message or shutdown", name)
            raise
        except Exception:
            logger.exception("Initiation pipeline failed for %s", name)
        finally:
            self._release_response_task([name], asyncio.current_task())

    async def _catchup_loop(self):
        """Periodically check if bots with pending mentions are now available."""
        while True:
            await asyncio.sleep(300)  # check every 5 minutes

            if not self._pending_mentions:
                continue

            try:
                for mention in list(self._pending_mentions):
                    # Drop mentions older than 6 hours — too stale
                    age_hours = (time.time() - mention.timestamp) / 3600
                    if age_hours > 6:
                        logger.debug(f"Dropping stale mention for {mention.friend_name}")
                        self._pending_mentions.remove(mention)
                        continue

                    if mention.friend_name not in self.bots:
                        self._pending_mentions.remove(mention)
                        continue

                    bot = self.bots[mention.friend_name]
                    friend_config = load_friend_config(mention.friend_name)
                    availability = get_availability(friend_config)

                    # Don't replace another in-flight opportunity for this friend.
                    active = self._active_tasks.get(mention.friend_name)
                    if not availability["awake"] or (active is not None and not active.done()):
                        continue

                    # For @mentions, very likely to catch up. For name mentions, moderate.
                    if mention.was_at_mention:
                        catchup_chance = 0.85
                    else:
                        catchup_chance = 0.5

                    # At work? Depends on work type
                    if availability["at_work"]:
                        work_type = friend_config.get("work_type", "office")
                        if work_type != "office":
                            continue
                        catchup_chance *= 0.7

                    if random.random() > catchup_chance:
                        continue

                    logger.info(f"{mention.friend_name} catching up on mention from {mention.sender}")

                    self._pending_mentions.remove(mention)
                    task = self._start_response_task(
                        [mention.friend_name],
                        self._catch_up(mention, bot, friend_config),
                    )
                    if not await self._await_response_task(task):
                        # A new human invalidates the whole current catchup pass.
                        break

            except Exception as e:
                logger.exception(f"Error in catchup loop: {e}")

    async def _catch_up(self, mention, bot, friend_config):
        name = mention.friend_name
        try:
            try:
                await self._prepare_media(mention.media)
            except Exception:
                logger.exception("Catchup media prerequisite failed for %s", name)
                return
            media = mention.media
            reply = await think_and_respond(
                client=self.llm,
                friend_name=name,
                sender=mention.sender,
                message=mention.text,
                message_id=mention.message_id,
                friend_config=friend_config,
                image_bytes=media.image_bytes if media else None,
                image_media_type=media.image_media_type if media else None,
                photo_description=media.photo_description if media else "",
                link_previews=media.link_previews if media else "",
            )
            if reply is None or not reply.atoms:
                return
            await asyncio.sleep(random.randint(3, 15))
            # The stored mention proves this target even after history compaction.
            sent = await self._send_messages(
                bot, name, reply,
                reply_to_message_id=mention.message_id if mention.sender != name else None,
            )
            if sent:
                logger.info(f"{name} caught up ({len(sent)} msgs): {sent[0].text[:50]}...")
        except asyncio.CancelledError:
            logger.info("%s's catchup cancelled — new message or shutdown", name)
            raise
        except Exception:
            logger.exception("Catchup pipeline failed for %s", name)
        finally:
            self._release_response_task([name], asyncio.current_task())

    async def _news_loop(self):
        """Refresh news headlines twice daily at 7am and 6pm ET, plus on startup if stale."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        last_refresh = None
        STARTUP_STALENESS_SECONDS = 4 * 3600

        # Refresh on startup only if news is missing or older than the staleness threshold.
        # Avoids hammering feeds on rapid restarts and keeps bots on consistent news context.
        try:
            age = news_age_seconds()
            if age is None or age > STARTUP_STALENESS_SECONDS:
                refresh_all_news()
                last_refresh = datetime.now(et)
                logger.info("Initial news refresh complete")
            else:
                last_refresh = datetime.fromtimestamp(time.time() - age, et)
                logger.info(f"News is {age/3600:.1f}h old, skipping initial refresh")
        except Exception as e:
            logger.exception(f"Initial news refresh failed: {e}")

        while True:
            await asyncio.sleep(1800)  # check every 30 min

            try:
                now = datetime.now(et)
                hour = now.hour

                should_refresh = False
                if 7 <= hour < 8 and (
                    last_refresh is None
                    or last_refresh.hour < 7
                    or last_refresh.date() < now.date()
                ):
                    should_refresh = True
                elif 18 <= hour < 19 and (
                    last_refresh is None
                    or last_refresh.hour < 18
                    or last_refresh.date() < now.date()
                ):
                    should_refresh = True

                if should_refresh:
                    refresh_all_news()
                    last_refresh = now
                    logger.info(f"News refreshed at {now.strftime('%H:%M %Z')}")
            except Exception as e:
                logger.exception(f"Error in news loop: {e}")

    def _is_mentioned(self, name: str, bot: FriendBot, text: str) -> tuple[bool, bool]:
        """Check if a friend is mentioned in a message.

        Returns (mentioned_by_name, mentioned_by_at).
        """
        text_lower = text.lower()
        by_name = name.lower() in text_lower
        by_at = f"@{bot.username}".lower() in text_lower if bot.username else False
        return by_name, by_at

    def _handle_reaction(self, reaction):
        """Record an emoji reaction in chat history (no bot response triggered)."""
        user = reaction.user
        if not user:
            return

        # Map bot user IDs to friend names
        reactor = None
        if user.id in self._bot_user_ids:
            for name, bot in self.bots.items():
                if bot.user_id == user.id:
                    reactor = name
                    break
        if not reactor:
            reactor = user.first_name or user.username or "someone"

        # Figure out which emoji was added (new - old)
        old_emojis = set()
        for r in (reaction.old_reaction or []):
            if hasattr(r, "emoji"):
                old_emojis.add(r.emoji)
        new_emojis = []
        for r in (reaction.new_reaction or []):
            if hasattr(r, "emoji") and r.emoji not in old_emojis:
                new_emojis.append(r.emoji)

        if not new_emojis:
            return

        emoji_str = " ".join(new_emojis)
        msg = ChatMessage(
            timestamp=time.time(),
            sender=reactor,
            text=emoji_str,
            message_id=0,
            reply_to=reaction.message_id,
            is_reaction=True,
        )
        append_message(msg)
        logger.debug(f"{reactor} reacted {emoji_str} to msg:{reaction.message_id}")

    async def _handle_message(self, message):
        """Process an incoming message and let friends respond."""
        sender_id = message.from_user.id
        is_bot_message = sender_id in self._bot_user_ids
        if not is_bot_message:
            # Cancel before any download, preview, or other awaited preparation.
            self._cancel_responses()
        # /test or /debug — all bots check in
        if message.text and message.text.strip() in ("/test", "/debug"):
            for name, bot in self.bots.items():
                await bot.bot.send_message(
                    chat_id=bot.group_chat_id,
                    text=f"Hi it's me, {name}",
                )
                await asyncio.sleep(1)
            return

        # Build the text representation of this message
        caption = message.caption or message.text or ""
        if message.photo:
            display_text = f"(photo) {caption}".strip()
        else:
            display_text = caption

        if not display_text:
            return

        media = IncomingMedia(
            caption=caption,
            photo_file_id=message.photo[-1].file_id if message.photo else None,
        )
        sender_name = message.from_user.first_name or message.from_user.username
        if is_bot_message:
            for name, bot in self.bots.items():
                if bot.user_id == sender_id:
                    sender_name = name
                    break
            logger.info(f"Processing bot message from {sender_name}: {display_text[:60]}")
        else:
            logger.info(f"Processing human message from {sender_name}: {display_text[:60]}")

        # Log the message to chat history
        chat_msg = ChatMessage(
            timestamp=time.time(),
            sender=sender_name,
            text=display_text,
            message_id=message.message_id,
            reply_to=message.reply_to_message.message_id if message.reply_to_message else 0,
        )
        append_message(chat_msg)

        # Track engagement: if this message follows a bot's message,
        # that bot is being "replied to" (conversation is continuing)
        recent = load_messages(limit=5)
        if len(recent) >= 2:
            for prev_msg in reversed(recent[:-1]):  # skip the one we just added
                if prev_msg.sender in self.bots:
                    self._record_replied_to(prev_msg.sender)
                break  # only check the most recent prior message

        # If a bot message arrives while a staggered flow is already running,
        # Don't cancel it: later speakers build fresh context at their own turn.
        # Only create a new response chain for bot messages when idle (e.g. initiations).
        has_active_flow = any(not t.done() for t in self._active_tasks.values())
        if is_bot_message and has_active_flow:
            logger.info(f"Bot message from {sender_name} while staggered flow active — letting flow handle it")
            return


        # Determine which friends want to respond
        reply_cfg = get_activity_config()["reply"]
        responders = []
        for name, bot in self.bots.items():
            if is_bot_message and bot.user_id == sender_id:
                continue

            friend_config = load_friend_config(name)
            by_name, by_at = self._is_mentioned(name, bot, display_text)
            mentioned = by_name or by_at

            engagement = self._get_engagement_modifier(name)
            if not should_respond(friend_config, is_bot_message=is_bot_message,
                                  mentioned=mentioned,
                                  engagement_modifier=engagement,
                                  human_dampener=reply_cfg["human_dampener"],
                                  bot_dampener=reply_cfg["bot_dampener"]):
                if mentioned:
                    self._pending_mentions.append(PendingMention(
                        friend_name=name,
                        sender=sender_name,
                        text=display_text,
                        message_id=message.message_id,
                        timestamp=time.time(),
                        was_at_mention=by_at,
                        media=media,
                    ))
                    logger.info(f"{name} was mentioned but unavailable — queued for later")
                else:
                    logger.debug(f"{name} is unavailable (schedule/chance)")
                continue

            responders.append((name, bot, friend_config))

        if is_bot_message:
            logger.info(f"Bot message responders: {[n for n, _, _ in responders] if responders else 'NONE'}")

        # Generate only at each shuffled speaking turn, after prior commits.
        if responders:
            self._start_response_task(
                [name for name, _, _ in responders],
                self._staggered_responses(
                    responders, sender_name, display_text, message.message_id,
                    media=media,
                ),
            )

        # Periodically compact chat history
        chat_config = self.global_config.get("chat", {})
        await maybe_compact(
            self.llm, self.model,
            max_messages=chat_config.get("max_messages", 100),
            compact_to=chat_config.get("compact_to", 30),
        )

    async def _staggered_responses(self, responders, sender, message, message_id,
                                   media: IncomingMedia | None = None):
        """Generate each friend's one reply using the latest delivered state."""
        try:
            if not responders:
                return
            try:
                await self._prepare_media(media)
            except Exception:
                logger.exception("Reply media prerequisite failed for msg:%s", message_id)
                return
            send_order = list(responders)
            random.shuffle(send_order)
            someone_sent = False
            for name, bot, friend_config in send_order:
                last_spoke = self._engagement.get(name, {}).get("last_spoke")
                try:
                    reply = await think_and_respond(
                        client=self.llm,
                        friend_name=name,
                        sender=sender,
                        message=message,
                        message_id=message_id,
                        friend_config=friend_config,
                        image_bytes=media.image_bytes if media else None,
                        image_media_type=media.image_media_type if media else None,
                        photo_description=media.photo_description if media else "",
                        link_previews=media.link_previews if media else "",
                    )
                    if reply is None or not reply.atoms:
                        continue
                    delay = reply.delay_seconds
                    if someone_sent:
                        delay += random.uniform(3, 8)
                    await asyncio.sleep(delay)
                    sent = await self._send_messages(
                        bot, name, reply,
                        reply_to_message_id=self._reply_target(name, reply.reply_to_message_id),
                    )
                    if sent:
                        someone_sent = True
                        logger.info(f"{name} responded ({len(sent)} msgs): {sent[0].text[:50]}...")
                except Exception:
                    logger.exception("Response pipeline failed for %s", name)
                    # A partially delivered batch still warrants the stagger.
                    someone_sent = someone_sent or (
                        self._engagement.get(name, {}).get("last_spoke") != last_spoke
                    )
        except asyncio.CancelledError:
            logger.info("Staggered responses cancelled — new message or shutdown")
            raise
        finally:
            self._release_response_task(
                [name for name, _, _ in responders], asyncio.current_task(),
            )

