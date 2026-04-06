"""Entry point for the friend group bot."""

import asyncio
import logging
import sys

from dotenv import load_dotenv
load_dotenv()

from .bot import FriendGroup


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Quiet down httpx/telegram polling noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def run():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Starting Sudomake Friends...")

    group = FriendGroup()
    await group.setup()

    if not group.bots:
        logger.error("No bots configured! Add friends to friends/ directory.")
        sys.exit(1)

    logger.info(f"Running with {len(group.bots)} friends. Polling for messages...")
    await group.poll_and_respond()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
