"""Load and resolve configuration."""

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FRIENDS_DIR = Path(os.environ.get("FRIENDS_DIR", str(ROOT / "friends")))
# Persistent data dir — use volume mount if available, else project root
DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH",
                os.environ.get("DATA_DIR", str(ROOT))))


def _resolve_env(value: str) -> str:
    """Replace ${VAR} with environment variable values."""
    def replacer(match):
        var = match.group(1)
        return os.environ.get(var, "")
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", replacer, value)
    return value


def _resolve_dict(d: dict) -> dict:
    """Recursively resolve env vars in a dict."""
    resolved = {}
    for k, v in d.items():
        if isinstance(v, dict):
            resolved[k] = _resolve_dict(v)
        elif isinstance(v, str):
            resolved[k] = _resolve_env(v)
        else:
            resolved[k] = v
    return resolved


def load_config() -> dict:
    config_path = ROOT / "config.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return _resolve_dict(raw)


def load_friend_config(name: str) -> dict:
    friend_dir = FRIENDS_DIR / name
    config_path = friend_dir / "config.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    resolved = _resolve_dict(raw)
    # Auto-resolve telegram token from TELEGRAM_BOT_TOKEN_<NAME> if not set
    if not resolved.get("telegram_token"):
        env_key = f"TELEGRAM_BOT_TOKEN_{name.upper()}"
        resolved["telegram_token"] = os.environ.get(env_key, "")
    return resolved


def load_friend_soul(name: str) -> str:
    soul_path = FRIENDS_DIR / name / "SOUL.md"
    return soul_path.read_text()


def _memory_path(name: str) -> Path:
    """Memory lives on the volume so it persists across deploys."""
    d = DATA_DIR / "memories" / name
    d.mkdir(parents=True, exist_ok=True)
    return d / "MEMORY.md"


def load_friend_memory(name: str) -> str:
    path = _memory_path(name)
    if path.exists():
        return path.read_text()
    return ""


def save_friend_memory(name: str, content: str):
    path = _memory_path(name)
    path.write_text(content)


def get_friend_names() -> list[str]:
    """Return list of configured friend directory names."""
    names = []
    for d in sorted(FRIENDS_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / "SOUL.md").exists():
            names.append(d.name)
    return names
