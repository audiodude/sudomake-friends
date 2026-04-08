#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40.0",
#     "pyyaml>=6.0",
# ]
# ///
"""Interactive friend group initialization — single self-contained script.

Run directly:  uv run https://raw.githubusercontent.com/audiodude/sudomake-friends/main/scripts/initialize.py
Or locally:    uv run scripts/initialize.py
"""

import curses
import hashlib
import json
import os
import re
import readline
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote, quote

import yaml


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

MODEL = "claude-4-sonnet-20250514"
CANDIDATE_COUNT = 8
SCRAPE_TIMEOUT = 180
REPO_URL = "https://github.com/audiodude/sudomake-friends.git"
TARBALL_URL = "https://github.com/audiodude/sudomake-friends/archive/main.tar.gz"
HOME_DIR = Path.home() / ".sudomake-friends"


# ═══════════════════════════════════════════════════════════════════════════════
# Platform Plugins
# ═══════════════════════════════════════════════════════════════════════════════

def _mastodon_detect(url: str) -> bool:
    import urllib.request
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not (path.startswith("@") and "/" not in path):
        return False
    try:
        check = f"https://{parsed.netloc}/api/v1/instance"
        req = urllib.request.Request(check, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _mastodon_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    instance = parsed.netloc
    username = parsed.path.strip("/").lstrip("@")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"mastodon:{instance}/{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached posts for @{username}@{instance}")
        return cache_file.read_text()

    print(f"  Fetching @{username}@{instance}...")

    lookup_url = f"https://{instance}/api/v1/accounts/lookup?acct={username}"
    try:
        with urllib.request.urlopen(lookup_url, timeout=10) as resp:
            account = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  Lookup failed: {e}")
        return ""

    account_id = account["id"]
    bio = re.sub(r'<[^>]+>', '', account.get("note", ""))

    parts = [f"Mastodon: @{username}@{instance}"]
    if account.get("display_name"):
        parts.append(f"Name: {account['display_name']}")
    if bio:
        parts.append(f"Bio: {bio}")

    posts = []
    max_id = None
    for _ in range(3):
        api_url = (
            f"https://{instance}/api/v1/accounts/{account_id}/statuses"
            f"?limit=40&exclude_reblogs=true"
        )
        if max_id:
            api_url += f"&max_id={max_id}"
        try:
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                statuses = json.loads(resp.read().decode())
        except Exception:
            break
        if not statuses:
            break
        for s in statuses:
            text = re.sub(r'<[^>]+>', '', s.get("content", "")).strip()
            if text:
                posts.append(text)
            max_id = s["id"]

    print(f"  Got {len(posts)} posts")
    parts.append(f"\n--- Posts ({len(posts)}) ---")
    parts.extend(posts)

    result = "\n\n".join(parts)[:30000]
    cache_file.write_text(result)
    return result


