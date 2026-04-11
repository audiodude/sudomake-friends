import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from wizard.checkpoint import save_checkpoint, clear_checkpoint
from wizard.claude import get_client, generate_candidates, compile_profile, MODEL
from wizard.friends import (
    get_existing_friend_names,
    generate_souls_for_selected,
    create_friend_dir,
)
from wizard.paths import load_env, set_env_var
from wizard.scraper import get_user_context
from wizard.selection import run_selection_loop
from wizard.telegram_setup import collect_bot_token

TARBALL_URL = "https://github.com/audiodude/sudomake-friends/archive/main.tar.gz"


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
    profile_path = paths["root"] / "profile.txt"

    # Check checkpoint first, then file
    existing_profile = cp.get("user_context") or (
        profile_path.read_text() if profile_path.exists() else ""
    )
    if existing_profile:
        print(f"\n  Profile already compiled ({len(existing_profile)} chars).")
        redo = input("  Create a new profile? [y/N]: ").strip().lower()
        if redo != "y":
            cp["user_context"] = existing_profile
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
    profile_path.write_text(profile)
    cp["step"] = "select_friends"
    # Clear cached candidates so friends are regenerated from new profile
    cp.pop("candidates", None)
    cp.pop("held_indices", None)
    save_checkpoint(cp)
    print("  Done.")
    print("  Note: You should re-roll your friends to match the new profile.")
    return cp


def step_select_friends(cp, paths):
    client = get_client(paths["env"])
    if not client:
        cp["step"] = "anthropic_key"
        save_checkpoint(cp)
        return cp

    user_context = cp["user_context"]
    existing = get_existing_friend_names(paths["friends"])

    # If friends already exist, offer to keep or edit them
    if existing and not cp.get("candidates"):
        print(f"\n  Current friends: {', '.join(existing)}")
        keep = input("  Keep these friends? [Y/n/[e]dit]: ").strip().lower()
        if keep in ("", "y", "yes"):
            cp["step"] = "telegram_bots"
            cp["selected"] = [{"name": n} for n in existing]
            save_checkpoint(cp)
            return cp
        elif keep == "e":
            # Load existing friends as pre-held candidates in the TUI
            candidates = []
            for name in existing:
                cpath = paths["friends"] / name / "candidate.json"
                if cpath.exists():
                    candidates.append(json.loads(cpath.read_text()))
                else:
                    print(f"  {name} was created before edit support — no candidate data.")
                    print(f"  Start over to regenerate, or keep as-is.")
                    cp["step"] = "telegram_bots"
                    cp["selected"] = [{"name": n} for n in existing]
                    save_checkpoint(cp)
                    return cp
            held_indices = set(range(len(candidates)))
            # Pad to CANDIDATE_COUNT with new candidates
            from wizard.claude import CANDIDATE_COUNT
            n_new = CANDIDATE_COUNT - len(candidates)
            if n_new > 0:
                print(f"\n  Generating {n_new} new candidates...")
                new = generate_candidates(client, user_context, candidates,
                                          existing_friends=existing, count=n_new)
                candidates = candidates + new
                candidates = candidates[:CANDIDATE_COUNT]
            cp["candidates"] = candidates
            cp["held_indices"] = sorted(held_indices)
            save_checkpoint(cp)
            # Fall through to the normal selection loop below

    if cp.get("candidates"):
        candidates = cp["candidates"]
        held_indices = set(cp.get("held_indices", []))
    else:
        candidates = None
        held_indices = None
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

    # If already exists, offer to keep or redo
    if history_path.exists():
        print(f"\n  HISTORY.md already exists.")
        redo = input("  Regenerate? [y/N]: ").strip().lower()
        if redo != "y":
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
        action = input("  [w]rite to file, [r]egenerate, or [q] to continue:").strip().lower()
        while action == "r":
            print("  Regenerating...")
            history = generate_history(client, souls, user_context)
            print()
            print(history)
            print()
            action = input("  [w]rite to file, [r]egenerate, or [q] to continue:").strip().lower()
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
        print("  Or deploy to a cloud platform:")
        print()
        print("  Railway:")
        print("    railway init && railway link")
        print("    railway volume add --mount /app/data")
        print("    railway variables set DATA_DIR=/app/data")
        print("    railway up --detach")
        print()
        print("  Fly.io:")
        print("    fly launch --no-deploy")
        print("    fly volumes create friend_data --size 1")
        print("    Add to fly.toml:")
        print('      [mounts]')
        print('        source = "friend_data"')
        print('        destination = "/app/data"')
        print('      [env]')
        print('        DATA_DIR = "/app/data"')
        print("    fly deploy")
        print()
        print("  Any platform that runs Docker + persistent volumes works.")
        print(f"  Key: set DATA_DIR=/app/data and mount friends at /app/friends-data.")

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
