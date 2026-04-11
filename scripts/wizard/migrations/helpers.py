"""Shared utilities that individual migrations can import.

Keep this module small and stable — migrations pin against these names.
"""

import json
from pathlib import Path
from typing import Callable, Iterator

import yaml


# ─── Friend iteration ─────────────────────────────────────────────────────────

def iter_friends(friends_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield (name, path) for each friend directory. Skips files and hidden dirs."""
    if not friends_dir.exists():
        return
    for child in sorted(friends_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        yield child.name, child


def load_friend_config(friend_path: Path) -> dict:
    cfg = friend_path / "config.yaml"
    if not cfg.exists():
        return {}
    return yaml.safe_load(cfg.read_text()) or {}


def save_friend_config(friend_path: Path, config: dict) -> None:
    cfg = friend_path / "config.yaml"
    cfg.write_text(yaml.dump(config, default_flow_style=False, sort_keys=True))


def load_friend_candidate(friend_path: Path) -> dict:
    cand = friend_path / "candidate.json"
    if not cand.exists():
        return {}
    return json.loads(cand.read_text())


def save_friend_candidate(friend_path: Path, candidate: dict) -> None:
    cand = friend_path / "candidate.json"
    cand.write_text(json.dumps(candidate, indent=2) + "\n")


def load_friend_soul(friend_path: Path) -> str:
    soul = friend_path / "SOUL.md"
    if not soul.exists():
        return ""
    return soul.read_text()


def update_config(friend_path: Path, updates: dict) -> None:
    """Patch config.yaml with new keys, preserving everything else."""
    cfg = load_friend_config(friend_path)
    cfg.update(updates)
    save_friend_config(friend_path, cfg)


def update_candidate(friend_path: Path, updates: dict) -> None:
    """Patch candidate.json with new keys, preserving everything else."""
    cand = load_friend_candidate(friend_path)
    cand.update(updates)
    save_friend_candidate(friend_path, cand)


# ─── Application strategies ──────────────────────────────────────────────────

def apply_to_all(
    friends_dir: Path,
    fn: Callable[[str, Path], dict],
) -> dict[str, dict]:
    """Run fn(name, path) for every friend, returning {name: result_dict}.

    fn is expected to return a dict of updates to apply (or an empty dict to skip).
    This helper does NOT write anything — it just collects. Migrations decide
    what to do with the results (e.g. write to config, show a preview, etc).
    """
    results = {}
    for name, path in iter_friends(friends_dir):
        results[name] = fn(name, path) or {}
    return results


def choose_one_by_one(
    friends_dir: Path,
    fn: Callable[[str, Path], dict],
    input_fn: Callable[[str], str] | None = None,
) -> dict[str, dict]:
    """Walk through friends one at a time, letting the user accept/skip each."""
    if input_fn is None:
        input_fn = input
    results = {}
    for name, path in iter_friends(friends_dir):
        print(f"\n  — {name} —")
        proposed = fn(name, path) or {}
        if not proposed:
            print("  (no changes proposed)")
            continue
        for k, v in proposed.items():
            print(f"    {k}: {v}")
        answer = input_fn("  Apply to this friend? [Y/n/e(dit)]: ").strip().lower()
        if answer == "n":
            print("  Skipped.")
            continue
        if answer == "e":
            edited = {}
            for k, v in proposed.items():
                new = input_fn(f"    {k} [{v}]: ").strip()
                edited[k] = new if new else v
            results[name] = edited
        else:
            results[name] = proposed
    return results


# ─── Interactive prompts ─────────────────────────────────────────────────────

def prompt_choice(
    question: str,
    options: list[str],
    default: str | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> str:
    """Prompt for a choice from a list of options. Returns the chosen option verbatim."""
    if input_fn is None:
        input_fn = input
    opts_display = "/".join(
        o.upper() if o == default else o for o in options
    )
    while True:
        answer = input_fn(f"  {question} [{opts_display}]: ").strip().lower()
        if not answer and default is not None:
            return default
        for opt in options:
            if answer == opt.lower() or (len(answer) == 1 and opt.lower().startswith(answer)):
                return opt
        print(f"  Please enter one of: {', '.join(options)}")


def prompt_float(
    question: str,
    default: float,
    minimum: float = 0.0,
    maximum: float = 1.0,
    input_fn: Callable[[str], str] | None = None,
) -> float:
    """Prompt for a float in [minimum, maximum]."""
    if input_fn is None:
        input_fn = input
    while True:
        raw = input_fn(f"  {question} [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = float(raw)
        except ValueError:
            print(f"  Not a number: {raw}")
            continue
        if val < minimum or val > maximum:
            print(f"  Must be between {minimum} and {maximum}")
            continue
        return val
