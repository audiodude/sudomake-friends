import os
from pathlib import Path

from dotenv import dotenv_values, set_key


def get_paths(root: Path) -> dict:
    return {
        "root": root,
        "friends": root / "friends",
        "env": root / ".env",
        "scrape_cache": root / ".scrape-cache",
    }


def load_env(env_path: Path) -> dict:
    return {
        key: value
        for key, value in dotenv_values(env_path, interpolate=False).items()
        if value is not None
    }


def save_env(env_path: Path, env: dict):
    lines = [f"{k}={v}" for k, v in env.items()]
    env_path.write_text("\n".join(lines) + "\n")


def set_env_var(env_path: Path, key: str, value: str):
    set_key(env_path, key, value, quote_mode="never")
    os.environ[key] = value
