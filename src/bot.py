"""Telegram bot management — one bot per friend, all in one process."""

import asyncio
import logging
import random
import time

import anthropic
from telegram import Bot, Update
from telegram.error import TelegramError

from .config import (
    load_config, load_friend_config, get_friend_names,
)
from .chat_history import ChatMessage, append_message, load_messages, maybe_compact
from .schedule import should_respond, get_availability
from .brain import think_and_respond, maybe_initiate
from .news import refresh_all_news, news_age_seconds
from .link_preview import fetch_previews

logger = logging.getLogger(__name__)


class FriendBot:
    """A single friend bot instance."""

    def __init__(self, name: str, config: dict, global_config: dict,
                 claude: anthropic.AsyncAnthropic):
        self.name = name
        self.config = config
        self.global_config = global_config
        self.claude = claude
        self.bot = Bot(token=config["telegram_token"])
        self.group_chat_id = int(global_config["group_chat_id"])
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None

    async def init(self):
        """Initialize bot and get its user info."""
        me = await self.bot.get_me()
        self._bot_user_id = me.id
        self._bot_username = me.username
        logger.info(f"Initialized {self.name} as @{self._bot_username} (id: {self._bot_user_id})")

    @property
    def user_id(self) -> int:
        return self._bot_user_id

    @property
    def username(self) -> str:
        return self._bot_username

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


class PendingMention:
    """A message that mentioned a bot who wasn't available."""
    def __init__(self, friend_name: str, sender: str, text: str,
                 message_id: int, timestamp: float, was_at_mention: bool):
        self.friend_name = friend_name
        self.sender = sender
        self.text = text
        self.message_id = message_id
        self.timestamp = timestamp
        self.was_at_mention = was_at_mention


