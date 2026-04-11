import json
import os
import readline
from pathlib import Path
from urllib.parse import quote

from wizard.paths import load_env, set_env_var


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
