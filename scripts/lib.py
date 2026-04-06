"""Shared library for friend-group scripts."""

import curses
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

MODEL = "claude-4-sonnet-20250514"
CANDIDATE_COUNT = 8
SCRAPE_TIMEOUT = 180


# ─── Project directory ────────────────────────────────────────────────────────

def resolve_project_dir() -> Path:
    """Find the project directory. Must already exist for lib consumers."""
    cwd = Path.cwd()
    if (cwd / "src" / "main.py").exists() and (cwd / "friends").exists():
        return cwd
    if (cwd / "friend-group" / "src" / "main.py").exists():
        return cwd / "friend-group"
    return cwd  # fallback, caller handles


def get_paths(root: Path) -> dict:
    return {
        "root": root,
        "friends": root / "friends",
        "env": root / ".env",
        "profile": root / "PROFILE.md",
        "scrape_cache": root / ".scrape-cache",
    }


# ─── .env management ─────────────────────────────────────────────────────────

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


# ─── Claude API ───────────────────────────────────────────────────────────────

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
candidates for a virtual group chat. Each friend should be someone this person
would naturally be friends with — shared interests, compatible personality, etc.

Return ONLY a JSON array of objects. Each object must have:
- "name": first name only, capitalized (prefer gender-neutral names or gender-ambiguous nicknames)
- "age": integer
- "location": city and country only (e.g. "Berlin, Germany" or "San Francisco, CA")
- "occupation": what they do
- "vibe": personality description (1-3 sentences — who they are, how they act, what makes them interesting)
- "why": why they'd be this person's friend (1 sentence)
- "timezone": IANA timezone string
- "chattiness": float 0.0-1.0

Make the friends diverse in personality, occupation, location, and timezone.
Some should be local, some remote. Mix of introverts/extroverts, tech/non-tech.
Give them distinct speech patterns and interests that don't overlap too much.
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


# ─── User profile ────────────────────────────────────────────────────────────

def compile_profile(client, raw_context: str) -> str:
    """Distill raw scrape/text into a clean, reusable user profile."""
    prompt = f"""Based on the following raw content about a person, write a concise but
detailed profile summary (300-500 words). Cover:
- Who they are (name, location, job)
- Personality traits and values
- Interests, hobbies, creative pursuits
- Social style and communication preferences
- What kind of people they'd naturally be friends with

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


def load_or_create_profile(client, paths: dict) -> str | None:
    """Load existing profile or return None if it needs to be created."""
    if paths["profile"].exists():
        return paths["profile"].read_text()
    return None


def save_profile(paths: dict, profile: str):
    paths["profile"].write_text(profile)


# ─── Scraper ──────────────────────────────────────────────────────────────────

def _fetch_page(url: str, timeout: int = 10) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_links(html: str, base_url: str) -> list[str]:
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
    from platforms import detect_platform

    plugin = detect_platform(url)
    if plugin:
        print(f"  Detected: {plugin.NAME}")
        return plugin.fetch(url, paths["scrape_cache"])

    # Generic website scrape
    result = scrape_site(url, paths["scrape_cache"])
    if result:
        return f"Website content from {url}:\n\n{result}"
    return ""


def _show_platform_help():
    """Show supported platforms."""
    from platforms import get_plugins
    print("\n  Supported platforms (auto-detected from URL):\n")
    for p in get_plugins():
        print(f"    {p.NAME:16s} {p.DESCRIPTION}")
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
                     cached_sources: list[dict] | None = None) -> tuple[str, list[dict]]:
    """Interactive loop to collect user context from multiple sources.

    cached_sources: list of {"label": str, "content": str} from a previous run.
    Returns (joined_context, sources_list) where sources_list can be checkpointed.
    """
    all_parts = []
    sources = []

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

    print("\n  Tell me about yourself. You can provide as many sources as you want.")
    print("  The more context, the better your friends will match you.\n")
    print("  Enter a URL, file path, or type a description.")
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
                print(f"  Added. Enter another, or q to finish.")
            else:
                print("  Got nothing from that URL. Try another?")
            continue

        # File path?
        path = Path(entry).expanduser()
        if path.exists() and path.is_file():
            if path.suffix in (".tgz", ".gz", ".tar", ".zip"):
                content = _extract_text_from_archive(path)
            else:
                try:
                    content = path.read_text()[:15000]
                except UnicodeDecodeError:
                    print(f"  Can't read {path.name} as text. Skipping.")
                    continue
            if content:
                all_parts.append(f"File ({path.name}):\n{content}")
                sources.append({"label": path.name, "content": f"File ({path.name}):\n{content}"})
                print(f"  Added {path.name}. Enter another, or q to finish.")
            else:
                print(f"  No readable text found in {path.name}.")
            continue

        # Treat as free-text description
        all_parts.append(f"Self-description:\n{entry}")
        sources.append({"label": "description", "content": f"Self-description:\n{entry}"})
        print(f"  Added. Enter another, or q to finish.")

    return "\n\n---\n\n".join(all_parts), sources


# ─── Selection UI ─────────────────────────────────────────────────────────────

def _show_detail_modal(stdscr, candidate: dict):
    """Show a centered modal with full candidate details. ESC/q to dismiss."""
    while True:
        height, width = stdscr.getmaxyx()

        # Modal dimensions — large as possible with padding
        pad_x, pad_y = 4, 2
        modal_w = min(width - pad_x * 2, 80)
        modal_h = height - pad_y * 2
        start_x = (width - modal_w) // 2
        start_y = pad_y

        # Word-wrap helper
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

        # Build content lines
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

        # Draw modal
        stdscr.clear()

        # Border
        for y in range(start_y, start_y + modal_h):
            if y >= height:
                break
            stdscr.addstr(y, start_x, "│", curses.A_DIM)
            stdscr.addstr(y, start_x + modal_w - 1, "│", curses.A_DIM)
        top_border = "┌" + "─" * (modal_w - 2) + "┐"
        bot_border = "└" + "─" * (modal_w - 2) + "┘"
        stdscr.addstr(start_y, start_x, top_border[:width - start_x], curses.A_DIM)
        if start_y + modal_h - 1 < height:
            stdscr.addstr(start_y + modal_h - 1, start_x, bot_border[:width - start_x], curses.A_DIM)

        # Title bar
        title = f" {candidate['name']} "
        stdscr.addstr(start_y, start_x + (modal_w - len(title)) // 2,
                       title, curses.A_BOLD | curses.A_REVERSE)

        # Content
        for i, line in enumerate(lines):
            row = start_y + 2 + i
            if row >= start_y + modal_h - 2:
                break
            text = line[:modal_w - 4]
            stdscr.addstr(row, start_x + 1, text)

        # Footer
        dismiss = " ESC/q to close "
        if start_y + modal_h - 1 < height:
            stdscr.addstr(start_y + modal_h - 1,
                           start_x + (modal_w - len(dismiss)) // 2,
                           dismiss, curses.A_DIM)

        stdscr.refresh()
        key = stdscr.getch()
        if key in (27, ord("q")):  # ESC or q
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
        visible_rows = height - 5  # header + col header + divider + footer + pad

        n_held = len(held_indices)
        header = f" Friend Selection ({n_held} invited) "
        stdscr.addstr(0, 0, header, curses.A_BOLD | curses.A_REVERSE)

        # Column layout: [marker] Name | Location | Vibe
        col_mark = 7    # " INV. "
        col_name = 12
        col_loc = 20
        col_vibe = max(10, width - col_mark - col_name - col_loc - 4)

        hdr_line = f" {'':6s} {'Name':<{col_name}s} {'Location':<{col_loc}s} {'Vibe'}"
        stdscr.addstr(1, 0, hdr_line[:width - 1], curses.A_DIM)
        stdscr.addstr(2, 0, "─" * min(width - 1, col_mark + col_name + col_loc + col_vibe + 4))

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
            row = i + 3  # after header + column header + divider

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
        footer = " ENTER=invite  x=expand  e=edit  r=re-roll  a=accept+continue  q=save+quit "
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
        elif key == ord("a"):
            if n_held > 0:
                return held_indices, "accept"
            # Flash warning
            warn = " You want at least one friend right? Press ENTER to invite each friend."
            stdscr.addstr(footer_row, 0, warn[:width - 1],
                          curses.color_pair(4) | curses.A_BOLD)
            stdscr.refresh()
            stdscr.getch()  # wait for any key to dismiss
        elif key == ord("q"):
            return held_indices, "quit"


# ─── Editor ───────────────────────────────────────────────────────────────────

def check_editor() -> str | None:
    """Check if an editor is available. Returns the editor command or None."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor:
        return editor
    # Try common editors
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
    """Format a candidate the same way the modal shows it."""
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
    """Parse edited text back into a candidate dict."""
    c = dict(original)  # keep fields like chattiness that aren't shown
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
    return c