class FriendGroup:
    """Manages the group of friend bots."""

    def __init__(self):
        self.global_config = load_config()
        self.claude = anthropic.AsyncAnthropic(
            api_key=self.global_config["anthropic_api_key"]
        )
        self.model = self.global_config.get("model", "claude-sonnet-4-6-20250514")
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
                             messages: list[str],
                             reply_to_message_id: int | None = None) -> list[ChatMessage]:
        """Send one or more messages with natural delays between them.
        Returns list of ChatMessages that were sent."""
        sent_msgs = []
        for i, text in enumerate(messages):
            # First message gets reply_to, subsequent ones don't
            reply_to = reply_to_message_id if i == 0 else None
            sent = await bot.send_message(text, reply_to_message_id=reply_to)
            if sent:
                msg = ChatMessage(
                    timestamp=time.time(),
                    sender=name,
                    text=text,
                    message_id=sent.message_id,
                    reply_to=reply_to or 0,
                )
                append_message(msg)
                sent_msgs.append(msg)
            # Delay between split messages (simulate typing)
            if i < len(messages) - 1:
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
            bot = FriendBot(name, config, self.global_config, self.claude)
            await bot.init()
            self.bots[name] = bot
            self._bot_user_ids.add(bot.user_id)

        logger.info(f"Ready with {len(self.bots)} friends")

    async def poll_and_respond(self):
        """Main loop: poll for messages + periodically let bots initiate."""
        poll_bot = next(iter(self.bots.values()))
        poll_interval = self.global_config.get("poll_interval", 2)

        logger.info("Starting message polling...")

        # Run polling, initiation, catchup, and news concurrently
        await asyncio.gather(
            self._poll_loop(poll_bot, poll_interval),
            self._initiation_loop(),
            self._catchup_loop(),
            self._news_loop(),
        )

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
            # Check every 15-45 minutes (randomized to feel natural)
            wait = random.randint(15 * 60, 45 * 60)
            await asyncio.sleep(wait)

            try:
                # How long has the chat been quiet?
                messages = load_messages(limit=1)
                if messages:
                    silence_minutes = int((time.time() - messages[-1].timestamp) / 60)
                else:
                    silence_minutes = 999

                # Only try to initiate if chat has been quiet for at least 10 min
                if silence_minutes < 10:
                    continue

                # Pick one random bot to consider initiating
                name = random.choice(list(self.bots.keys()))
                bot = self.bots[name]
                friend_config = load_friend_config(name)
                availability = get_availability(friend_config)

                if not availability["awake"]:
                    continue

                # Chattier friends are more likely to initiate
                chattiness = friend_config.get("chattiness", 0.5)
                if random.random() > chattiness:
                    continue

                logger.info(f"{name} considering starting a conversation (quiet for {silence_minutes}min)...")

                result = await maybe_initiate(
                    client=self.claude,
                    model=self.model,
                    friend_name=name,
                    friend_config=friend_config,
                    silence_minutes=silence_minutes,
                )

                if result and result.get("messages"):
                    await asyncio.sleep(random.randint(2, 10))

                    sent = await self._send_messages(bot, name, result["messages"])
                    if sent:
                        self._record_spoke(name)
                        logger.info(f"{name} initiated ({len(sent)} msgs): {sent[0].text[:50]}...")

            except Exception as e:
                logger.exception(f"Error in initiation loop: {e}")

    async def _catchup_loop(self):
        """Periodically check if bots with pending mentions are now available."""
        while True:
            await asyncio.sleep(300)  # check every 5 minutes

            if not self._pending_mentions:
                continue

            try:
                still_pending = []
                for mention in self._pending_mentions:
                    # Drop mentions older than 6 hours — too stale
                    age_hours = (time.time() - mention.timestamp) / 3600
                    if age_hours > 6:
                        logger.debug(f"Dropping stale mention for {mention.friend_name}")
                        continue

                    if mention.friend_name not in self.bots:
                        continue

                    bot = self.bots[mention.friend_name]
                    friend_config = load_friend_config(mention.friend_name)
                    availability = get_availability(friend_config)

                    # Are they available now?
                    if not availability["awake"]:
                        still_pending.append(mention)
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
                            still_pending.append(mention)
                            continue
                        catchup_chance *= 0.7

                    if random.random() > catchup_chance:
                        still_pending.append(mention)
                        continue

                    logger.info(f"{mention.friend_name} catching up on mention from {mention.sender}")

                    result = await think_and_respond(
                        client=self.claude,
                        model=self.model,
                        friend_name=mention.friend_name,
                        sender=mention.sender,
                        message=mention.text,
                        message_id=mention.message_id,
                        friend_config=friend_config,
                    )

                    if result and result.get("messages"):
                        await asyncio.sleep(random.randint(3, 15))
                        sent = await self._send_messages(
                            bot, mention.friend_name, result["messages"],
                            reply_to_message_id=mention.message_id,
                        )
                        if sent:
                            self._record_spoke(mention.friend_name)
                            logger.info(f"{mention.friend_name} caught up ({len(sent)} msgs): {sent[0].text[:50]}...")
                    # Whether they responded or not, they "saw" it — remove from queue

                self._pending_mentions = still_pending

            except Exception as e:
                logger.exception(f"Error in catchup loop: {e}")

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
        # /test or /debug — all bots check in
        if message.text and message.text.strip() in ("/test", "/debug"):
            for name, bot in self.bots.items():
                await bot.bot.send_message(
                    chat_id=bot.group_chat_id,
                    text=f"Hi it's me, {name}",
                )
                await asyncio.sleep(1)
            return

        # Download photo if present (largest size)
        image_bytes: bytes | None = None
        image_media_type: str | None = None
        if message.photo:
            try:
                largest = message.photo[-1]
                poll_bot = next(iter(self.bots.values()))
                tg_file = await poll_bot.bot.get_file(largest.file_id)
                data = await tg_file.download_as_bytearray()
                image_bytes = bytes(data)
                # Telegram photos are always served as JPEG
                image_media_type = "image/jpeg"
                logger.info(f"Downloaded photo ({len(image_bytes)} bytes) from message")
            except Exception as e:
                logger.exception(f"Failed to download photo: {e}")
                image_bytes = None

        # Build the text representation of this message
        caption = message.caption or message.text or ""
        if message.photo:
            display_text = f"(photo) {caption}".strip()
        else:
            display_text = caption

        if not display_text:
            return

        # Fetch link previews once for all responders to share
        link_previews = ""
        if caption:
            try:
                link_previews = await asyncio.to_thread(fetch_previews, caption)
                if link_previews:
                    logger.info(f"Fetched link previews ({len(link_previews)} chars)")
            except Exception as e:
                logger.exception(f"Link preview fetch failed: {e}")

        sender_id = message.from_user.id
        sender_name = message.from_user.first_name or message.from_user.username

        # Figure out if this is from a human or from one of the bots
        is_bot_message = sender_id in self._bot_user_ids
        if is_bot_message:
            for name, bot in self.bots.items():
                if bot.user_id == sender_id:
                    sender_name = name
                    break

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

        # Cancel any pending responses — new message changes context
        for name, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
                logger.info(f"{name}'s pending response cancelled — new message arrived")
        self._active_tasks.clear()

        # Determine which friends want to respond
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
                                  engagement_modifier=engagement):
                if mentioned:
                    self._pending_mentions.append(PendingMention(
                        friend_name=name,
                        sender=sender_name,
                        text=display_text,
                        message_id=message.message_id,
                        timestamp=time.time(),
                        was_at_mention=by_at,
                    ))
                    logger.info(f"{name} was mentioned but unavailable — queued for later")
                else:
                    logger.debug(f"{name} is unavailable (schedule/chance)")
                continue

            responders.append((name, bot, friend_config))

        # All responders think concurrently, but send sequentially
        if responders:
            task = asyncio.create_task(
                self._staggered_responses(
                    responders, sender_name, display_text, message.message_id,
                    image_bytes=image_bytes, image_media_type=image_media_type,
                    link_previews=link_previews,
                )
            )
            for name, _, _ in responders:
                self._active_tasks[name] = task

        # Periodically compact chat history
        chat_config = self.global_config.get("chat", {})
        await maybe_compact(
            self.claude, self.model,
            max_messages=chat_config.get("max_messages", 100),
            compact_to=chat_config.get("compact_to", 30),
        )

    async def _staggered_responses(self, responders, sender, message, message_id,
                                    image_bytes: bytes | None = None,
                                    image_media_type: str | None = None,
                                    link_previews: str = ""):
        """All bots think concurrently, but send one at a time with staggered delays.

        After each bot sends, remaining bots get a fresh LLM call to reconsider
        their response in light of what was just said.
        """
        try:
            # Phase 1: Everyone thinks at once
            think_tasks = {}
            for name, bot, friend_config in responders:
                think_tasks[name] = asyncio.create_task(
                    think_and_respond(
                        client=self.claude,
                        model=self.model,
                        friend_name=name,
                        sender=sender,
                        message=message,
                        message_id=message_id,
                        friend_config=friend_config,
                        image_bytes=image_bytes,
                        image_media_type=image_media_type,
                        link_previews=link_previews,
                    )
                )

            results = {}
            for name, task in think_tasks.items():
                try:
                    results[name] = await task
                except Exception as e:
                    logger.exception(f"Error in {name}'s thinking: {e}")

            # Phase 2: Send one at a time, shuffled for variety
            send_order = list(responders)
            random.shuffle(send_order)

            someone_sent = False
            for i, (name, bot, friend_config) in enumerate(send_order):
                result = results.get(name)
                if not result or not result.get("messages"):
                    continue

                # If someone already sent, reconsider with fresh context
                if someone_sent:
                    logger.info(f"{name} reconsidering after another bot responded...")
                    try:
                        result = await think_and_respond(
                            client=self.claude,
                            model=self.model,
                            friend_name=name,
                            sender=sender,
                            message=message,
                            message_id=message_id,
                            friend_config=friend_config,
                            image_bytes=image_bytes,
                            image_media_type=image_media_type,
                            link_previews=link_previews,
                        )
                    except Exception as e:
                        logger.exception(f"Error in {name}'s reconsideration: {e}")
                        continue
                    if not result or not result.get("messages"):
                        continue

                # Stagger delay: first bot gets normal delay, subsequent get extra
                delay = result.get("delay_seconds", 3)
                if someone_sent:
                    delay += random.uniform(3, 8)
                await asyncio.sleep(delay)

                # Prevent self-replies
                reply_to = result.get("reply_to_message_id")
                if reply_to:
                    recent = load_messages(limit=50)
                    for msg in recent:
                        if msg.message_id == reply_to and msg.sender == name:
                            reply_to = None
                            break

                sent = await self._send_messages(bot, name, result["messages"],
                                                  reply_to_message_id=reply_to)
                if sent:
                    self._record_spoke(name)
                    someone_sent = True
                    logger.info(f"{name} responded ({len(sent)} msgs): {sent[0].text[:50]}...")

        except asyncio.CancelledError:
            logger.info("Staggered responses cancelled — new message arrived")
        finally:
            for name, _, _ in responders:
                self._active_tasks.pop(name, None)

    async def _friend_consider_response(
        self, name: str, bot: FriendBot, friend_config: dict,
        sender: str, message: str, message_id: int
    ):
        """Have one friend consider and optionally respond to a message."""
        try:
            result = await think_and_respond(
                client=self.claude,
                model=self.model,
                friend_name=name,
                sender=sender,
                message=message,
                message_id=message_id,
                friend_config=friend_config,
            )

            if result and result.get("messages"):
                delay = result.get("delay_seconds", 3)
                await asyncio.sleep(delay)

                # Prevent self-replies
                reply_to = result.get("reply_to_message_id")
                if reply_to:
                    recent = load_messages(limit=50)
                    for msg in recent:
                        if msg.message_id == reply_to and msg.sender == name:
                            reply_to = None
                            break

                sent = await self._send_messages(bot, name, result["messages"],
                                                  reply_to_message_id=reply_to)
                if sent:
                    self._record_spoke(name)
                    logger.info(f"{name} responded ({len(sent)} msgs): {sent[0].text[:50]}...")

        except asyncio.CancelledError:
            logger.info(f"{name}'s response was interrupted by new message")
        except Exception as e:
            logger.exception(f"Error in {name}'s response: {e}")
        finally:
            self._active_tasks.pop(name, None)
