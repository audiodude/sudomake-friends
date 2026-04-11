import os
from pathlib import Path


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
