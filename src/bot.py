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

        # Run polling, initiation, and catchup concurrently
        await asyncio.gather(
            self._poll_loop(poll_bot, poll_interval),
            self._initiation_loop(),
            self._catchup_loop(),
        )

    async def _poll_loop(self, poll_bot, poll_interval):
        """Poll Telegram for new messages."""
        while True:
            try:
                updates = await poll_bot.bot.get_updates(
                    offset=self._last_update_id + 1,
                    timeout=30,
                    allowed_updates=["message"],
                )

                for update in updates:
                    self._last_update_id = update.update_id
                    if update.message and update.message.chat.id == poll_bot.group_chat_id:
                        await self._handle_message(update.message)

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
                            logger.info(f"{mention.friend_name} caught up ({len(sent)} msgs): {sent[0].text[:50]}...")
                    # Whether they responded or not, they "saw" it — remove from queue

                self._pending_mentions = still_pending

            except Exception as e:
                logger.exception(f"Error in catchup loop: {e}")

    def _is_mentioned(self, name: str, bot: FriendBot, text: str) -> tuple[bool, bool]:
        """Check if a friend is mentioned in a message.

        Returns (mentioned_by_name, mentioned_by_at).
        """
        text_lower = text.lower()
        by_name = name.lower() in text_lower
        by_at = f"@{bot.username}".lower() in text_lower if bot.username else False
        return by_name, by_at

    async def _handle_message(self, message):
        """Process an incoming message and let friends respond."""
        if not message.text:
            return

        sender_id = message.from_user.id
        sender_name = message.from_user.first_name or message.from_user.username

        # Figure out if this is from Travis or from one of the bots
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
            text=message.text,
            message_id=message.message_id,
            reply_to=message.reply_to_message.message_id if message.reply_to_message else 0,
        )
        append_message(chat_msg)

        # Each friend independently decides whether to respond
        tasks = []
        for name, bot in self.bots.items():
            # Don't respond to own messages
            if is_bot_message and bot.user_id == sender_id:
                continue

            friend_config = load_friend_config(name)
            by_name, by_at = self._is_mentioned(name, bot, message.text)
            mentioned = by_name or by_at

            # Schedule gate — are they even "around" right now?
            if not should_respond(friend_config, is_bot_message=is_bot_message,
                                  mentioned=mentioned):
                # If they were mentioned but unavailable, queue for later
                if mentioned:
                    self._pending_mentions.append(PendingMention(
                        friend_name=name,
                        sender=sender_name,
                        text=message.text,
                        message_id=message.message_id,
                        timestamp=time.time(),
                        was_at_mention=by_at,
                    ))
                    logger.info(f"{name} was mentioned but unavailable — queued for later")
                else:
                    logger.debug(f"{name} is unavailable (schedule/chance)")
                continue

            tasks.append(self._friend_consider_response(
                name, bot, friend_config, sender_name, message.text, message.message_id
            ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Periodically compact chat history
        chat_config = self.global_config.get("chat", {})
        await maybe_compact(
            self.claude, self.model,
            max_messages=chat_config.get("max_messages", 100),
            compact_to=chat_config.get("compact_to", 30),
        )

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

                reply_to = result.get("reply_to_message_id")
                sent = await self._send_messages(bot, name, result["messages"],
                                                  reply_to_message_id=reply_to)
                if sent:
                    logger.info(f"{name} responded ({len(sent)} msgs): {sent[0].text[:50]}...")

        except Exception as e:
            logger.exception(f"Error in {name}'s response: {e}")
