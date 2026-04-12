"""Fetch news headlines from RSS feeds for each bot."""

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import DATA_DIR, get_friend_names, load_friend_soul

logger = logging.getLogger(__name__)

NEWS_DIR = DATA_DIR / "news"

# General feeds — everyone sees these
GENERAL_FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.npr.org/1001/rss.xml",
]

# Interest-specific feeds, matched by keywords found in SOUL.md
INTEREST_FEEDS = [
    {
        "keywords": ["software", "tech", "programming", "AI", "startup",
                      "engineer", "computer", "code", "open-source", "data sci"],
        "feeds": [
            "https://hnrss.org/frontpage",
            "https://feeds.arstechnica.com/arstechnica/index",
        ],
    },
    {
        "keywords": ["music", "audio", "sound", "band", "recording",
                      "studio", "synth", "guitar", "bass", "concert", "album"],
        "feeds": [
            "https://feeds.npr.org/1039/rss.xml",
        ],
    },
    {
        "keywords": ["science", "biotech", "biology", "research",
                      "drug discovery", "CRISPR", "clinical"],
        "feeds": [
            "https://www.nature.com/nature.rss",
        ],
    },
    {
        "keywords": ["design", "UX", "interface", "graphic", "typography"],
        "feeds": [
            "https://www.smashingmagazine.com/feed/",
        ],
    },
    {
        "keywords": ["environment", "climate", "sustainability",
                      "conservation", "nature", "environmental justice"],
        "feeds": [
            "https://grist.org/feed/",
        ],
    },
    {
        "keywords": ["food", "cooking", "recipe", "restaurant", "chef",
                      "farmers market", "meal"],
        "feeds": [
            "https://www.bonappetit.com/feed/rss",
        ],
    },
    {
        "keywords": ["book", "novel", "fiction", "literary", "reading",
                      "writing", "author", "sci-fi"],
        "feeds": [
            "https://lithub.com/feed/",
        ],
    },
]


def _fetch_feed(url: str, max_items: int = 10) -> list[dict]:
    """Fetch and parse an RSS/Atom feed. Returns list of {title, summary}."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SudomakeFriends/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        root = ET.fromstring(data)

        items = []

        # Try RSS 2.0 format
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if title:
                desc = _clean_summary(desc)
                items.append({"title": title, "summary": desc})

        # Try Atom format (with namespace)
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns)[:max_items]:
                title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()
                if title:
                    summary = _clean_summary(summary)
                    items.append({"title": title, "summary": summary})

        # Try Atom without namespace
        if not items:
            for entry in root.findall(".//entry")[:max_items]:
                title = (entry.findtext("title") or "").strip()
                summary = (entry.findtext("summary") or "").strip()
                if title:
                    summary = _clean_summary(summary)
                    items.append({"title": title, "summary": summary})

        return items
    except Exception as e:
        logger.warning(f"Failed to fetch feed {url}: {e}")
        return []


def _strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _clean_summary(text: str) -> str:
    """Clean up a feed summary — strip HTML, URLs, and metadata noise."""
    text = _strip_html(text)
    # Remove lines that are just URLs or metadata
    lines = text.split("\n")
    clean = [l.strip() for l in lines
             if l.strip() and not l.strip().startswith(("http", "Article URL", "Comments URL", "Points:", "# Comments"))]
    return " ".join(clean)[:150]


def _match_interest_feeds(soul_text: str) -> list[str]:
    """Return RSS feed URLs matching a bot's SOUL.md interests."""
    soul_lower = soul_text.lower()
    feeds = []
    for category in INTEREST_FEEDS:
        if any(kw.lower() in soul_lower for kw in category["keywords"]):
            feeds.extend(category["feeds"])
    return feeds


def fetch_shared_news() -> str:
    """Fetch the combined news pool all friends share.

    Structure: 4 general headlines + one headline from each friend's interest
    feeds. Everyone sees the same pool, so breaking news reaches every bot at
    once and they can't contradict each other about recent events.
    """
    import random

    # 4 general headlines (deduped across the general feeds)
    general_items = []
    for feed_url in GENERAL_FEEDS:
        general_items.extend(_fetch_feed(feed_url, max_items=8))

    seen: set[str] = set()
    general = []
    for item in general_items:
        if item["title"] not in seen:
            seen.add(item["title"])
            general.append(item)
        if len(general) >= 4:
            break

    # One interest headline per friend
    specific = []
    for name in get_friend_names():
        try:
            soul = load_friend_soul(name)
        except Exception:
            continue
        interest_feeds = _match_interest_feeds(soul)
        if not interest_feeds:
            continue
        random.shuffle(interest_feeds)
        picked = None
        for feed_url in interest_feeds:
            items = _fetch_feed(feed_url, max_items=8)
            random.shuffle(items)
            for item in items:
                if item["title"] not in seen:
                    seen.add(item["title"])
                    picked = item
                    break
            if picked:
                break
        if picked:
            specific.append(picked)

    lines = []
    if general:
        lines.append("### What's happening today")
        for item in general:
            summary = f" — {item['summary']}" if item["summary"] else ""
            lines.append(f"- {item['title']}{summary}")

    if specific:
        lines.append("")
        lines.append("### Stuff in your world")
        for item in specific:
            summary = f" — {item['summary']}" if item["summary"] else ""
            lines.append(f"- {item['title']}{summary}")

    return "\n".join(lines) if lines else ""


def refresh_all_news():
    """Fetch fresh news once and write the same content to every friend's NEWS.md."""
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    names = get_friend_names()

    try:
        content = fetch_shared_news()
    except Exception as e:
        logger.exception(f"Failed to fetch shared news: {e}")
        return

    for name in names:
        try:
            news_path = NEWS_DIR / f"{name}.md"
            news_path.write_text(content)
        except Exception as e:
            logger.exception(f"Failed to write news for {name}: {e}")

    logger.info(f"Refreshed shared news for {len(names)} friends ({len(content)} chars)")


def load_friend_news(name: str) -> str:
    """Load a friend's current NEWS.md content."""
    news_path = NEWS_DIR / f"{name}.md"
    if news_path.exists():
        content = news_path.read_text().strip()
        if content:
            return content
    return ""
