import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse, unquote


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
    import urllib.request
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
