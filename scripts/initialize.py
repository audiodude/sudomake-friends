#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Sudomake Friends bootstrap — locate or clone the repo, then hand off to the wizard.

This script is the `uv run <url>` entry point. It has no third-party dependencies
so `uv run` can spin it up instantly against any Python. Its only job is to:

  1. Find a working repo checkout (dev tree or cached clone at ~/.sudomake-friends/.src-cache)
  2. Offer to `git pull --ff-only` on the cached clone (dev trees are left alone)
  3. Exec `uv run --project <repo> python -m wizard` so the real wizard runs in the
     project's own uv-managed venv — never installs anything into system Python.
"""

import os
import subprocess
import sys
from pathlib import Path


REPO_URL = "https://github.com/audiodude/sudomake-friends.git"
HOME_DIR = Path.home() / ".sudomake-friends"
SRC_CACHE_DIR = HOME_DIR / ".src-cache"


def _find_local_repo_root() -> "Path | None":
    """If this script is running from a checked-out repo, return its root. Else None."""
    try:
        script_path = Path(__file__).resolve()
    except NameError:
        return None
    candidate = script_path.parent.parent
    if (candidate / "scripts" / "wizard").exists() or (candidate / ".git").exists():
        return candidate
    return None


def ensure_src_cache() -> "Path":
    """Return a path to a working repo checkout.

    Prefers a local dev checkout (if this script is running from one). Falls back
    to cloning the repo to ~/.sudomake-friends/.src-cache/. Raises on failure.
    """
    local = _find_local_repo_root()
    if local:
        return local
    SRC_CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    if not SRC_CACHE_DIR.exists():
        print("  Fetching sudomake-friends source code...")
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", REPO_URL, str(SRC_CACHE_DIR)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
            print(f"  git clone failed: {stderr}")
            raise
    return SRC_CACHE_DIR


def check_for_updates(repo_root: "Path") -> None:
    """Offer to pull latest code. Skipped for dev checkouts."""
    if repo_root != SRC_CACHE_DIR:
        return
    print()
    print("  Check for updates?")
    print("  Pulls the latest code from GitHub. Updates may include new features,")
    print("  bug fixes, or data migrations that modify your friends directory.")
    print("  (Any migration will prompt before touching your data.)")
    answer = input("  Check for updates now? [Y/n]: ").strip().lower()
    if answer == "n":
        return
    print("  Pulling latest...")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "pull", "--ff-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        out = (result.stdout or "").strip()
        if "Already up to date" in out or "Already up-to-date" in out:
            print("  Already up to date.")
        else:
            print(f"  Updated.\n{out}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        print(f"  git pull failed: {stderr}")
        print("  Continuing with current code.")


def _exec_wizard(repo_root: "Path") -> None:
    """Hand off to the wizard package under the repo's uv-managed venv."""
    scripts_dir = repo_root / "scripts"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(scripts_dir) + os.pathsep + env.get("PYTHONPATH", "")
    wizard_args = [a for a in sys.argv[1:]]
    cmd = [
        "uv", "run",
        "--project", str(repo_root),
        "python", "-m", "wizard",
        *wizard_args,
    ]
    os.execvpe("uv", cmd, env)


def main() -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    repo_root = ensure_src_cache()
    check_for_updates(repo_root)
    _exec_wizard(repo_root)


if __name__ == "__main__":
    main()
