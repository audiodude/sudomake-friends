#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40.0",
#     "pyyaml>=6.0",
# ]
# ///
"""Interactive friend group initialization.

Run directly:  uv run https://raw.githubusercontent.com/audiodude/friend-group/main/scripts/initialize.py
Or locally:    uv run scripts/initialize.py
"""

import curses
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_URL = "https://github.com/audiodude/friend-group.git"
MODEL = "claude-4-sonnet-20250514"

# ─── Project directory resolution ─────────────────────────────────────────────

def resolve_project_dir() -> Path:
    """Find or create the project directory."""
    # If we're already inside a friend-group project (has src/main.py), use it
    cwd = Path.cwd()
    if (cwd / "src" / "main.py").exists() and (cwd / "friends").exists():
        return cwd

    # If there's a friend-group subdir, use that
    if (cwd / "friend-group" / "src" / "main.py").exists():
        return cwd / "friend-group"

    # Otherwise, clone the repo
    target = cwd / "friend-group"
    if not target.exists():
        print(f"\n  Cloning friend-group into {target}...")
        result = subprocess.run(
            ["git", "clone", REPO_URL, str(target)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  Error cloning: {result.stderr}")
            sys.exit(1)
        print("  Done.")
    return target


ROOT = resolve_project_dir()
FRIENDS_DIR = ROOT / "friends"
CHECKPOINT_PATH = ROOT / ".init-checkpoint.json"
ENV_PATH = ROOT / ".env"
SCRAPE_CACHE_DIR = ROOT / ".scrape-cache"


# ─── Checkpoint system ────────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text())
    return {"step": "start"}


def save_checkpoint(data: dict):
    CHECKPOINT_PATH.write_text(json.dumps(data, indent=2))


def clear_checkpoint():
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()


# ─── .env management ─────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_env(env: dict):
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n")


def set_env_var(key: str, value: str):
    env = load_env()
    env[key] = value
    save_env(env)
    os.environ[key] = value


# ─── Claude API ───────────────────────────────────────────────────────────────

def get_client():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY") or load_env().get("ANTHROPIC_API_KEY")
    if not key:
        return None
    os.environ["ANTHROPIC_API_KEY"] = key
    return anthropic.Anthropic(api_key=key)


def generate_candidates(client, context: str, held: list[dict],
                        count: int = 20) -> list[dict]:
    held_desc = ""
    if held:
        held_desc = f"""
These friends have already been selected (do NOT regenerate them, and make sure
new candidates have good chemistry with them):
{json.dumps(held, indent=2)}
"""
    prompt = f"""Based on this profile of a person, generate exactly {count} fictional friend
candidates for a virtual group chat. Each friend should be someone this person
would naturally be friends with — shared interests, compatible personality, etc.

Return ONLY a JSON array of objects. Each object must have:
- "name": first name only
- "age": integer
- "location": city + brief descriptor
- "occupation": what they do
- "vibe": 1-sentence personality summary
- "why": why they'd be this person's friend (1 sentence)
- "timezone": IANA timezone string
- "chattiness": float 0.0-1.0

Make the friends diverse in personality, occupation, location, and timezone.
Some should be local, some remote. Mix of introverts/extroverts, tech/non-tech.
Give them distinct speech patterns and interests that don't overlap too much.
{held_desc}

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
    others_desc = "\n".join(f"- {f['name']}: {f['vibe']}" for f in others)

    prompt = f"""Write a detailed SOUL.md personality file for a virtual chat bot character.
This character will be in a group chat with a real person and other bot characters.

## The character
{json.dumps(candidate, indent=2)}

## Other friends in the group
{others_desc}

## The real person they're friends with
{user_context}

Write the SOUL.md in this exact format:

# {{Name}}

## Identity
- **Age:** ...
- **Location:** ...
- **Occupation:** ...
- **Timezone:** ...

## Personality
(2-3 paragraphs: core traits, emotional patterns, worldview, humor style)

## Interests
(bullet list of specific interests, hobbies, obsessions)

## Relationships
(how they relate to the real person and each of the other friends)

## Speech Patterns
(very specific texting style: capitalization, punctuation, emoji usage, message length,
slang, verbal tics. This section is CRITICAL for making the character feel real.
Include examples of how they'd actually text.)

## Boundaries
(topics they avoid, things that annoy them, conversational pet peeves)

Be specific and vivid. Avoid generic traits. The Speech Patterns section should make
it possible to distinguish this character's messages from any other character at a glance."""

    response = client.messages.create(
        model=MODEL, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ─── Scraper ──────────────────────────────────────────────────────────────────

SCRAPE_TIMEOUT = 180  # 3 minutes total


def _fetch_page(url: str, timeout: int = 10) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_links(html: str, base_url: str) -> list[str]:
    import re
    from urllib.parse import urljoin, urlparse
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
    import re
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def scrape_site(url: str) -> str:
    import hashlib
    SCRAPE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_key = hashlib.md5(url.encode()).hexdigest()
    cache_file = SCRAPE_CACHE_DIR / f"{cache_key}.txt"
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


# ─── User profile ────────────────────────────────────────────────────────────

def get_user_context() -> str:
    print("\nHow should I learn about you to generate good friend candidates?\n")
    print("  1) Enter a URL to scrape (e.g. your personal site)")
    print("  2) Paste or type a description")
    print("  3) Load from a file")
    print()
    choice = input("Choice [1/2/3]: ").strip()

    if choice == "1":
        url = input("URL: ").strip()
        result = scrape_site(url)
        if result:
            return f"Website content from {url}:\n\n{result}"
        print("  Scrape returned nothing. Falling back to manual description.")
        return input("\nDescribe yourself:\n> ")
    elif choice == "3":
        path = input("File path: ").strip()
        return Path(path).expanduser().read_text()[:15000]
    else:
        return input("\nDescribe yourself (interests, personality, location, etc.):\n> ")


# ─── Selection UI ─────────────────────────────────────────────────────────────

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
        header = f" Friend Selection ({n_held} held) "
        stdscr.addstr(0, 0, header, curses.A_BOLD | curses.A_REVERSE)
        stdscr.addstr(1, 0, "─" * min(width - 1, 80))

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
            row = i + 2

            marker = " HOLD " if is_held else "      "
            line = f" {marker} {c['name']:10s} {c['age']:3d}  {c['location'][:25]:25s}  {c['vibe'][:width-52]}"
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

        detail_row = height - 3
        if cursor < len(candidates):
            c = candidates[cursor]
            detail = f"  {c['name']} — {c['occupation']} | {c['why']}"
            stdscr.addstr(detail_row, 0, detail[:width - 1], curses.A_DIM)

        footer_row = height - 1
        if n_held > 0:
            footer = " ENTER=toggle  r=re-roll unheld  a=accept  e=edit+accept  q=save+quit "
        else:
            footer = " ENTER=toggle  r=re-roll unheld  q=save+quit "
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
        elif key == ord("r"):
            return held_indices, "reroll"
        elif key == ord("a") and n_held > 0:
            return held_indices, "accept"
        elif key == ord("e") and n_held > 0:
            return held_indices, "edit"
        elif key == ord("q"):
            return held_indices, "quit"


# ─── Editor ───────────────────────────────────────────────────────────────────

def edit_with_editor(text: str, label: str = "") -> str:
    editor = os.environ.get("EDITOR", "vim")
    suffix = f"-{label}.md" if label else ".md"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(text)
        f.flush()
        tmppath = f.name
    subprocess.call([editor, tmppath])
    with open(tmppath) as f:
        result = f.read()
    os.unlink(tmppath)
    return result


# ─── Friend directory creation ────────────────────────────────────────────────

def create_friend_dir(name: str, soul: str, candidate: dict) -> str:
    slug = name.lower().replace(" ", "_")
    friend_dir = FRIENDS_DIR / slug
    friend_dir.mkdir(parents=True, exist_ok=True)

    (friend_dir / "SOUL.md").write_text(soul)
    (friend_dir / "MEMORY.md").write_text("# Memory\n")

    config = {
        "timezone": candidate.get("timezone", "America/New_York"),
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


# ─── Step: Anthropic key ─────────────────────────────────────────────────────

def step_anthropic_key(cp: dict) -> dict:
    existing = os.environ.get("ANTHROPIC_API_KEY") or load_env().get("ANTHROPIC_API_KEY")
    if existing:
        print(f"\n  Anthropic API key found (sk-ant-...{existing[-6:]})")
        use = input("  Use this key? [Y/n]: ").strip().lower()
        if use in ("", "y", "yes"):
            os.environ["ANTHROPIC_API_KEY"] = existing
            cp["step"] = "user_context"
            save_checkpoint(cp)
            return cp

    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │  Anthropic API Key                      │")
    print("  ├─────────────────────────────────────────┤")
    print("  │  1. Go to console.anthropic.com         │")
    print("  │  2. Sign in or create an account        │")
    print("  │  3. Go to Settings > API Keys           │")
    print("  │  4. Click 'Create Key'                  │")
    print("  │  5. Copy the key (starts with sk-ant-)  │")
    print("  └─────────────────────────────────────────┘")
    print()

    while True:
        key = input("  Paste your API key (or 'q' to quit): ").strip()
        if key.lower() == "q":
            print("\n  Progress saved. Run again to resume.")
            save_checkpoint(cp)
            sys.exit(0)
        if key.startswith("sk-ant-"):
            set_env_var("ANTHROPIC_API_KEY", key)
            print("  Saved to .env")
            cp["step"] = "user_context"
            save_checkpoint(cp)
            return cp
        print("  Doesn't look right (should start with sk-ant-). Try again.")


# ─── Step: User context ──────────────────────────────────────────────────────

def step_user_context(cp: dict) -> dict:
    if cp.get("user_context"):
        print("\n  User profile already collected. Skipping.")
        cp["step"] = "select_friends"
        save_checkpoint(cp)
        return cp

    context = get_user_context()
    cp["user_context"] = context
    cp["step"] = "select_friends"
    save_checkpoint(cp)
    return cp


# ─── Step: Select friends ────────────────────────────────────────────────────

def step_select_friends(cp: dict) -> dict:
    client = get_client()
    if not client:
        print("  Error: No Anthropic API key. Restarting from that step.")
        cp["step"] = "anthropic_key"
        save_checkpoint(cp)
        return cp

    user_context = cp["user_context"]

    if cp.get("candidates"):
        candidates = cp["candidates"]
        held_indices = set(cp.get("held_indices", []))
        print(f"\n  Resuming with {len(candidates)} candidates ({len(held_indices)} held)...")
    else:
        print("\n  Generating candidates...")
        candidates = generate_candidates(client, user_context, [], count=20)
        held_indices = set()

    while True:
        held_indices, action = curses.wrapper(
            selection_ui, candidates, held_indices
        )

        cp["candidates"] = candidates
        cp["held_indices"] = sorted(held_indices)
        save_checkpoint(cp)

        if action == "quit":
            print("\n  Progress saved. Run again to resume.")
            sys.exit(0)

        elif action == "reroll":
            held = [candidates[i] for i in sorted(held_indices)]
            n_new = 20 - len(held)
            print(f"\n  Re-rolling {n_new} candidates (keeping {len(held)} held)...")
            new_candidates = generate_candidates(
                client, user_context, held, count=n_new
            )
            rebuilt = []
            new_iter = iter(new_candidates)
            new_held_indices = set()
            for i, c in enumerate(candidates):
                if i in held_indices:
                    new_held_indices.add(len(rebuilt))
                    rebuilt.append(c)
                else:
                    try:
                        rebuilt.append(next(new_iter))
                    except StopIteration:
                        pass
            for c in new_iter:
                rebuilt.append(c)
            candidates = rebuilt
            held_indices = new_held_indices
            cp["candidates"] = candidates
            cp["held_indices"] = sorted(held_indices)
            save_checkpoint(cp)

        elif action in ("accept", "edit"):
            selected = [candidates[i] for i in sorted(held_indices)]
            print(f"\n  Selected {len(selected)} friends:")
            for c in selected:
                print(f"    {c['name']} — {c['vibe']}")

            if len(selected) > 3:
                print(f"\n  Warning: You selected {len(selected)} friends.")
                print(f"  BotFather limits bot creation to ~20 per account and may")
                print(f"  throttle you if you create too many at once.")
                cont = input("\n  Continue? [Y/n]: ").strip().lower()
                if cont not in ("", "y", "yes"):
                    continue

            print("\n  Generating detailed personalities...")
            souls = cp.get("souls", {})
            for c in selected:
                if c["name"] in souls:
                    print(f"    {c['name']}'s soul already written")
                else:
                    print(f"    Writing {c['name']}'s soul...", end="", flush=True)
                    souls[c["name"]] = generate_soul(client, c, selected, user_context)
                    cp["souls"] = souls
                    save_checkpoint(cp)
                    print(" done")

            if action == "edit":
                for c in selected:
                    print(f"\n  Opening {c['name']}'s SOUL.md in editor...")
                    souls[c["name"]] = edit_with_editor(
                        souls[c["name"]], label=c["name"].lower()
                    )
                    cp["souls"] = souls
                    save_checkpoint(cp)

            print()
            for c in selected:
                slug = create_friend_dir(c["name"], souls[c["name"]], c)
                print(f"  Created friends/{slug}/")

            cp["selected"] = selected
            cp["step"] = "telegram_bots"
            save_checkpoint(cp)
            return cp


# ─── Step: Telegram bot tokens ───────────────────────────────────────────────

def step_telegram_bots(cp: dict) -> dict:
    selected = cp["selected"]
    tokens = cp.get("tokens", {})

    needed = []
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        env_key = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"
        existing = os.environ.get(env_key) or load_env().get(env_key)
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
    print("  ┌─────────────────────────────────────────────────────┐")
    print("  │  Telegram Bot Setup                                 │")
    print("  ├─────────────────────────────────────────────────────┤")
    print("  │  You need a Telegram bot for each friend.           │")
    print("  │                                                     │")
    print("  │  For EACH friend below:                             │")
    print("  │    1. Open Telegram, message @BotFather             │")
    print("  │    2. Send /newbot                                  │")
    print("  │    3. Set the display name (shown below)            │")
    print("  │    4. Set a username (must end in 'bot')            │")
    print("  │    5. Copy the token BotFather gives you            │")
    print("  │    6. Paste it here                                 │")
    print("  │                                                     │")
    print("  │  After creating ALL bots, you MUST also:            │")
    print("  │    1. Send /setprivacy to @BotFather                │")
    print("  │    2. Select each bot                               │")
    print("  │    3. Choose 'Disable'                              │")
    print("  │  (This lets bots see all group messages)            │")
    print("  └─────────────────────────────────────────────────────┘")

    for c in needed:
        slug = c["name"].lower().replace(" ", "_")
        env_key = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"
        print(f"\n  Bot for {c['name']}:")
        print(f"    Display name: {c['name']}")
        print(f"    Suggested username: tm_{slug}_bot")

        while True:
            token = input(f"    Paste token (or 'q' to save+quit): ").strip()
            if token.lower() == "q":
                cp["tokens"] = tokens
                save_checkpoint(cp)
                print("\n  Progress saved. Run again to resume.")
                sys.exit(0)
            if ":" in token and len(token) > 20:
                set_env_var(env_key, token)
                tokens[c["name"]] = token
                cp["tokens"] = tokens
                save_checkpoint(cp)
                print(f"    Saved")
                break
            print("    Doesn't look like a bot token. Try again.")

    print("\n  All bot tokens collected!")
    print()
    print("  Now disable privacy mode for each bot:")
    print("    1. Message @BotFather")
    print("    2. Send /setprivacy")
    print("    3. Select each bot and choose 'Disable'")
    print()
    print("  Then remove and re-add each bot to the group")
    print("  (privacy changes only take effect on re-join)")
    input("\n  Press ENTER when done...")

    cp["step"] = "telegram_group"
    save_checkpoint(cp)
    return cp


# ─── Step: Telegram group ────────────────────────────────────────────────────

def step_telegram_group(cp: dict) -> dict:
    existing = os.environ.get("TELEGRAM_GROUP_CHAT_ID") or load_env().get("TELEGRAM_GROUP_CHAT_ID")
    if existing:
        print(f"\n  Group chat ID found: {existing}")
        use = input("  Use this? [Y/n]: ").strip().lower()
        if use in ("", "y", "yes"):
            cp["step"] = "done"
            save_checkpoint(cp)
            return cp

    tokens = cp["tokens"]
    first_token = next(iter(tokens.values()))

    print()
    print("  ┌─────────────────────────────────────────────────────┐")
    print("  │  Telegram Group Setup                               │")
    print("  ├─────────────────────────────────────────────────────┤")
    print("  │  1. Create a new Telegram group                     │")
    print("  │  2. Add ALL your friend bots to the group           │")
    print("  │  3. Send any message in the group                   │")
    print("  │  4. Come back here and press ENTER                  │")
    print("  └─────────────────────────────────────────────────────┘")

    input("\n  Press ENTER after sending a message in the group...")

    print("  Fetching group ID...")
    import urllib.request
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
            set_env_var("TELEGRAM_GROUP_CHAT_ID", str(chat_id))
            print(f"  Saved to .env")
            cp["step"] = "done"
            save_checkpoint(cp)
            return cp
        else:
            print("  No group messages found. Make sure you:")
            print("    - Added the bots to the group")
            print("    - Disabled privacy mode (/setprivacy > Disable)")
            print("    - Removed and re-added bots after disabling privacy")
            print("    - Sent a message AFTER all of the above")
            retry = input("\n  Try again? [Y/n]: ").strip().lower()
            if retry in ("", "y", "yes"):
                return step_telegram_group(cp)

            manual = input("  Enter group chat ID manually (or 'q' to quit): ").strip()
            if manual and manual != "q":
                set_env_var("TELEGRAM_GROUP_CHAT_ID", manual)
                cp["step"] = "done"
                save_checkpoint(cp)
                return cp
            print("\n  Progress saved. Run again to resume.")
            save_checkpoint(cp)
            sys.exit(0)

    except Exception as e:
        print(f"  Error: {e}")
        manual = input("  Enter group chat ID manually (or 'q' to quit): ").strip()
        if manual and manual != "q":
            set_env_var("TELEGRAM_GROUP_CHAT_ID", manual)
            cp["step"] = "done"
            save_checkpoint(cp)
            return cp
        print("\n  Progress saved. Run again to resume.")
        save_checkpoint(cp)
        sys.exit(0)


# ─── Step: Done ───────────────────────────────────────────────────────────────

def step_done(cp: dict):
    selected = cp.get("selected", [])
    print()
    print("  ┌──────────────────────────────────────┐")
    print("  │  Setup Complete!                      │")
    print("  └──────────────────────────────────────┘")
    print()
    print(f"  Project directory: {ROOT}")
    print()
    print("  Your friends:")
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        print(f"    {c['name']} (friends/{slug}/)")
    print()
    print("  Run locally:")
    print(f"    cd {ROOT}")
    print("    uv sync && uv run python -m src.main")
    print()
    print("  Deploy to Railway:")
    print("    Set the env vars from .env on your Railway service")
    print("    railway up --detach")
    print()
    clear_checkpoint()


# ─── Main ─────────────────────────────────────────────────────────────────────

STEPS = {
    "start": step_anthropic_key,
    "anthropic_key": step_anthropic_key,
    "user_context": step_user_context,
    "select_friends": step_select_friends,
    "telegram_bots": step_telegram_bots,
    "telegram_group": step_telegram_group,
    "done": step_done,
}


def main():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Friend Group — Initialize          ║")
    print("  ╚══════════════════════════════════════╝")
    print(f"  Project: {ROOT}")

    cp = load_checkpoint()

    if cp["step"] != "start":
        print(f"\n  Resuming from: {cp['step']}")
        reset = input("  Start over? [y/N]: ").strip().lower()
        if reset == "y":
            clear_checkpoint()
            cp = {"step": "start"}

    while cp["step"] != "done":
        step_fn = STEPS.get(cp["step"])
        if not step_fn:
            print(f"  Unknown step: {cp['step']}")
            break
        cp = step_fn(cp)

    if cp["step"] == "done":
        step_done(cp)


if __name__ == "__main__":
    main()
