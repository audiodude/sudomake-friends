"""Fetch lightweight previews for URLs shared in chat.

Given a message text, find URLs, fetch them (with caps), and return a
short text block suitable for injecting into a bot's prompt as context.
"""

import logging
import re
import urllib.request
from html import unescape
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

MAX_URLS_PER_MESSAGE = 2
MAX_BYTES = 100_000
TIMEOUT = 10
MAX_TEXT_CHARS = 600

# Hosts we don't bother fetching — they require JS/login or are noisy
SKIP_HOSTS = {
    "twitter.com", "x.com", "t.co",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com",
    "tiktok.com", "www.tiktok.com",
    "linkedin.com", "www.linkedin.com",
}


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    urls = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,);:!?]")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= MAX_URLS_PER_MESSAGE:
            break
    return urls


def _fetch_one(url: str) -> dict | None:
    host = (urlparse(url).hostname or "").lower()
    if host in SKIP_HOSTS:
        return {"url": url, "title": "", "text": f"(link to {host} — preview unavailable)"}

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SudomakeFriends/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower() and "xml" not in ctype.lower():
                return {"url": url, "title": "", "text": f"(non-HTML content: {ctype.split(';')[0]})"}
            raw = resp.read(MAX_BYTES)
    except Exception as e:
        logger.warning(f"Link preview failed for {url}: {e}")
        return None

    try:
        html = raw.decode("utf-8", errors="replace")
    except Exception:
        return None

    title = ""
    m = TITLE_RE.search(html)
    if m:
        title = WS_RE.sub(" ", unescape(TAG_RE.sub("", m.group(1)))).strip()[:200]

    body = SCRIPT_RE.sub(" ", html)
    body = TAG_RE.sub(" ", body)
    body = unescape(body)
    body = WS_RE.sub(" ", body).strip()
    body = body[:MAX_TEXT_CHARS]

    return {"url": url, "title": title, "text": body}


def fetch_previews(text: str) -> str:
    """Return a formatted preview block for URLs in text, or empty string."""
    urls = extract_urls(text)
    if not urls:
        return ""

    blocks = []
    for url in urls:
        preview = _fetch_one(url)
        if not preview:
            blocks.append(f"- {url}\n  (couldn't fetch)")
            continue
        title = preview["title"] or "(no title)"
        body = preview["text"] or ""
        blocks.append(f"- {url}\n  Title: {title}\n  Excerpt: {body}")

    return "\n".join(blocks)