def _bluesky_detect(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "bsky.app" and "/profile/" in parsed.path


def _bluesky_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    API = "https://public.api.bsky.app"
    parsed = urlparse(url)
    handle = parsed.path.split("/profile/")[-1].strip("/")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"bluesky:{handle}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Bluesky posts for {handle}")
        return cache_file.read_text()

    print(f"  Fetching Bluesky @{handle}...")

    try:
        profile_url = f"{API}/xrpc/app.bsky.actor.getProfile?actor={handle}"
        with urllib.request.urlopen(profile_url, timeout=10) as resp:
            profile = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  Profile fetch failed: {e}")
        return ""

    parts = [f"Bluesky: @{handle}"]
    if profile.get("displayName"):
        parts.append(f"Name: {profile['displayName']}")
    if profile.get("description"):
        parts.append(f"Bio: {profile['description']}")

    posts = []
    cursor = None
    for _ in range(3):
        feed_url = f"{API}/xrpc/app.bsky.feed.getAuthorFeed?actor={handle}&limit=50"
        if cursor:
            feed_url += f"&cursor={cursor}"
        try:
            with urllib.request.urlopen(feed_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            break
        for item in data.get("feed", []):
            post = item.get("post", {}).get("record", {})
            text = post.get("text", "").strip()
            if text:
                posts.append(text)
        cursor = data.get("cursor")
        if not cursor:
            break

    print(f"  Got {len(posts)} posts")
    parts.append(f"\n--- Posts ({len(posts)}) ---")
    parts.extend(posts)

    result = "\n\n".join(parts)[:30000]
    cache_file.write_text(result)
    return result


def _github_detect(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc != "github.com":
        return False
    path = parsed.path.strip("/")
    return bool(path) and "/" not in path


def _github_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    API = "https://api.github.com"
    parsed = urlparse(url)
    username = parsed.path.strip("/")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"github:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached GitHub data for {username}")
        return cache_file.read_text()

    print(f"  Fetching GitHub @{username}...")
    headers = {"User-Agent": "sudomake-friends/1.0"}

    try:
        req = urllib.request.Request(f"{API}/users/{username}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            user = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  Failed: {e}")
        return ""

    parts = [f"GitHub: {username}"]
    if user.get("name"):
        parts.append(f"Name: {user['name']}")
    if user.get("bio"):
        parts.append(f"Bio: {user['bio']}")
    if user.get("location"):
        parts.append(f"Location: {user['location']}")
    if user.get("blog"):
        parts.append(f"Website: {user['blog']}")
    parts.append(f"Public repos: {user.get('public_repos', 0)}")

    try:
        req = urllib.request.Request(
            f"{API}/users/{username}/repos?sort=stars&per_page=30&type=owner",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            repos = json.loads(resp.read().decode())
    except Exception:
        repos = []

    if repos:
        parts.append("\n--- Top repositories ---")
        for r in repos[:20]:
            lang = r.get("language") or "?"
            stars = r.get("stargazers_count", 0)
            desc = r.get("description") or ""
            parts.append(f"  {r['name']} ({lang}, {stars}★): {desc}")

    try:
        req = urllib.request.Request(
            f"{API}/repos/{username}/{username}/readme",
            headers={**headers, "Accept": "application/vnd.github.v3.raw"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            readme_text = resp.read().decode("utf-8", errors="replace")[:3000]
        parts.append(f"\n--- Profile README ---\n{readme_text}")
    except Exception:
        pass

    print(f"  Got profile + {len(repos)} repos")
    result = "\n\n".join(parts)[:30000]
    cache_file.write_text(result)
    return result


def _lastfm_detect(url: str) -> bool:
    parsed = urlparse(url)
    return "last.fm" in parsed.netloc and "/user/" in parsed.path


def _lastfm_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    username = parsed.path.split("/user/")[-1].strip("/")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"lastfm:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Last.fm data for {username}")
        return cache_file.read_text()

    print(f"  Fetching Last.fm {username}...")
    parts = [f"Last.fm: {username}"]

    def _scrape(path: str) -> str:
        try:
            req = urllib.request.Request(
                f"https://www.last.fm/user/{username}{path}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    html = _scrape("/library/artists?date_preset=ALL")
    artists = re.findall(r'class="link-block-target"[^>]*>([^<]+)</a>', html)
    artists = [a.strip() for a in artists if a.strip() and len(a.strip()) > 1][:30]
    if artists:
        parts.append(f"\n--- Top artists (all time, {len(artists)}) ---")
        parts.extend(f"  {a}" for a in artists)

    html = _scrape("/library")
    tracks = re.findall(
        r'class="chartlist-name"[^>]*>.*?<a[^>]*>([^<]+)</a>',
        html, re.DOTALL,
    )
    tracks = [t.strip() for t in tracks if t.strip()][:20]
    if tracks:
        parts.append(f"\n--- Recent tracks ---")
        parts.extend(f"  {t}" for t in tracks)

    print(f"  Got {len(artists)} top artists, {len(tracks)} recent tracks")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _letterboxd_detect(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc != "letterboxd.com":
        return False
    path = parsed.path.strip("/")
    return bool(path) and "/" not in path


def _letterboxd_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    username = parsed.path.strip("/")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"letterboxd:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Letterboxd data for {username}")
        return cache_file.read_text()

    print(f"  Fetching Letterboxd @{username}...")

    rss_url = f"https://letterboxd.com/{username}/rss/"
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Failed: {e}")
        return ""

    parts = [f"Letterboxd: {username}"]

    titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>", xml)
    descriptions = re.findall(r"<description><!\[CDATA\[(.+?)\]\]></description>", xml, re.DOTALL)

    entries = []
    for i, title in enumerate(titles[1:], start=1):
        desc = ""
        if i < len(descriptions):
            desc = re.sub(r'<[^>]+>', '', descriptions[i]).strip()[:200]
        entries.append(f"  {title}: {desc}" if desc else f"  {title}")

    print(f"  Got {len(entries)} entries")
    parts.append(f"\n--- Recent activity ({len(entries)}) ---")
    parts.extend(entries)

    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _bandcamp_detect(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.endswith(".bandcamp.com")


def _bandcamp_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    username = parsed.netloc.replace(".bandcamp.com", "")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"bandcamp:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Bandcamp data for {username}")
        return cache_file.read_text()

    print(f"  Fetching Bandcamp {username}...")
    parts = [f"Bandcamp: {username}"]

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Failed: {e}")
        return ""

    titles = re.findall(r'<p class="title">\s*(.+?)\s*</p>', html)
    if titles:
        parts.append("\n--- Releases ---")
        for t in titles[:30]:
            parts.append(f"  {t.strip()}")

    try:
        coll_url = f"https://bandcamp.com/{username}"
        req = urllib.request.Request(coll_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            coll_html = resp.read().decode("utf-8", errors="replace")
        coll_items = re.findall(
            r'class="collection-item-title">(.+?)</div>', coll_html
        )
        if coll_items:
            parts.append(f"\n--- Collection ({len(coll_items)} items) ---")
            for item in coll_items[:50]:
                parts.append(f"  {item.strip()}")
    except Exception:
        pass

    print(f"  Got {len(titles)} releases")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _goodreads_detect(url: str) -> bool:
    parsed = urlparse(url)
    return "goodreads.com" in parsed.netloc and "/user/" in parsed.path


def _goodreads_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    user_id = ""
    for part in path.split("/"):
        if part and part[0].isdigit():
            user_id = part.split("-")[0]
            break

    if not user_id:
        print("  Could not extract Goodreads user ID from URL")
        return ""

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"goodreads:{user_id}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Goodreads data")
        return cache_file.read_text()

    print(f"  Fetching Goodreads user {user_id}...")
    parts = [f"Goodreads user: {user_id}"]

    rss_url = f"https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  RSS fetch failed: {e}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text)[:10000]
            result = f"Goodreads profile (scraped):\n{text}"
            cache_file.write_text(result)
            return result
        except Exception:
            return ""

    titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>", xml)
    authors = re.findall(r"<author_name>(.+?)</author_name>", xml)
    ratings = re.findall(r"<user_rating>(\d+)</user_rating>", xml)

    books = []
    for i, title in enumerate(titles[1:]):
        author = authors[i] if i < len(authors) else "?"
        rating = f" ({ratings[i]}★)" if i < len(ratings) and ratings[i] != "0" else ""
        books.append(f"  {title} by {author}{rating}")

    if books:
        parts.append(f"\n--- Books read ({len(books)}) ---")
        parts.extend(books)

    print(f"  Got {len(books)} books")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _steam_detect(url: str) -> bool:
    parsed = urlparse(url)
    return "steamcommunity.com" in parsed.netloc and (
        "/id/" in parsed.path or "/profiles/" in parsed.path
    )


def _steam_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"steam:{url}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Steam data")
        return cache_file.read_text()

    print(f"  Fetching Steam profile...")
    parts = [f"Steam: {url}"]

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Failed: {e}")
        return ""

    name_match = re.search(r'class="actual_persona_name">(.+?)</span>', html)
    if name_match:
        parts.append(f"Name: {name_match.group(1).strip()}")

    summary_match = re.search(
        r'class="profile_summary"[^>]*>(.+?)</div>', html, re.DOTALL
    )
    if summary_match:
        summary = re.sub(r'<[^>]+>', '', summary_match.group(1)).strip()
        if summary:
            parts.append(f"Summary: {summary}")

    games_url = url.rstrip("/") + "/games/?tab=all"
    try:
        req = urllib.request.Request(games_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            games_html = resp.read().decode("utf-8", errors="replace")

        games_match = re.search(r'var rgGames = (\[.+?\]);', games_html, re.DOTALL)
        if games_match:
            games = json.loads(games_match.group(1))
            games.sort(key=lambda g: g.get("hours_forever", "0").replace(",", ""),
                       reverse=True)
            parts.append(f"\n--- Games ({len(games)} total, sorted by playtime) ---")
            for g in games[:40]:
                name = g.get("name", "?")
                hours = g.get("hours_forever", "0")
                parts.append(f"  {name}: {hours}h")
    except Exception:
        game_names = re.findall(r'class="game_name"[^>]*>(.+?)</a>', html)
        if game_names:
            parts.append(f"\n--- Games ({len(game_names)}) ---")
            parts.extend(f"  {g.strip()}" for g in game_names[:40])

    print(f"  Got Steam profile + games")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _devto_detect(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc != "dev.to":
        return False
    path = parsed.path.strip("/")
    return bool(path) and "/" not in path


def _devto_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    API = "https://dev.to/api"
    parsed = urlparse(url)
    username = parsed.path.strip("/")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"devto:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached dev.to data for {username}")
        return cache_file.read_text()

    print(f"  Fetching dev.to @{username}...")
    parts = [f"dev.to: {username}"]

    try:
        req = urllib.request.Request(
            f"{API}/users/by_username?url={username}",
            headers={"User-Agent": "sudomake-friends/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            user = json.loads(resp.read().decode())
        if user.get("name"):
            parts.append(f"Name: {user['name']}")
        if user.get("summary"):
            parts.append(f"Bio: {user['summary']}")
        if user.get("location"):
            parts.append(f"Location: {user['location']}")
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            f"{API}/articles?username={username}&per_page=30",
            headers={"User-Agent": "sudomake-friends/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            articles = json.loads(resp.read().decode())

        if articles:
            parts.append(f"\n--- Articles ({len(articles)}) ---")
            for a in articles:
                tags = ", ".join(a.get("tag_list", []))
                parts.append(f"  {a['title']} [{tags}]")
                if a.get("description"):
                    parts.append(f"    {a['description'][:200]}")
    except Exception:
        pass

    print(f"  Got profile + articles")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _tumblr_detect(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.endswith(".tumblr.com")


def _tumblr_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    username = parsed.netloc.replace(".tumblr.com", "")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"tumblr:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Tumblr data for {username}")
        return cache_file.read_text()

    print(f"  Fetching Tumblr @{username}...")
    parts = [f"Tumblr: {username}"]

    rss_url = f"https://{username}.tumblr.com/rss"
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  RSS failed: {e}")
        return ""

    titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>", xml)
    descriptions = re.findall(
        r"<description><!\[CDATA\[(.+?)\]\]></description>", xml, re.DOTALL
    )

    posts = []
    for i in range(min(len(titles) - 1, len(descriptions) - 1)):
        title = titles[i + 1].strip()
        desc = re.sub(r'<[^>]+>', '', descriptions[i + 1]).strip()[:300]
        if title:
            posts.append(f"  {title}: {desc}" if desc else f"  {title}")
        elif desc:
            posts.append(f"  {desc}")

    if posts:
        parts.append(f"\n--- Recent posts ({len(posts)}) ---")
        parts.extend(posts)

    print(f"  Got {len(posts)} posts")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _discogs_detect(url: str) -> bool:
    parsed = urlparse(url)
    return "discogs.com" in parsed.netloc and "/user/" in parsed.path


def _discogs_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    username = parsed.path.split("/user/")[-1].strip("/")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"discogs:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Discogs data for {username}")
        return cache_file.read_text()

    print(f"  Fetching Discogs @{username}...")
    parts = [f"Discogs: {username}"]
    headers = {"User-Agent": "sudomake-friends/1.0"}

    try:
        api_url = (
            f"https://api.discogs.com/users/{username}/collection/folders/0/releases"
            f"?per_page=100&sort=added&sort_order=desc"
        )
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        releases = data.get("releases", [])
        if releases:
            parts.append(f"\n--- Collection ({data.get('pagination', {}).get('items', len(releases))} total, showing {len(releases)}) ---")
            for r in releases:
                info = r.get("basic_information", {})
                artist = ", ".join(a.get("name", "?") for a in info.get("artists", []))
                title = info.get("title", "?")
                year = info.get("year", "")
                genres = ", ".join(info.get("genres", []))
                parts.append(f"  {artist} — {title} ({year}) [{genres}]")
    except Exception as e:
        print(f"  Collection fetch failed: {e}")

    try:
        api_url = f"https://api.discogs.com/users/{username}/wants?per_page=50"
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        wants = data.get("wants", [])
        if wants:
            parts.append(f"\n--- Wantlist ({len(wants)}) ---")
            for w in wants:
                info = w.get("basic_information", {})
                artist = ", ".join(a.get("name", "?") for a in info.get("artists", []))
                title = info.get("title", "?")
                parts.append(f"  {artist} — {title}")
    except Exception:
        pass

    print(f"  Got collection + wantlist")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _pixelfed_detect(url: str) -> bool:
    import urllib.request
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    if not path:
        return False

    known = ["github.com", "bsky.app", "dev.to", "last.fm", "letterboxd.com",
             "goodreads.com", "steamcommunity.com", "bandcamp.com",
             "tumblr.com", "discogs.com", "news.ycombinator.com"]
    if any(k in parsed.netloc for k in known):
        return False

    if path.startswith("@") and "/" not in path:
        return False  # let mastodon handle it

    if path.startswith("u/") and "/" not in path[2:]:
        try:
            check = f"https://{parsed.netloc}/api/v3/site"
            req = urllib.request.Request(check, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    if "/" not in path and not path.startswith("u/"):
        try:
            check = f"https://{parsed.netloc}/api/v1/instance"
            req = urllib.request.Request(check, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                return "pixelfed" in data.get("version", "").lower()
        except Exception:
            return False

    return False


def _pixelfed_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    instance = parsed.netloc
    path = parsed.path.strip("/")

    if path.startswith("u/"):
        username = path[2:]
        platform = "Lemmy"
    else:
        username = path.lstrip("@")
        platform = "Pixelfed"

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"activitypub:{instance}/{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached {platform} data for {username}@{instance}")
        return cache_file.read_text()

    print(f"  Fetching {platform} @{username}@{instance}...")
    parts = [f"{platform}: @{username}@{instance}"]

    if platform == "Lemmy":
        try:
            api_url = f"https://{instance}/api/v3/user?username={username}&limit=50"
            req = urllib.request.Request(api_url, headers={"User-Agent": "sudomake-friends/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            person = data.get("person_view", {}).get("person", {})
            if person.get("bio"):
                parts.append(f"Bio: {person['bio']}")

            lposts = data.get("posts", [])
            if lposts:
                parts.append(f"\n--- Posts ({len(lposts)}) ---")
                for p in lposts:
                    post = p.get("post", {})
                    parts.append(f"  {post.get('name', '?')}")

            comments = data.get("comments", [])
            if comments:
                parts.append(f"\n--- Comments ({len(comments)}) ---")
                for c in comments[:30]:
                    text = c.get("comment", {}).get("content", "")[:200]
                    if text:
                        parts.append(f"  {text}")
        except Exception as e:
            print(f"  Lemmy fetch failed: {e}")
    else:
        try:
            lookup_url = f"https://{instance}/api/v1/accounts/lookup?acct={username}"
            with urllib.request.urlopen(lookup_url, timeout=10) as resp:
                account = json.loads(resp.read().decode())

            if account.get("display_name"):
                parts.append(f"Name: {account['display_name']}")
            bio = re.sub(r'<[^>]+>', '', account.get("note", ""))
            if bio:
                parts.append(f"Bio: {bio}")

            account_id = account["id"]
            api_url = f"https://{instance}/api/v1/accounts/{account_id}/statuses?limit=40"
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                statuses = json.loads(resp.read().decode())

            posts_text = []
            for s in statuses:
                text = re.sub(r'<[^>]+>', '', s.get("content", "")).strip()
                if text:
                    posts_text.append(text)
            if posts_text:
                parts.append(f"\n--- Posts ({len(posts_text)}) ---")
                parts.extend(posts_text)
        except Exception as e:
            print(f"  Pixelfed fetch failed: {e}")

    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _wikipedia_detect(url: str) -> bool:
    parsed = urlparse(url)
    return "wikipedia.org" in parsed.netloc and "User:" in parsed.path


def _wikipedia_fetch(url: str, cache_dir: Path) -> str:
    import urllib.request
    parsed = urlparse(url)
    path = unquote(parsed.path)
    username = path.split("User:")[-1].strip("/")
    lang = parsed.netloc.split(".")[0]

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"wikipedia:{lang}:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached Wikipedia data for {username}")
        return cache_file.read_text()

    print(f"  Fetching Wikipedia User:{username}...")
    parts = [f"Wikipedia user: {username} ({lang}.wikipedia.org)"]

    api_url = (
        f"https://{lang}.wikipedia.org/w/api.php"
        f"?action=query&titles=User:{username}&prop=revisions"
        f"&rvprop=content&rvslots=main&format=json"
    )
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "sudomake-friends/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        pages = data.get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            if page_id == "-1":
                print("  User page doesn't exist")
                break
            revisions = page.get("revisions", [])
            if revisions:
                content = revisions[0].get("slots", {}).get("main", {}).get("*", "")
                if content:
                    content = re.sub(r'\{\{[^}]+\}\}', '', content)
                    content = re.sub(r'\[\[([^|\]]+\|)?([^\]]+)\]\]', r'\2', content)
                    content = re.sub(r"'{2,}", '', content)
                    parts.append(f"\n--- User page ---\n{content[:5000]}")
    except Exception as e:
        print(f"  Failed: {e}")

    try:
        contrib_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=query&list=usercontribs&ucuser={username}"
            f"&uclimit=50&ucprop=title|comment&format=json"
        )
        req = urllib.request.Request(contrib_url, headers={"User-Agent": "sudomake-friends/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        contribs = data.get("query", {}).get("usercontribs", [])
        if contribs:
            articles = list(dict.fromkeys(c["title"] for c in contribs))
            parts.append(f"\n--- Recent edits ({len(articles)} articles) ---")
            parts.extend(f"  {a}" for a in articles)
    except Exception:
        pass

    print(f"  Got user page + contributions")
    result = "\n\n".join(parts)[:15000]
    cache_file.write_text(result)
    return result


def _hackernews_detect(url: str) -> bool:
    parsed = urlparse(url)
    return "ycombinator.com" in parsed.netloc and "user" in parsed.path


def _hackernews_fetch(url: str, cache_dir: Path) -> str:
    parsed = urlparse(url)
    from urllib.parse import parse_qs
    qs = parse_qs(parsed.query)
    username = qs.get("id", [""])[0]
    if not username:
        return ""

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(f"hn:{username}".encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached HN data for {username}")
        return cache_file.read_text()

    print(f"  Fetching HN @{username}...")
    parts = [f"Hacker News: {username}"]

    try:
        req = urllib.request.Request(
            f"https://hacker-news.firebaseio.com/v0/user/{username}.json",
            headers={"User-Agent": "sudomake-friends/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            user = json.loads(resp.read().decode())
        if user.get("about"):
            about = re.sub(r'<[^>]+>', '', user["about"])
            parts.append(f"About: {about}")
        parts.append(f"Karma: {user.get('karma', 0)}")
    except Exception:
        pass

    try:
        search_url = f"https://hn.algolia.com/api/v1/search?tags=comment,author_{username}&hitsPerPage=100"
        req = urllib.request.Request(search_url, headers={"User-Agent": "sudomake-friends/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        comments = []
        for hit in data.get("hits", []):
            text = hit.get("comment_text", "").strip()
            if text:
                text = re.sub(r'<[^>]+>', '', text)[:300]
                comments.append(text)
        if comments:
            parts.append(f"\n--- Recent comments ({len(comments)}) ---")
            parts.extend(comments[:50])
    except Exception:
        pass

    try:
        search_url = f"https://hn.algolia.com/api/v1/search?tags=story,author_{username}&hitsPerPage=50"
        req = urllib.request.Request(search_url, headers={"User-Agent": "sudomake-friends/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        stories = [h.get("title", "") for h in data.get("hits", []) if h.get("title")]
        if stories:
            parts.append(f"\n--- Submissions ({len(stories)}) ---")
            parts.extend(f"  {s}" for s in stories)
    except Exception:
        pass

    result = "\n\n".join(parts)[:30000]
    cache_file.write_text(result)
    return result


# Platform registry — order matters: first match wins.
PLATFORMS = [
    {
        "name": "Bandcamp",
        "description": "Bandcamp profile (e.g. https://username.bandcamp.com)",
        "detect": _bandcamp_detect,
        "fetch": _bandcamp_fetch,
    },
    {
        "name": "Bluesky",
        "description": "Bluesky profile (e.g. https://bsky.app/profile/user.bsky.social)",
        "detect": _bluesky_detect,
        "fetch": _bluesky_fetch,
    },
    {
        "name": "dev.to",
        "description": "dev.to profile (e.g. https://dev.to/username)",
        "detect": _devto_detect,
        "fetch": _devto_fetch,
    },
    {
        "name": "Discogs",
        "description": "Discogs profile (e.g. https://www.discogs.com/user/username)",
        "detect": _discogs_detect,
        "fetch": _discogs_fetch,
    },
    {
        "name": "GitHub",
        "description": "GitHub profile (e.g. https://github.com/username)",
        "detect": _github_detect,
        "fetch": _github_fetch,
    },
    {
        "name": "Hacker News",
        "description": "HN profile (e.g. https://news.ycombinator.com/user?id=username)",
        "detect": _hackernews_detect,
        "fetch": _hackernews_fetch,
    },
    {
        "name": "Goodreads",
        "description": "Goodreads profile (e.g. https://www.goodreads.com/user/show/12345-username)",
        "detect": _goodreads_detect,
        "fetch": _goodreads_fetch,
    },
    {
        "name": "Last.fm",
        "description": "Last.fm profile (e.g. https://www.last.fm/user/username)",
        "detect": _lastfm_detect,
        "fetch": _lastfm_fetch,
    },
    {
        "name": "Letterboxd",
        "description": "Letterboxd profile (e.g. https://letterboxd.com/username)",
        "detect": _letterboxd_detect,
        "fetch": _letterboxd_fetch,
    },
    {
        "name": "Mastodon",
        "description": "Mastodon profile (e.g. https://mastodon.social/@user)",
        "detect": _mastodon_detect,
        "fetch": _mastodon_fetch,
    },
    {
        "name": "Pixelfed/Lemmy",
        "description": "ActivityPub profile (e.g. https://pixelfed.social/@user, https://lemmy.world/u/user)",
        "detect": _pixelfed_detect,
        "fetch": _pixelfed_fetch,
    },
    {
        "name": "Steam",
        "description": "Steam profile (e.g. https://steamcommunity.com/id/username)",
        "detect": _steam_detect,
        "fetch": _steam_fetch,
    },
    {
        "name": "Tumblr",
        "description": "Tumblr blog (e.g. https://username.tumblr.com)",
        "detect": _tumblr_detect,
        "fetch": _tumblr_fetch,
    },
    {
        "name": "Wikipedia",
        "description": "Wikipedia user page (e.g. https://en.wikipedia.org/wiki/User:Username)",
        "detect": _wikipedia_detect,
        "fetch": _wikipedia_fetch,
    },
]


def detect_platform(url: str) -> dict | None:
    """Return the first platform plugin that matches this URL, or None."""
    for p in PLATFORMS:
        if p["detect"](url):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Paths & .env Management
# ═══════════════════════════════════════════════════════════════════════════════

def get_paths(root: Path) -> dict:
    return {
        "root": root,
        "friends": root / "friends",
        "env": root / ".env",
        "scrape_cache": root / ".scrape-cache",
    }


def load_env(env_path: Path) -> dict:
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_env(env_path: Path, env: dict):
    lines = [f"{k}={v}" for k, v in env.items()]
    env_path.write_text("\n".join(lines) + "\n")


def set_env_var(env_path: Path, key: str, value: str):
    env = load_env(env_path)
    env[key] = value
    save_env(env_path, env)
    os.environ[key] = value


# ═══════════════════════════════════════════════════════════════════════════════
# Claude API
# ═══════════════════════════════════════════════════════════════════════════════

def get_client(env_path: Path):
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY") or load_env(env_path).get("ANTHROPIC_API_KEY")
    if not key:
        return None
    os.environ["ANTHROPIC_API_KEY"] = key
    return anthropic.Anthropic(api_key=key)


def generate_candidates(client, context: str, held: list[dict],
                        existing_friends: list[str] | None = None,
                        count: int = CANDIDATE_COUNT) -> list[dict]:
    held_desc = ""
    if held:
        held_desc = f"""
These friends have already been selected in this session (do NOT regenerate them,
and make sure new candidates have good chemistry with them):
{json.dumps(held, indent=2)}
"""
    existing_desc = ""
    if existing_friends:
        existing_desc = f"""
These friends ALREADY EXIST in the group (do NOT regenerate them, generate
candidates who would fit in with this existing group):
{', '.join(existing_friends)}
"""

    prompt = f"""Based on this profile of a person, generate exactly {count} fictional friend
candidates for a virtual group chat.

START WITH PERSONALITY, NOT PROFESSION. Think about what kind of PERSON would be
a great friend — their energy, their humor, their emotional style — then figure out
what they do for a living as a secondary detail.

Return ONLY a JSON array of objects. Each object must have:
- "name": first name only, capitalized (prefer gender-neutral names or gender-ambiguous nicknames)
- "traits": array of 3-4 personality trait words (e.g. ["sarcastic", "loyal", "impulsive"] or ["gentle", "witty", "stubborn"]). These are the CORE of who this person is.
- "age": integer
- "location": city and country only (e.g. "Berlin, Germany" or "San Francisco, CA")
- "occupation": what they do (keep it brief — this is NOT the interesting part)
- "vibe": 1-3 sentences about who they are as a PERSON. Lead with personality and energy, not their job. How do they make you feel when you're around them? What's their deal? IMPORTANT: Show their traits through behavior and anecdotes, don't just list adjectives. "Will roast your music taste then make you a perfect playlist" not "sarcastic but caring".
- "why": why they'd be this person's friend — focus on personality chemistry, not shared hobbies (1 sentence)
- "timezone": IANA timezone string
- "chattiness": float 0.0-1.0

CRITICAL: A friend group needs PERSONALITY DIVERSITY, not just occupational diversity.
You need the snarky one, the sincere one, the chaotic one, the calm one, the one who
roasts everyone, the one who gives unsolicited advice, the one who sends memes at 2am.
Don't make everyone pleasant and supportive — real friend groups have friction, teasing,
and complementary energies.

Make the friends diverse in personality, location, and timezone.
Some should be local, some remote. Mix of introverts/extroverts, tech/non-tech.
{held_desc}{existing_desc}

## Profile of the person
{context}

JSON array only, no markdown fencing:"""

    response = client.messages.create(
        model=MODEL, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def generate_soul(client, candidate: dict, all_friends: list[dict],
                  user_context: str) -> str:
    others = [f for f in all_friends if f["name"] != candidate["name"]]
    others_desc = "\n".join(
        f"- {f['name']} ({', '.join(f.get('traits', []))}): {f['vibe']}"
        for f in others
    )
    traits = candidate.get("traits", [])
    traits_str = ", ".join(traits) if traits else "not specified"

    prompt = f"""Write a detailed SOUL.md personality file for a virtual chat bot character.
This character will be in a group chat with friends.

## The character
{json.dumps(candidate, indent=2)}

## Their core personality traits: {traits_str}
These traits are the FOUNDATION of this character. Everything else — their backstory,
their interests, their speech patterns — should flow from and reinforce these traits.
A "sarcastic, loyal, impulsive" person texts differently than a "gentle, witty, stubborn"
person. They have different childhoods, different relationships, different coping mechanisms.

## Other friends in the group
{others_desc}

## Another friend in the group
{user_context}

CRITICAL INSTRUCTION: This is a WHOLE PERSON, not a walking job description. Their
occupation is ONE facet of who they are. You MUST flesh out their entire life — where
they grew up, their family, what they studied, what they eat, what they watch, what
they do on a lazy Sunday. A real friend is someone you know deeply, not a LinkedIn
profile with a texting style.

The personality traits above should PERMEATE everything. If someone is "sarcastic",
their text examples should drip with sarcasm. If someone is "anxious", their backstory
should explain why. If someone is "chaotic", their interests should be all over the place.

Write the SOUL.md in this exact format:

# {{Name}}

## Identity
- **Age:** ...
- **Location:** ...
- **Hometown:** ... (where they grew up — should be different from current location)
- **Occupation:** ...
- **Traits:** {traits_str}
- **Timezone:** ...

## Backstory
(1-2 paragraphs: Where did they grow up? What was their family like — siblings,
parents' jobs, family dynamics? Where did they go to college (or not), what did
they study, what year did they graduate? How did they end up where they live now?
What's the arc of their life so far? The backstory should EXPLAIN the personality traits.)

## Personality
(2-3 paragraphs: core traits, emotional patterns, worldview, humor style.
This should go BEYOND their professional identity. Ground it in the traits: {traits_str}.
How do these traits manifest day-to-day? How do they interact with each other?
What's charming about this person and what's annoying?)

## Interests & Life
(bullet list that covers their WHOLE life, not just their job niche:
- Professional/hobby interests
- Favorite foods, cooking habits, restaurants
- Movies, TV, books, podcasts they love
- Outdoor activities, sports, fitness habits
- Travel experiences or aspirations
- Guilty pleasures, comfort activities
- What they do on a Friday night or lazy Sunday)

## Relationships
(how they relate to each of the other friends — think about
personality chemistry, not just shared interests)

## Speech Patterns
(very specific texting style: capitalization, punctuation, emoji usage, message length,
slang, verbal tics. This section is CRITICAL for making the character feel real.
The traits ({traits_str}) should be OBVIOUS from the text examples alone.
Include 5+ examples of how they'd actually text — covering different moods and topics,
NOT just about their job.)

## Boundaries
(topics they avoid, things that annoy them, conversational pet peeves — these should
also flow from their personality traits)

Be specific and vivid. Avoid generic traits. The character should feel like someone
you've known for years — you know their coffee order, that they hate cilantro, that
they call their mom every Sunday. The Speech Patterns section should make it possible
to distinguish this character's messages from any other character at a glance."""

    response = client.messages.create(
        model=MODEL, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# User Profile
# ═══════════════════════════════════════════════════════════════════════════════

def compile_profile(client, raw_context: str) -> str:
    """Distill raw scrape/text into a clean, reusable user profile."""
    prompt = f"""Based on the following raw content about a person, write a concise but
detailed profile summary (300-500 words). Cover:
- Who they are (name, location, job)
- Their personality traits — not just interests, but HOW they are as a person
  (e.g. sarcastic, earnest, self-deprecating, intense, goofy, loyal, anxious,
  competitive, nurturing, blunt, etc.)
- Their humor style (dry, absurdist, punny, dark, wholesome, etc.)
- Interests, hobbies, creative pursuits
- Social style and communication preferences
- What personality TRAITS they'd vibe with in friends (not just shared interests —
  think about complementary energies. A sarcastic person might love a sincere friend.
  An anxious person might need a calm, grounding friend.)

End the profile with a section called "Friend chemistry notes" that lists 4-6
personality trait combos that would make good friends for this person, e.g.:
- "witty + slightly chaotic — someone who matches their banter energy"
- "calm + deeply sincere — a grounding presence when things get intense"

Be specific — names, places, projects, preferences. This profile will be reused
to generate fictional friend characters, so include anything that would help
determine compatibility.

Raw content:
{raw_context[:20000]}

Write the profile directly, no preamble:"""

    response = client.messages.create(
        model=MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()



# ═══════════════════════════════════════════════════════════════════════════════
# Scraper
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_page(url: str, timeout: int = 10) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_links(html: str, base_url: str) -> list[str]:
    base_domain = urlparse(base_url).netloc
    links = []
    for match in re.finditer(r'href=["\']([^"\']+)["\']', html):
        href = match.group(1)
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if (parsed.netloc == base_domain
                and not parsed.path.endswith(('.png', '.jpg', '.gif', '.css', '.js', '.svg', '.pdf', '.zip'))
                and '#' not in full):
            links.append(full.split('#')[0])
    return list(dict.fromkeys(links))


def _strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def scrape_site(url: str, cache_dir: Path) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(url.encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached scrape for {url}")
        return cache_file.read_text()

    print(f"  Scraping {url} (up to 3 minutes)...")
    start = time.time()
    visited = set()
    to_visit = [url]
    pages = []

    while to_visit and (time.time() - start) < SCRAPE_TIMEOUT:
        current = to_visit.pop(0)
        if current in visited:
            continue
        visited.add(current)

        html = _fetch_page(current)
        if not html:
            continue

        text = _strip_html(html)
        if len(text) > 100:
            pages.append(f"--- Page: {current} ---\n{text[:3000]}")
            print(f"    [{len(pages)}] {current[:60]}...")

        if len(pages) < 15:
            for link in _extract_links(html, url):
                if link not in visited:
                    to_visit.append(link)

    elapsed = time.time() - start
    print(f"  Scraped {len(pages)} pages in {elapsed:.0f}s")

    result = "\n\n".join(pages)[:30000]
    cache_file.write_text(result)
    return result


def _fetch_url(url: str, paths: dict) -> str:
    """Fetch a single URL — auto-detect platform or fall back to scrape."""
    plugin = detect_platform(url)
    if plugin:
        print(f"  Detected: {plugin['name']}")
        return plugin["fetch"](url, paths["scrape_cache"])

    # Generic website scrape
    result = scrape_site(url, paths["scrape_cache"])
    if result:
        return f"Website content from {url}:\n\n{result}"
    return ""


def _show_platform_help():
    """Show supported platforms."""
    print("\n  Supported platforms (auto-detected from URL):\n")
    for p in PLATFORMS:
        print(f"    {p['name']:16s} {p['description']}")
    print(f"\n    {'Any website':16s} Falls back to crawling + scraping")
    print()
    print("  You can provide multiple URLs to build a richer profile.")
    print("  Example: your website, Mastodon, Last.fm, and GitHub.\n")


def _extract_text_from_archive(path: Path, max_bytes: int = 30000) -> str:
    """Extract readable text files from a tar/zip archive."""
    import tarfile
    import zipfile

    text_extensions = {".txt", ".md", ".json", ".csv", ".html", ".htm",
                       ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                       ".py", ".js", ".ts", ".rst", ".log", ".mbox"}
    parts = []
    total = 0

    try:
        if tarfile.is_tarfile(str(path)):
            with tarfile.open(str(path), "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    suffix = Path(member.name).suffix.lower()
                    if suffix not in text_extensions:
                        continue
                    try:
                        f = tf.extractfile(member)
                        if f:
                            chunk = f.read(max_bytes - total).decode("utf-8", errors="replace")
                            parts.append(f"--- {member.name} ---\n{chunk}")
                            total += len(chunk)
                            if total >= max_bytes:
                                break
                    except Exception:
                        continue
        elif zipfile.is_zipfile(str(path)):
            with zipfile.ZipFile(str(path)) as zf:
                for name in zf.namelist():
                    suffix = Path(name).suffix.lower()
                    if suffix not in text_extensions:
                        continue
                    try:
                        chunk = zf.read(name)[:max_bytes - total].decode("utf-8", errors="replace")
                        parts.append(f"--- {name} ---\n{chunk}")
                        total += len(chunk)
                        if total >= max_bytes:
                            break
                    except Exception:
                        continue
    except Exception as e:
        print(f"  Error reading archive: {e}")
        return ""

    print(f"  Extracted {len(parts)} text files from archive")
    return "\n\n".join(parts)


def get_user_context(paths: dict,
                     cached_sources: list[dict] | None = None,
                     on_save_sources=None) -> tuple[str, list[dict]]:
    """Interactive loop to collect user context from multiple sources."""
    all_parts = []
    sources = []

    def _save():
        if on_save_sources:
            on_save_sources(sources)

    # Check for sources.txt in current directory
    sources_file = Path("sources.txt")
    if sources_file.exists() and not cached_sources:
        lines = [l.strip() for l in sources_file.read_text().splitlines() if l.strip()]
        if lines:
            print(f"\n  Found sources.txt with {len(lines)} source(s):")
            for l in lines:
                print(f"    - {l}")
            use_file = input("  Use these sources? [Y/n]: ").strip().lower()
            if use_file in ("", "y", "yes"):
                for line in lines:
                    print(f"\n  Processing: {line}")
                    if line.startswith("http://") or line.startswith("https://"):
                        result = _fetch_url(line, paths)
                        if result:
                            all_parts.append(result)
                            sources.append({"label": line, "content": result})
                            _save()
                            print(f"  Added.")
                        else:
                            print(f"  Got nothing from {line}. Skipping.")
                    else:
                        fpath = Path(line).expanduser()
                        if fpath.exists() and fpath.is_file():
                            try:
                                content = fpath.read_bytes().decode("utf-8", errors="replace")[:15000]
                            except Exception:
                                print(f"  Can't read {fpath.name}. Skipping.")
                                continue
                            if content:
                                all_parts.append(f"File ({fpath.name}):\n{content}")
                                sources.append({"label": fpath.name, "content": f"File ({fpath.name}):\n{content}"})
                                _save()
                                print(f"  Added {fpath.name}.")
                        else:
                            # Treat as free-text description
                            all_parts.append(f"Self-description:\n{line}")
                            sources.append({"label": "description", "content": f"Self-description:\n{line}"})
                            _save()
                            print(f"  Added as description.")
                print(f"\n  Loaded {len(sources)} source(s) from sources.txt.")

    # Offer to reuse cached sources
    if cached_sources:
        print(f"\n  Found {len(cached_sources)} source(s) from last time:")
        for s in cached_sources:
            print(f"    - {s['label']}")
        reuse = input("  Reuse these? [Y/n]: ").strip().lower()
        if reuse in ("", "y", "yes"):
            for s in cached_sources:
                all_parts.append(s["content"])
                sources.append(s)
            print(f"  Loaded {len(sources)} cached source(s).")
        else:
            print("  Starting fresh.")

    if all_parts:
        print(f"\n  You can add more sources, or q to continue with what you have.")
    else:
        print("\n  Tell me about yourself. You can provide as many sources as you want.")
        print("  The more context, the better your friends will match you.")
    print("\n  Enter a URL, file path, or type a description.")
    print("  Type ? for supported platforms, q when done.\n")

    while True:
        prompt = f"  [{len(all_parts)} source{'s' if len(all_parts) != 1 else ''}] > "
        entry = input(prompt).strip()

        if not entry:
            continue
        if entry == "q":
            if all_parts:
                break
            print("  Need at least one source. Enter a URL, file path, or description.")
            continue
        if entry == "?":
            _show_platform_help()
            continue

        # URL?
        if entry.startswith("http://") or entry.startswith("https://"):
            result = _fetch_url(entry, paths)
            if result:
                all_parts.append(result)
                sources.append({"label": entry, "content": result})
                _save()
                print(f"  Added. Enter another, or q to finish.")
            else:
                print("  Got nothing from that URL. Try another?")
            continue

        # File path?
        fpath = Path(entry).expanduser()
        if fpath.exists() and fpath.is_file():
            if fpath.suffix in (".tgz", ".gz", ".tar", ".zip"):
                content = _extract_text_from_archive(fpath)
            else:
                try:
                    content = fpath.read_bytes().decode("utf-8", errors="replace")[:15000]
                except Exception:
                    print(f"  Can't read {fpath.name}. Skipping.")
                    continue
            if content:
                all_parts.append(f"File ({fpath.name}):\n{content}")
                sources.append({"label": fpath.name, "content": f"File ({fpath.name}):\n{content}"})
                _save()
                print(f"  Added {fpath.name}. Enter another, or q to finish.")
            else:
                print(f"  No readable text found in {fpath.name}.")
            continue

        # Check if it looks like a path that doesn't exist
        if "/" in entry or entry.startswith("~") or entry.startswith("."):
            print(f"  File not found: {entry}")
            continue

        # Treat as free-text description
        all_parts.append(f"Self-description:\n{entry}")
        sources.append({"label": "description", "content": f"Self-description:\n{entry}"})
        _save()
        print(f"  Added. Enter another, or q to finish.")

    # Offer to save sources for next time
    source_labels = [s["label"] for s in sources if s["label"] != "description"]
    if source_labels and not sources_file.exists():
        save = input("\n  Save source list to ./sources.txt for next time? [Y/n]: ").strip().lower()
        if save in ("", "y", "yes"):
            sources_file.write_text("\n".join(source_labels) + "\n")
            print(f"  Saved {len(source_labels)} source(s) to sources.txt")

    return "\n\n---\n\n".join(all_parts), sources


# ═══════════════════════════════════════════════════════════════════════════════
# Selection TUI
# ═══════════════════════════════════════════════════════════════════════════════

def _show_detail_modal(stdscr, candidate: dict):
    """Show a centered modal with full candidate details. ESC/q to dismiss."""
    while True:
        height, width = stdscr.getmaxyx()

        pad_x, pad_y = 4, 2
        modal_w = min(width - pad_x * 2, 80)
        modal_h = height - pad_y * 2
        start_x = (width - modal_w) // 2
        start_y = pad_y

        wrap_w = modal_w - 4

        def _wrap(text: str, indent: str = "  ") -> list[str]:
            wrapped = []
            words = text.split()
            current = indent
            for word in words:
                if len(current) + len(word) + 1 > wrap_w:
                    wrapped.append(current)
                    current = indent + word
                else:
                    current += (" " if current.strip() else "") + word
            if current.strip():
                wrapped.append(current)
            return wrapped

        lines = []
        lines.append(f"  {candidate['name']}, {candidate['age']}")
        lines.extend(_wrap(candidate['location']))
        lines.extend(_wrap(candidate['occupation']))
        lines.append("")
        lines.extend(_wrap(candidate.get('vibe', '')))
        lines.append("")
        lines.extend(_wrap(f"Why: {candidate.get('why', '')}"))
        lines.append("")
        lines.append(f"  Timezone: {candidate.get('timezone', '?')}")

        stdscr.clear()

        for y in range(start_y, start_y + modal_h):
            if y >= height:
                break
            stdscr.addstr(y, start_x, "|", curses.A_DIM)
            stdscr.addstr(y, start_x + modal_w - 1, "|", curses.A_DIM)
        top_border = "+" + "-" * (modal_w - 2) + "+"
        bot_border = "+" + "-" * (modal_w - 2) + "+"
        stdscr.addstr(start_y, start_x, top_border[:width - start_x], curses.A_DIM)
        if start_y + modal_h - 1 < height:
            stdscr.addstr(start_y + modal_h - 1, start_x, bot_border[:width - start_x], curses.A_DIM)

        title = f" {candidate['name']} "
        stdscr.addstr(start_y, start_x + (modal_w - len(title)) // 2,
                       title, curses.A_BOLD | curses.A_REVERSE)

        for i, line in enumerate(lines):
            row = start_y + 2 + i
            if row >= start_y + modal_h - 2:
                break
            text = line[:modal_w - 4]
            stdscr.addstr(row, start_x + 1, text)

        dismiss = " any key to close "
        if start_y + modal_h - 1 < height:
            stdscr.addstr(start_y + modal_h - 1,
                           start_x + (modal_w - len(dismiss)) // 2,
                           dismiss, curses.A_DIM)

        stdscr.refresh()
        stdscr.getch()
        return


def selection_ui(stdscr, candidates: list[dict],
                 held_indices: set[int]) -> tuple[set[int], str]:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_WHITE, -1)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)

    cursor = 0
    scroll_offset = 0

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        visible_rows = height - 5

        n_held = len(held_indices)
        header = f" Friend Selection ({n_held} invited) "
        stdscr.addstr(0, 0, header, curses.A_BOLD | curses.A_REVERSE)

        col_mark = 7
        col_name = 12
        col_loc = 20
        col_vibe = max(10, width - col_mark - col_name - col_loc - 4)

        hdr_line = f" {'':6s} {'Name':<{col_name}s} {'Location':<{col_loc}s} {'Vibe'}"
        stdscr.addstr(1, 0, hdr_line[:width - 1], curses.A_DIM)
        stdscr.addstr(2, 0, "-" * min(width - 1, col_mark + col_name + col_loc + col_vibe + 4))

        if cursor < scroll_offset:
            scroll_offset = cursor
        if cursor >= scroll_offset + visible_rows:
            scroll_offset = cursor - visible_rows + 1

        for i in range(visible_rows):
            idx = scroll_offset + i
            if idx >= len(candidates):
                break
            c = candidates[idx]
            is_held = idx in held_indices
            is_cursor = idx == cursor
            row = i + 3

            marker = " INV. " if is_held else "      "
            name = c["name"][:col_name]
            loc = c["location"][:col_loc]
            vibe = c["vibe"][:col_vibe]
            line = f" {marker} {name:<{col_name}s} {loc:<{col_loc}s} {vibe}"
            line = line[:width - 1]

            if is_cursor and is_held:
                attr = curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE
            elif is_cursor:
                attr = curses.color_pair(2) | curses.A_REVERSE
            elif is_held:
                attr = curses.color_pair(1) | curses.A_BOLD
            else:
                attr = curses.color_pair(3)
            stdscr.addstr(row, 0, line, attr)

        footer_row = height - 1
        footer = " ENTER=invite  ESC/s=save+exit  x=expand  e=edit  r=re-roll  q=accept+continue "
        stdscr.addstr(footer_row, 0, footer[:width - 1], curses.color_pair(4))

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(candidates) - 1, cursor + 1)
        elif key in (ord("\n"), ord(" ")):
            if cursor in held_indices:
                held_indices.discard(cursor)
            else:
                held_indices.add(cursor)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            held_indices.discard(cursor)
        elif key == ord("x") and cursor < len(candidates):
            _show_detail_modal(stdscr, candidates[cursor])
        elif key == ord("e") and cursor < len(candidates):
            return held_indices, ("edit_candidate", cursor)
        elif key == ord("r"):
            return held_indices, "reroll"
        elif key == ord("q"):
            if n_held > 0:
                # Confirm accept
                confirm = " Accept selected friends and continue? [y/n] "
                stdscr.addstr(footer_row, 0, " " * (width - 1))
                stdscr.addstr(footer_row, 0, confirm[:width - 1],
                              curses.color_pair(4) | curses.A_BOLD)
                stdscr.refresh()
                if stdscr.getch() == ord("y"):
                    return held_indices, "accept"
            else:
                warn = " You want at least one friend right? Press ENTER to invite each friend. "
                stdscr.addstr(footer_row, 0, " " * (width - 1))
                stdscr.addstr(footer_row, 0, warn[:width - 1],
                              curses.color_pair(4) | curses.A_BOLD)
                stdscr.refresh()
                stdscr.getch()
        elif key in (ord("s"), 27):  # s or ESC
            # Confirm save+exit
            confirm = " Save selections and exit? [y/n] "
            stdscr.addstr(footer_row, 0, " " * (width - 1))
            stdscr.addstr(footer_row, 0, confirm[:width - 1],
                          curses.color_pair(4) | curses.A_BOLD)
            stdscr.refresh()
            if stdscr.getch() == ord("y"):
                return held_indices, "quit"


# ═══════════════════════════════════════════════════════════════════════════════
# Editor
# ═══════════════════════════════════════════════════════════════════════════════

def check_editor() -> str | None:
    """Check if an editor is available. Returns the editor command or None."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor:
        return editor
    for candidate in ["nano", "vim", "vi"]:
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return candidate
    return None


def edit_with_editor(text: str, label: str = "") -> str:
    suffix = f"-{label}.md" if label else ".md"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(text)
        f.flush()
        tmppath = f.name

    editor = check_editor()
    if editor:
        subprocess.call([editor, tmppath])
    else:
        print(f"\n  No $EDITOR set. Edit this file manually:")
        print(f"    {tmppath}")
        input("  Press ENTER when done editing...")

    with open(tmppath) as f:
        result = f.read()
    os.unlink(tmppath)
    return result


def candidate_to_text(c: dict) -> str:
    lines = [
        f"Name: {c['name']}",
        f"Age: {c['age']}",
        f"Location: {c['location']}",
        f"Occupation: {c['occupation']}",
        "",
        f"Vibe: {c.get('vibe', '')}",
        "",
        f"Why: {c.get('why', '')}",
        f"Timezone: {c.get('timezone', '')}",
    ]
    return "\n".join(lines)


def text_to_candidate(text: str, original: dict) -> dict:
    c = dict(original)
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "name":
            c["name"] = value
        elif key == "age":
            try:
                c["age"] = int(value)
            except ValueError:
                pass
        elif key == "location":
            c["location"] = value
        elif key == "occupation":
            c["occupation"] = value
        elif key == "vibe":
            c["vibe"] = value
        elif key == "why":
            c["why"] = value
        elif key == "timezone":
            c["timezone"] = value
    # Validate timezone after all fields are parsed (location may help)
    c["timezone"] = _validate_timezone(c.get("timezone", ""), c.get("location", ""))
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# Selection Loop
# ═══════════════════════════════════════════════════════════════════════════════

def run_selection_loop(
    client,
    user_context: str,
    candidates: list[dict] | None = None,
    held_indices: set[int] | None = None,
    existing_friends: list[str] | None = None,
    on_save=None,
) -> list[dict] | None:
    """Shared TUI selection loop. Returns selected candidates or None if quit."""
    if candidates is None:
        print("\n  Generating candidates...")
        candidates = generate_candidates(client, user_context, [],
                                          existing_friends=existing_friends)
        if on_save:
            on_save(candidates, set())

    if held_indices is None:
        held_indices = set()

    while True:
        held_indices, action = curses.wrapper(
            selection_ui, candidates, held_indices
        )

        if on_save:
            on_save(candidates, held_indices)

        if isinstance(action, tuple) and action[0] == "edit_candidate":
            idx = action[1]
            text = candidate_to_text(candidates[idx])
            edited = edit_with_editor(text, label=candidates[idx]["name"].lower())
            candidates[idx] = text_to_candidate(edited, candidates[idx])
            if on_save:
                on_save(candidates, held_indices)
            continue

        if action == "quit":
            return None

        elif action == "reroll":
            held = [candidates[i] for i in sorted(held_indices)]
            n_new = CANDIDATE_COUNT - len(held)
            print(f"\n  Re-rolling {n_new} candidates (keeping {len(held)} invited)...")
            # Request extra to handle LLM returning fewer than asked
            new_candidates = generate_candidates(
                client, user_context, held,
                existing_friends=existing_friends, count=n_new,
            )
            # Held first, then new ones, cap at CANDIDATE_COUNT
            candidates = held + new_candidates
            candidates = candidates[:CANDIDATE_COUNT]
            new_held_indices = set(range(len(held)))
            held_indices = new_held_indices
            if on_save:
                on_save(candidates, held_indices)

        elif action == "accept":
            selected = [candidates[i] for i in sorted(held_indices)]
            return selected


# ═══════════════════════════════════════════════════════════════════════════════
# Soul Generation & Friend Directories
# ═══════════════════════════════════════════════════════════════════════════════

def generate_souls_for_selected(
    client,
    selected: list[dict],
    user_context: str,
    friends_dir: Path,
    cached_souls: dict | None = None,
    on_save_soul=None,
) -> dict:
    """Generate SOUL.md for each selected candidate. Returns {name: soul_text}."""
    souls = dict(cached_souls or {})

    print("\n  Generating detailed personalities...")
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        existing_soul = friends_dir / slug / "SOUL.md"
        if existing_soul.exists():
            souls[c["name"]] = existing_soul.read_text()
            print(f"    {c['name']}'s soul exists on disk (keeping)")
        elif c["name"] in souls:
            print(f"    {c['name']}'s soul already generated")
        else:
            print(f"    Writing {c['name']}'s soul...", end="", flush=True)
            souls[c["name"]] = generate_soul(client, c, selected, user_context)
            print(" done")
        if on_save_soul:
            on_save_soul(c["name"], souls[c["name"]])

    return souls


def _validate_timezone(tz_str: str, location: str = "") -> str:
    """Validate a timezone string. Try to fix it, fall back to America/New_York."""
    from zoneinfo import ZoneInfo, available_timezones

    # Basic cleanup
    tz_str = tz_str.strip().replace(" ", "_")

    # Try it directly
    try:
        ZoneInfo(tz_str)
        return tz_str
    except (KeyError, Exception):
        pass

    # Try common fixes
    for prefix in ["America/", "Europe/", "Asia/", "Australia/", "Pacific/", "Africa/"]:
        candidate = prefix + tz_str.split("/")[-1]
        try:
            ZoneInfo(candidate)
            return candidate
        except (KeyError, Exception):
            continue

    # Try to guess from location
    if location:
        loc_lower = location.lower()
        # Map common cities/regions to timezones
        city_map = {
            "new york": "America/New_York", "nyc": "America/New_York",
            "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
            "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
            "chicago": "America/Chicago", "denver": "America/Denver",
            "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles",
            "boston": "America/New_York", "philadelphia": "America/New_York",
            "austin": "America/Chicago", "houston": "America/Chicago",
            "atlanta": "America/New_York", "miami": "America/New_York",
            "london": "Europe/London", "berlin": "Europe/Berlin",
            "paris": "Europe/Paris", "amsterdam": "Europe/Amsterdam",
            "tokyo": "Asia/Tokyo", "sydney": "Australia/Sydney",
            "toronto": "America/Toronto", "vancouver": "America/Vancouver",
            "mexico city": "America/Mexico_City",
        }
        for city, tz in city_map.items():
            if city in loc_lower:
                return tz

    return "America/New_York"


def create_friend_dir(friends_dir: Path, name: str, soul: str,
                      candidate: dict) -> str:
    slug = name.lower().replace(" ", "_")
    friend_dir = friends_dir / slug
    friend_dir.mkdir(parents=True, exist_ok=True)

    (friend_dir / "SOUL.md").write_text(soul)
    (friend_dir / "MEMORY.md").write_text("# Memory\n")

    config = {
        "timezone": _validate_timezone(
            candidate.get("timezone", "America/New_York"),
            candidate.get("location", ""),
        ),
        "schedule": {
            "wake_up": "08:00",
            "sleep_at": "23:00",
            "work_start": "09:00",
            "work_end": "17:00",
            "days_off": [5, 6],
        },
        "chattiness": candidate.get("chattiness", 0.5),
        "bot_reply_chance": 0.3,
    }
    with open(friend_dir / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    return slug


def get_existing_friend_names(friends_dir: Path) -> list[str]:
    """Return names of friends that already have SOUL.md files."""
    names = []
    if friends_dir.exists():
        for d in sorted(friends_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "SOUL.md").exists():
                names.append(d.name)
    return names


# ═══════════════════════════════════════════════════════════════════════════════
# Bot Token Collection
# ═══════════════════════════════════════════════════════════════════════════════

def _set_bot_display_name(token: str, name: str) -> bool:
    """Set a bot's display name via the Telegram API."""
    import urllib.request
    url = f"https://api.telegram.org/bot{token}/setMyName?name={quote(name)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("ok", False)
    except Exception:
        return False


def collect_bot_token(env_path: Path, friend_name: str) -> str | None:
    """Collect a single bot token interactively. Returns token or None if quit."""
    slug = friend_name.lower().replace(" ", "_")
    env_key = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"

    existing = os.environ.get(env_key) or load_env(env_path).get(env_key)
    if existing:
        print(f"  Token for {friend_name} already configured.")
        return existing

    import random as _rng
    prefix = "".join(_rng.choices("abcdefghijklmnopqrstuvwxyz", k=3))
    print(f"\n  Bot for {friend_name}:")
    print(f"    @BotFather > /newbot > any username > e.g. {prefix}_{slug}_bot")

    while True:
        token = input(f"    Paste token (or 'q' to quit): ").strip()
        if token.lower() == "q":
            return None
        if token == "mocktg" or (":" in token and len(token) > 20):
            set_env_var(env_path, env_key, token)
            print(f"    Saved")

            # Set display name
            def _prefill():
                readline.insert_text(friend_name)
                readline.redisplay()
            readline.set_pre_input_hook(_prefill)
            display = input(f"    Display name: ").strip()
            readline.set_pre_input_hook(None)
            if not display:
                display = friend_name
            if token == "mocktg":
                print(f"    (mock token, skipping API call)")
            elif _set_bot_display_name(token, display):
                print(f"    Set display name to '{display}'")
            else:
                print(f"    Couldn't set name automatically. Use /setname in BotFather.")

            return token
        print("    Doesn't look like a bot token. Try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint
# ═══════════════════════════════════════════════════════════════════════════════

CHECKPOINT_PATH = None  # set after HOME_DIR is known


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH and CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text())
    return {"step": "start"}


def save_checkpoint(data: dict):
    if CHECKPOINT_PATH:
        CHECKPOINT_PATH.write_text(json.dumps(data, indent=2))


def clear_checkpoint():
    if CHECKPOINT_PATH and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()


# ═══════════════════════════════════════════════════════════════════════════════
# Wizard Steps
# ═══════════════════════════════════════════════════════════════════════════════

def step_anthropic_key(cp, paths):
    existing = os.environ.get("ANTHROPIC_API_KEY") or load_env(paths["env"]).get("ANTHROPIC_API_KEY")
    if existing:
        print(f"\n  Anthropic API key found (sk-ant-...{existing[-6:]})")
        use = input("  Use this key? [Y/n]: ").strip().lower()
        if use in ("", "y", "yes"):
            os.environ["ANTHROPIC_API_KEY"] = existing
            cp["step"] = "user_profile"
            save_checkpoint(cp)
            return cp

    print()
    print("  +-------------------------------------------+")
    print("  |  Anthropic API Key                        |")
    print("  +-------------------------------------------+")
    print("  |  1. Go to console.anthropic.com           |")
    print("  |  2. Sign in or create an account          |")
    print("  |  3. Go to Settings > API Keys             |")
    print("  |  4. Click 'Create Key'                    |")
    print("  |  5. Copy the key (starts with sk-ant-)    |")
    print("  +-------------------------------------------+")
    print()

    while True:
        key = input("  Paste your API key (or 'q' to quit): ").strip()
        if key.lower() == "q":
            save_checkpoint(cp)
            print("\n  Progress saved. Run again to resume.")
            sys.exit(0)
        if key.startswith("sk-ant-"):
            set_env_var(paths["env"], "ANTHROPIC_API_KEY", key)
            print("  Saved to .env")
            cp["step"] = "user_profile"
            save_checkpoint(cp)
            return cp
        print("  Doesn't look right (should start with sk-ant-). Try again.")


def step_user_profile(cp, paths):
    """Collect or load the user profile."""
    if cp.get("user_context"):
        print(f"\n  Profile already compiled ({len(cp['user_context'])} chars).")
        redo = input("  Create a new profile? [y/N]: ").strip().lower()
        if redo != "y":
            cp["step"] = "select_friends"
            save_checkpoint(cp)
            return cp

    client = get_client(paths["env"])
    if not client:
        cp["step"] = "anthropic_key"
        save_checkpoint(cp)
        return cp

    def _on_save_sources(sources):
        cp["sources"] = sources
        save_checkpoint(cp)

    raw_context, sources = get_user_context(paths,
                                             cached_sources=cp.get("sources"),
                                             on_save_sources=_on_save_sources)
    cp["sources"] = sources
    save_checkpoint(cp)

    print("\n  Compiling your profile...")
    profile = compile_profile(client, raw_context)

    cp["user_context"] = profile
    cp["step"] = "select_friends"
    save_checkpoint(cp)
    print("  Done.")
    return cp


def step_select_friends(cp, paths):
    client = get_client(paths["env"])
    if not client:
        cp["step"] = "anthropic_key"
        save_checkpoint(cp)
        return cp

    user_context = cp["user_context"]
    existing = get_existing_friend_names(paths["friends"])

    candidates = cp.get("candidates")
    held_indices = set(cp.get("held_indices", []))
    if candidates:
        print(f"\n  Resuming with {len(candidates)} candidates ({len(held_indices)} invited)...")

    def _on_save(cands, held):
        cp["candidates"] = cands
        cp["held_indices"] = sorted(held)
        save_checkpoint(cp)

    selected = run_selection_loop(
        client, user_context,
        candidates=candidates,
        held_indices=held_indices if candidates else None,
        existing_friends=existing,
        on_save=_on_save,
    )

    if selected is None:
        print("\n  Progress saved. Run again to resume.")
        sys.exit(0)

    print(f"\n  Selected {len(selected)} friends:")
    for c in selected:
        print(f"    {c['name']} -- {c['vibe']}")

    if len(selected) > 3:
        print(f"\n  Warning: You selected {len(selected)} friends.")
        print(f"  BotFather limits bot creation to ~20 per account and may")
        print(f"  throttle you if you create too many at once.")

    def _on_save_soul(name, soul_text):
        cp.setdefault("souls", {})[name] = soul_text
        save_checkpoint(cp)

    souls = generate_souls_for_selected(
        client, selected, user_context, paths["friends"],
        cached_souls=cp.get("souls"),
        on_save_soul=_on_save_soul,
    )

    print()
    for c in selected:
        slug = create_friend_dir(paths["friends"], c["name"],
                                  souls[c["name"]], c)
        print(f"  Created friends/{slug}/")

    cp["selected"] = selected
    cp["step"] = "telegram_bots"
    save_checkpoint(cp)
    return cp


def step_telegram_bots(cp, paths):
    selected = cp["selected"]
    tokens = cp.get("tokens", {})

    needed = []
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        env_key = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"
        existing = os.environ.get(env_key) or load_env(paths["env"]).get(env_key)
        if existing:
            tokens[c["name"]] = existing
        else:
            needed.append(c)

    if not needed:
        print("\n  All Telegram bot tokens already configured.")
        cp["tokens"] = tokens
        cp["step"] = "telegram_group"
        save_checkpoint(cp)
        return cp

    print()
    print("  +------------------------------------------------------------+")
    print("  | Telegram Bot Setup                                         |")
    print("  +------------------------------------------------------------+")
    print("  | You'll chat with your friends in a Telegram group chat.    |")
    print("  | Each friend needs a Telegram bot, you'll create them one   |")
    print("  | by one and paste the tokens below.                         |")
    print("  |                                                            |")
    print("  | After creating ALL bots, you MUST also:                    |")
    print("  |   /setprivacy > select each bot > Disable (group privacy)  |")
    print("  | (This lets bots see all group messages)                    |")
    print("  +------------------------------------------------------------+")

    for c in needed:
        token = collect_bot_token(paths["env"], c["name"])
        if token is None:
            cp["tokens"] = tokens
            save_checkpoint(cp)
            print("\n  Progress saved. Run again to resume.")
            sys.exit(0)
        tokens[c["name"]] = token
        cp["tokens"] = tokens
        save_checkpoint(cp)

    cp["step"] = "telegram_group"
    save_checkpoint(cp)
    return cp


def step_telegram_group(cp, paths):
    import urllib.request

    existing = os.environ.get("TELEGRAM_GROUP_CHAT_ID") or load_env(paths["env"]).get("TELEGRAM_GROUP_CHAT_ID")
    if existing:
        print(f"\n  Group chat ID found: {existing}")
        use = input("  Use this? [Y/n]: ").strip().lower()
        if use in ("", "y", "yes"):
            cp["step"] = "history"
            save_checkpoint(cp)
            return cp

    tokens = cp["tokens"]
    first_token = next(iter(tokens.values()))

    print()
    print("  +------------------------------------------------------+")
    print("  |  Telegram Group Setup                                |")
    print("  +------------------------------------------------------+")
    print("  |  1. Create a new Telegram group                      |")
    print("  |  2. Add ALL your friend bots to the group            |")
    print("  |  3. Send any message in the group                    |")
    print("  |  4. Come back here and press ENTER                   |")
    print("  +------------------------------------------------------+")

    input("\n  Press ENTER after sending a message in the group...")

    print("  Fetching group ID...")
    url = f"https://api.telegram.org/bot{first_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        chat_id = None
        for update in data.get("result", []):
            msg = update.get("message", {})
            chat = msg.get("chat", {})
            if chat.get("type") in ("group", "supergroup"):
                chat_id = chat["id"]
                chat_title = chat.get("title", "Unknown")
                break

        if chat_id:
            print(f"  Found group: '{chat_title}' (ID: {chat_id})")
            set_env_var(paths["env"], "TELEGRAM_GROUP_CHAT_ID", str(chat_id))
            print(f"  Saved to .env")
            cp["step"] = "history"
            save_checkpoint(cp)
            return cp
        else:
            print("  No group messages found. Make sure you:")
            print("    - Added the bots to the group")
            print("    - Disabled group privacy (/setprivacy > Disable)")
            print("    - Removed and re-added bots after disabling privacy")
            print("    - Sent a message AFTER all of the above")
            retry = input("\n  Try again? [Y/n]: ").strip().lower()
            if retry in ("", "y", "yes"):
                return step_telegram_group(cp, paths)

            manual = input("  Enter group chat ID manually (or 'q' to quit): ").strip()
            if manual and manual != "q":
                set_env_var(paths["env"], "TELEGRAM_GROUP_CHAT_ID", manual)
                cp["step"] = "history"
                save_checkpoint(cp)
                return cp
            save_checkpoint(cp)
            print("\n  Progress saved. Run again to resume.")
            sys.exit(0)

    except Exception as e:
        print(f"  Error: {e}")
        manual = input("  Enter group chat ID manually (or 'q' to quit): ").strip()
        if manual and manual != "q":
            set_env_var(paths["env"], "TELEGRAM_GROUP_CHAT_ID", manual)
            cp["step"] = "history"
            save_checkpoint(cp)
            return cp
        save_checkpoint(cp)
        print("\n  Progress saved. Run again to resume.")
        sys.exit(0)


def generate_history(client, souls: dict[str, str], user_context: str) -> str:
    """Generate a shared HISTORY.md for how all the friends know each other."""
    friend_summaries = "\n\n".join(
        f"### {name}\n{soul[:1500]}" for name, soul in souls.items()
    )

    prompt = f"""You are writing a shared history document for a group of friends in a
Telegram group chat.

## The person at the center
{user_context}

## Their friends
{friend_summaries}

Write a HISTORY.md with one section PER FRIEND. Each section should cover:
- How this friend met the central person (be specific — through what, when, where)
- A specific shared memory between them ("remember when...")
- What their friendship is like day-to-day

End with a short paragraph about how the group chat came together.

RULES:
- Keep it grounded and realistic.
- Focus on each friend's relationship WITH THE CENTRAL PERSON. Don't invent
  elaborate backstories for how the friends know each other — they mostly met
  through the group chat.
- About 300-500 words total.
- Write in third person past tense, like a narrator setting the stage.
- Use the friends' actual names, locations, and personalities from their SOULs.
- NEVER use the words "real person", "user", "human", or "bot" — everyone is just a friend.
- NEVER refer to anyone as "the central person" in the output — use their actual name.

Write the HISTORY.md directly, no preamble:"""

    response = client.messages.create(
        model=MODEL, max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def step_history(cp, paths):
    """Offer to generate shared history for the friend group."""
    friends_dir = paths["friends"]
    history_path = friends_dir / "HISTORY.md"

    # Skip if already exists
    if history_path.exists():
        print(f"\n  HISTORY.md already exists.")
        cp["step"] = "deploy"
        save_checkpoint(cp)
        return cp

    print()
    choice = input("  Generate a shared history for your friend group? [d]isplay / [w]rite / [n]o: ").strip().lower()

    if choice == "n":
        cp["step"] = "deploy"
        save_checkpoint(cp)
        return cp

    # Load all souls
    souls = {}
    for d in sorted(friends_dir.iterdir()):
        soul_path = d / "SOUL.md"
        if d.is_dir() and not d.name.startswith(".") and soul_path.exists():
            souls[d.name] = soul_path.read_text()

    if not souls:
        print("  No friends found. Skipping.")
        cp["step"] = "deploy"
        save_checkpoint(cp)
        return cp

    import anthropic
    client = anthropic.Anthropic(api_key=cp.get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY"))
    user_context = cp.get("user_context", "")

    print(f"  Generating shared history for {len(souls)} friends...")
    history = generate_history(client, souls, user_context)

    if choice == "d":
        print()
        print(history)
        print()
        action = input("  [w]rite to file / [r]egenerate / [n]o thanks: ").strip().lower()
        while action == "r":
            print("  Regenerating...")
            history = generate_history(client, souls, user_context)
            print()
            print(history)
            print()
            action = input("  [w]rite to file / [r]egenerate / [n]o thanks: ").strip().lower()
        if action != "w":
            cp["step"] = "deploy"
            save_checkpoint(cp)
            return cp

    history_path.write_text(history)
    print(f"  Wrote HISTORY.md to {history_path}")

    cp["step"] = "deploy"
    save_checkpoint(cp)
    return cp


def step_deploy(cp, paths):
    import urllib.request

    root = paths["root"]
    print()
    print("  +------------------------------------------------------+")
    print("  |  Deploy                                              |")
    print("  +------------------------------------------------------+")
    print("  |  How do you want to run your friend group?           |")
    print("  |                                                      |")
    print("  |  1) Docker (local, always on)                        |")
    print("  |  2) Skip -- I'll deploy myself later                 |")
    print("  +------------------------------------------------------+")
    print()

    choice = input("  Choice [1/2]: ").strip()

    if choice == "1":
        print("\n  Downloading source...")
        tmp_dir = Path(tempfile.mkdtemp(prefix="sudomake-friends-"))
        tarball_path = tmp_dir / "main.tar.gz"

        try:
            req = urllib.request.Request(TARBALL_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                tarball_path.write_bytes(resp.read())
        except Exception as e:
            print(f"  Download failed: {e}")
            print(f"  You can download manually from {TARBALL_URL}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            retry = input("\n  Try a different deploy option? [y/N]: ").strip().lower()
            if retry == "y":
                return step_deploy(cp, paths)
            cp["step"] = "done"
            save_checkpoint(cp)
            return cp

        print("  Extracting...")
        import tarfile
        with tarfile.open(str(tarball_path), "r:gz") as tf:
            tf.extractall(str(tmp_dir))

        build_dir = tmp_dir / "sudomake-friends-main"

        print("  Building Docker image...")
        r = subprocess.run(
            ["docker", "build", "-t", "sudomake-friends", str(build_dir)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  Docker build failed: {r.stderr[:300]}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            retry = input("\n  Try a different deploy option? [y/N]: ").strip().lower()
            if retry == "y":
                return step_deploy(cp, paths)
            cp["step"] = "done"
            save_checkpoint(cp)
            return cp

        # Ensure data directory exists
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Stop existing container if any
        subprocess.run(["docker", "rm", "-f", "sudomake-friends"],
                       capture_output=True, text=True)

        print("  Starting container...")
        r = subprocess.run([
            "docker", "run", "-d",
            "--name", "sudomake-friends",
            "--env-file", str(paths["env"]),
            "-v", f"{paths['friends']}:/app/friends-data",
            "-v", f"{data_dir}:/app/data",
            "-e", "FRIENDS_DIR=/app/friends-data",
            "-e", "DATA_DIR=/app/data",
            "--restart", "unless-stopped",
            "sudomake-friends",
        ], capture_output=True, text=True)

        if r.returncode == 0:
            print("  Running! Check logs with:")
            print("    docker logs -f sudomake-friends")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            cp["step"] = "done"
            save_checkpoint(cp)
            return cp
        else:
            print(f"  Error starting container: {r.stderr[:300]}")

        # Clean up temp dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

    else:
        print(f"\n  Run locally with Docker later:")
        print(f"    # Download and build")
        print(f"    curl -L {TARBALL_URL} | tar xz")
        print(f"    docker build -t sudomake-friends sudomake-friends-main/")
        print(f"    docker run -d --name sudomake-friends \\")
        print(f"      --env-file {paths['env']} \\")
        print(f"      -v {paths['friends']}:/app/friends-data \\")
        print(f"      -v {root / 'data'}:/app/data \\")
        print(f"      -e FRIENDS_DIR=/app/friends-data \\")
        print(f"      -e DATA_DIR=/app/data \\")
        print(f"      --restart unless-stopped \\")
        print(f"      sudomake-friends")

    print()
    retry = input("  Try a different deploy option? [y/N]: ").strip().lower()
    if retry == "y":
        return step_deploy(cp, paths)

    cp["step"] = "done"
    save_checkpoint(cp)
    return cp


def step_done(cp, paths):
    selected = cp.get("selected", [])
    print()
    print("  +---------------------------------------+")
    print("  |  Setup Complete!                      |")
    print("  +---------------------------------------+")
    print()
    print("  Your friends:")
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        print(f"    {c['name']} (friends/{slug}/)")
    print()
    print(f"  Data directory: {paths['root']}")
    print()

    clear_checkpoint()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

STEPS = {
    "start": step_anthropic_key,
    "anthropic_key": step_anthropic_key,
    "user_profile": step_user_profile,
    "select_friends": step_select_friends,
    "telegram_bots": step_telegram_bots,
    "telegram_group": step_telegram_group,
    "history": step_history,
    "deploy": step_deploy,
    "done": step_done,
}


def main():
    global CHECKPOINT_PATH

    # Handle --start-over
    if "--start-over" in sys.argv:
        if HOME_DIR.exists():
            shutil.rmtree(HOME_DIR)
        print("  Cleared all setup state. Starting fresh.\n")
        sys.argv.remove("--start-over")

    print()
    title = "Sudomake Friends -- Initialize"
    inner = f"   {title}   "
    w = len(inner) + 2  # +2 for the || on each side, -2 for the + on each side = same
    print(f"  +{'=' * (len(inner) + 2)}+")
    print(f"  ||{inner}||")
    print(f"  +{'=' * (len(inner) + 2)}+")

    # Ensure home directory exists
    HOME_DIR.mkdir(parents=True, exist_ok=True)

    root = HOME_DIR
    paths = get_paths(root)
    paths["friends"].mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH = root / ".init-checkpoint.json"

    print(f"  Data directory: {root}")

    cp = load_checkpoint()

    # Detect completed setup: .env exists and friends exist
    existing_friends = get_existing_friend_names(paths["friends"])
    setup_complete = paths["env"].exists() and len(existing_friends) > 0

    if setup_complete:
        print(f"\n  Found {len(existing_friends)} friend(s): {', '.join(existing_friends)}")
        print()
        has_incomplete = cp.get("step") and cp["step"] not in ("start", "done", "deploy")
        if has_incomplete:
            print(f"  (In-progress setup at step: {cp['step']})")
        choice = input("  [r]esume, [s]tart over, [d]eploy, or [a]dd friend? [r/s/d/a]: ").strip().lower()
        if choice == "s":
            clear_checkpoint()
            cp = {"step": "start"}
        elif choice == "a":
            # Need a profile for generation — if not in checkpoint, collect one
            if not cp.get("user_context"):
                cp["step"] = "user_profile"
            else:
                cp["step"] = "select_friends"
        elif choice == "d":
            cp["step"] = "deploy"
        else:
            # Resume — if no incomplete checkpoint, resume means deploy
            if not has_incomplete:
                cp["step"] = "deploy"
            # else: continue from current checkpoint step

    elif cp["step"] != "start":
        print(f"\n  In-progress setup at step: {cp['step']}")
        choice = input("  [r]esume, or [s]tart over? [r/s]: ").strip().lower()
        if choice == "s":
            clear_checkpoint()
            cp = {"step": "start"}

    while cp["step"] != "done":
        step_fn = STEPS.get(cp["step"])
        if not step_fn:
            print(f"  Unknown step: {cp['step']}")
            break
        cp = step_fn(cp, paths)

    if cp["step"] == "done":
        step_done(cp, paths)


if __name__ == "__main__":
    main()
