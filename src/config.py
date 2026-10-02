"""Load and resolve configuration."""

import os
import re
from pathlib import Path

import yaml

from .llm import DEFAULT_HELPER_MODEL, DEFAULT_MODEL

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
    config = _resolve_dict(raw)
    config["model"] = os.environ.get("OPENROUTER_MODEL") or config.get("model") or DEFAULT_MODEL
    config["helper_model"] = (
        os.environ.get("OPENROUTER_HELPER_MODEL")
        or config.get("helper_model")
        or DEFAULT_HELPER_MODEL
    )
    return config


# Group-chat activity tuning. These are the structural knobs behind how chatty
# the friends are. Live overrides go in DATA_DIR/activity.yaml (see
# get_activity_config); these defaults apply for any key left unset.
DEFAULT_ACTIVITY = {
    "initiation": {
        "check_min_minutes": 3,        # low end of the random "consider initiating" interval
        "check_max_minutes": 8,        # high end of that interval
        "silence_threshold_minutes": 5,  # only initiate after the chat's been quiet this long
        "dampener": 1.0,               # global multiplier on initiation chance (lower = calmer)
        "decay_floor": 0.5,            # initiation never decays below this while the human's away
        "decay_divisor_hours": 12.0,   # larger = slower decay as time-since-human grows
    },
    "reply": {
        "human_dampener": 0.9,         # multiplier on replying to a human (non-mention)
        "bot_dampener": 0.9,           # extra multiplier on replying to another bot
    },
}


def get_activity_config() -> dict:
    """Activity-tuning settings merged over DEFAULT_ACTIVITY.

    Read live from DATA_DIR/activity.yaml on every call. DATA_DIR is a *directory*
    bind-mount, so edits (even editors that save-by-replace) are picked up on the
    next loop — no restart, no rebuild. Missing file/section/keys fall back to the
    defaults, so it's safe when nothing is configured."""
    raw = {}
    activity_path = DATA_DIR / "activity.yaml"
    if activity_path.exists():
        with open(activity_path) as f:
            raw = yaml.safe_load(f) or {}
    merged = {}
    for section, defaults in DEFAULT_ACTIVITY.items():
        section_cfg = dict(defaults)
        override = raw.get(section) or {}
        if isinstance(override, dict):
            section_cfg.update({k: v for k, v in override.items() if k in defaults})
        merged[section] = section_cfg
    return merged


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


def load_history() -> str:
    """Load the shared HISTORY.md from friends dir."""
    path = FRIENDS_DIR / "HISTORY.md"
    if path.exists():
        return path.read_text()
    return ""


def get_friend_names() -> list[str]:
    """Return list of configured friend directory names."""
    names = []
    for d in sorted(FRIENDS_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / "SOUL.md").exists():
            names.append(d.name)
    return names
