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

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_URL = "https://github.com/audiodude/friend-group.git"


# ─── Bootstrap ────────────────────────────────────────────────────────────────
# The script works in a staging directory (temp or existing project).
# No files are written to the user's filesystem until they choose to
# deploy or explicitly save the project.

def _find_project() -> Path | None:
    """Check if we're already inside a friend-group project."""
    cwd = Path.cwd()
    if (cwd / "src" / "main.py").exists() and (cwd / "friends").exists():
        return cwd
    if (cwd / "friend-group" / "src" / "main.py").exists():
        return cwd / "friend-group"
    return None


def _bootstrap() -> tuple[Path, bool]:
    """Ensure we have a project directory. Returns (path, is_temp).

    If already in a project dir, uses that (is_temp=False).
    Otherwise clones to a temp dir (is_temp=True).
    """
    existing = _find_project()
    if existing:
        return existing, False

    # Clone to a persistent temp location (survives checkpoint/resume)
    staging = Path(tempfile.gettempdir()) / "friend-group-setup"
    if not (staging / "src" / "main.py").exists():
        print(f"\n  Setting up workspace...")
        if staging.exists():
            shutil.rmtree(staging)
        result = subprocess.run(
            ["git", "clone", REPO_URL, str(staging)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  Error: {result.stderr}")
            sys.exit(1)
        print("  Ready.")
    return staging, True


_project, _is_temp = _bootstrap()

# Re-exec the repo's copy if we're running from a URL download
if not (Path(__file__).resolve().parent / "lib.py").exists():
    repo_script = _project / "scripts" / "initialize.py"
    os.execv(sys.executable, [sys.executable, str(repo_script)] + sys.argv[1:])

sys.path.insert(0, str(_project / "scripts"))

import curses  # noqa: E402
import json  # noqa: E402
from lib import (  # noqa: E402
    get_client, get_paths, get_user_context,
    load_env, set_env_var, compile_profile,
    load_or_create_profile, save_profile,
    run_selection_loop, generate_souls_for_selected,
    create_friend_dir, get_existing_friend_names, collect_bot_token,
)


# ─── Checkpoint ───────────────────────────────────────────────────────────────

CHECKPOINT_PATH = None  # set after ROOT is known


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


# ─── Steps ────────────────────────────────────────────────────────────────────

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
    profile = load_or_create_profile(get_client(paths["env"]), paths)
    if profile:
        print(f"\n  User profile found ({len(profile)} chars).")
        redo = input("  Create a new profile? [y/N]: ").strip().lower()
        if redo != "y":
            cp["user_context"] = profile
            cp["step"] = "select_friends"
            save_checkpoint(cp)
            return cp
        paths["profile"].unlink()

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
    save_profile(paths, profile)
    print(f"  Saved to PROFILE.md ({len(profile)} chars)")

    cp["user_context"] = profile
    cp["step"] = "select_friends"
    save_checkpoint(cp)
    return cp


def step_select_friends(cp, paths):
    from lib import get_existing_friend_names, create_friend_dir

    client = get_client(paths["env"])
    if not client:
        cp["step"] = "anthropic_key"
        save_checkpoint(cp)
        return cp

    user_context = cp["user_context"]
    existing = get_existing_friend_names(paths["friends"])

    # Restore from checkpoint or start fresh
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
        print(f"    {c['name']} — {c['vibe']}")

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
    print("  ┌──────────────────────────────────────────────────────────┐")
    print("  │ Telegram Bot Setup                                       │")
    print("  ├──────────────────────────────────────────────────────────┤")
    print("  │ You'll chat with your friends in a Telegram group chat.  │")
    print("  │ Each friend needs a Telegram bot, you'll create them one │")
    print("  │ by one and paste the tokens below.                       │")
    print("  │                                                          │")
    print("  │ After creating ALL bots, you MUST also:                  │")
    print("  │   /setprivacy > select each bot > Disable                │")
    print("  │ (This lets bots see all group messages)                  │")
    print("  └──────────────────────────────────────────────────────────┘")

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
    existing = os.environ.get("TELEGRAM_GROUP_CHAT_ID") or load_env(paths["env"]).get("TELEGRAM_GROUP_CHAT_ID")
    if existing:
        print(f"\n  Group chat ID found: {existing}")
        use = input("  Use this? [Y/n]: ").strip().lower()
        if use in ("", "y", "yes"):
            cp["step"] = "deploy"
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
            set_env_var(paths["env"], "TELEGRAM_GROUP_CHAT_ID", str(chat_id))
            print(f"  Saved to .env")
            cp["step"] = "deploy"
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
                return step_telegram_group(cp, paths)

            manual = input("  Enter group chat ID manually (or 'q' to quit): ").strip()
            if manual and manual != "q":
                set_env_var(paths["env"], "TELEGRAM_GROUP_CHAT_ID", manual)
                cp["step"] = "deploy"
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
            cp["step"] = "deploy"
            save_checkpoint(cp)
            return cp
        save_checkpoint(cp)
        print("\n  Progress saved. Run again to resume.")
        sys.exit(0)


def _copy_project(src: Path, dest: Path):
    """Copy the project to a user-chosen location."""
    if dest.exists():
        overwrite = input(f"  {dest} exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            return False
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(
        "__pycache__", ".scrape-cache", ".init-checkpoint.json",
    ))
    return True


def _ask_save_location(root: Path) -> Path | None:
    """Ask the user where to save the project. Returns path or None."""
    default = Path.cwd() / "friend-group"
    print(f"\n  Where would you like to save your friend group project?")

    import readline
    def _prefill():
        readline.insert_text(str(default))
        readline.redisplay()
    readline.set_pre_input_hook(_prefill)
    dest_str = input("  Path: ").strip()
    readline.set_pre_input_hook(None)

    if not dest_str:
        return None

    dest = Path(dest_str).expanduser().resolve()

    # Validate parent exists
    if not dest.parent.exists():
        print(f"  Parent directory {dest.parent} doesn't exist.")
        return None

    if _copy_project(root, dest):
        print(f"  Saved to {dest}")
        return dest
    return None


def step_deploy(cp, paths):
    root = paths["root"]
    print()
    print("  ┌─────────────────────────────────────────────────────┐")
    print("  │  Deploy (optional)                                  │")
    print("  ├─────────────────────────────────────────────────────┤")
    print("  │  How do you want to run your friend group?          │")
    print("  │                                                     │")
    print("  │  1) Docker Compose (local, always on)               │")
    print("  │  2) Railway (cloud, ~$5/mo)                         │")
    print("  │  3) Fly.io (cloud, free tier available)             │")
    print("  │  4) Skip — I'll deploy myself later                 │")
    print("  └─────────────────────────────────────────────────────┘")
    print()

    choice = input("  Choice [1/2/3/4]: ").strip()

    if choice == "1":
        print("\n  Starting with docker compose...")
        for cmd in [["docker", "compose"], ["docker-compose"]]:
            r = subprocess.run(cmd + ["version"], capture_output=True, text=True)
            if r.returncode == 0:
                r = subprocess.run(cmd + ["up", "-d", "--build"],
                                   cwd=str(root), capture_output=True, text=True)
                if r.returncode == 0:
                    compose_cmd = " ".join(cmd)
                    print(f"  Running! Check logs with:")
                    print(f"    {compose_cmd} -f {root}/docker-compose.yml logs -f")
                else:
                    print(f"  Error: {r.stderr[:200]}")
                break
        else:
            print("  Docker Compose not found. Install it:")
            print("    Arch: sudo pacman -S docker-compose")
            print("    Ubuntu: sudo apt install docker-compose-v2")
            print("    Mac: included with Docker Desktop")

    elif choice in ("2", "3", "4"):
        # These need a local copy of the project
        if _is_temp:
            if choice == "2":
                label = "Railway"
            elif choice == "3":
                label = "Fly.io"
            else:
                label = "later use"

            print(f"\n  To deploy to {label}, you'll need a local copy of the project.")
            dest = _ask_save_location(root)
            if dest:
                root = dest
            else:
                print("  Skipped saving. The project is temporarily at:")
                print(f"    {root}")
                print("  It will be lost when you clean up.")

        if choice == "2":
            print(f"\n  Deploy to Railway:")
            print(f"    cd {root}")
            print("    railway init --name friend-group")
            print("    # Set env vars from .env in the Railway dashboard")
            print("    # Add a volume mounted at /app/data")
            print("    railway up --detach")
        elif choice == "3":
            print(f"\n  Deploy to Fly.io:")
            print(f"    cd {root}")
            print("    flyctl launch --no-deploy")
            print("    flyctl secrets set $(cat .env | xargs)")
            print("    flyctl volumes create friend_data --size 1")
            print("    flyctl deploy")
        else:
            print(f"\n  Run locally with:")
            print(f"    cd {root}")
            print("    uv sync && uv run python -m src.main")

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
    print("  ┌──────────────────────────────────────┐")
    print("  │  Setup Complete!                      │")
    print("  └──────────────────────────────────────┘")
    print()
    print("  Your friends:")
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        print(f"    {c['name']} (friends/{slug}/)")
    print()

    # Clean up temp staging dir
    if _is_temp:
        clean = input("  Clean up temporary files? [Y/n]: ").strip().lower()
        if clean in ("", "y", "yes"):
            shutil.rmtree(paths["root"], ignore_errors=True)
            print("  Cleaned up.")

    clear_checkpoint()


# ─── Main ─────────────────────────────────────────────────────────────────────

STEPS = {
    "start": step_anthropic_key,
    "anthropic_key": step_anthropic_key,
    "user_profile": step_user_profile,
    "select_friends": step_select_friends,
    "telegram_bots": step_telegram_bots,
    "telegram_group": step_telegram_group,
    "deploy": step_deploy,
    "done": step_done,
}


def main():
    global CHECKPOINT_PATH

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Friend Group — Initialize          ║")
    print("  ╚══════════════════════════════════════╝")

    root = _project
    paths = get_paths(root)
    CHECKPOINT_PATH = root / ".init-checkpoint.json"

    if not _is_temp:
        print(f"  Project: {root}")

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
        cp = step_fn(cp, paths)

    if cp["step"] == "done":
        step_done(cp, paths)


if __name__ == "__main__":
    main()