def run_selection_loop(
    client,
    user_context: str,
    candidates: list[dict] | None = None,
    held_indices: set[int] | None = None,
    existing_friends: list[str] | None = None,
    on_save=None,
) -> list[dict] | None:
    """Shared TUI selection loop. Returns selected candidates or None if quit.

    on_save(candidates, held_indices) is called after each state change
    so callers can persist to checkpoint.
    """
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
            new_candidates = generate_candidates(
                client, user_context, held,
                existing_friends=existing_friends, count=n_new,
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
            if on_save:
                on_save(candidates, held_indices)

        elif action == "accept":
            selected = [candidates[i] for i in sorted(held_indices)]
            return selected


def generate_souls_for_selected(
    client,
    selected: list[dict],
    user_context: str,
    friends_dir: Path,
    cached_souls: dict | None = None,
    on_save_soul=None,
) -> dict:
    """Generate SOUL.md for each selected candidate. Returns {name: soul_text}.

    Checks disk and cached_souls before generating.
    on_save_soul(name, soul_text) called after each generation for checkpointing.
    """
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


# ─── Friend directory ─────────────────────────────────────────────────────────

def create_friend_dir(friends_dir: Path, name: str, soul: str,
                      candidate: dict) -> str:
    slug = name.lower().replace(" ", "_")
    friend_dir = friends_dir / slug
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


def get_existing_friend_names(friends_dir: Path) -> list[str]:
    """Return names of friends that already have SOUL.md files."""
    names = []
    if friends_dir.exists():
        for d in sorted(friends_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "SOUL.md").exists():
                names.append(d.name)
    return names


# ─── Bot token collection ────────────────────────────────────────────────────

def _set_bot_display_name(token: str, name: str) -> bool:
    """Set a bot's display name via the Telegram API."""
    import urllib.request
    import urllib.parse
    url = f"https://api.telegram.org/bot{token}/setMyName?name={urllib.parse.quote(name)}"
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
            import readline
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
