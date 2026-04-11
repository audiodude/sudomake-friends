import shutil
import subprocess
import sys
from pathlib import Path

import wizard.checkpoint as _checkpoint_mod
from wizard.checkpoint import load_checkpoint, save_checkpoint, clear_checkpoint
from wizard.friends import get_existing_friend_names
from wizard.paths import get_paths
from wizard.steps import (
    step_anthropic_key,
    step_user_profile,
    step_select_friends,
    step_telegram_bots,
    step_telegram_group,
    step_history,
    step_deploy,
    step_done,
)

HOME_DIR = Path.home() / ".sudomake-friends"

STEPS = {
    "start": step_anthropic_key,
    "anthropic_key": step_anthropic_key,
    "user_profile": step_user_profile,
    "select_friends": step_select_friends,
    "telegram_bots": step_telegram_bots,
    "telegram_group": step_telegram_group,
    "history": step_history,
    "deploy": step_deploy,
    "done": step_done,
}


def _offer_delete_docker_volume():
    """Check for Docker volume with old data and offer to delete it."""
    try:
        r = subprocess.run(
            ["docker", "volume", "ls", "--filter", "name=friend-group_friend-data",
             "--format", "{{.Name}}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and "friend-data" in r.stdout:
            print("\n  Docker volume with old chat/memory data found.")
            delete = input("  Delete it? (recommended for fresh start) [y/N]: ").strip().lower()
            if delete == "y":
                # Must stop container first or volume rm fails
                subprocess.run(
                    ["docker", "compose", "down"],
                    capture_output=True, timeout=30,
                )
                r2 = subprocess.run(
                    ["docker", "volume", "rm", "friend-group_friend-data"],
                    capture_output=True, text=True, timeout=10,
                )
                if r2.returncode == 0:
                    print("  Deleted.")
                else:
                    print(f"  Could not delete: {r2.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Docker not installed or not responding


def main():
    # Handle --start-over
    if "--start-over" in sys.argv:
        _offer_delete_docker_volume()
        if HOME_DIR.exists():
            shutil.rmtree(HOME_DIR)
        print("  Cleared all setup state. Starting fresh.\n")
        sys.argv.remove("--start-over")

    print()
    title = "Sudomake Friends -- Initialize"
    inner = f"   {title}   "
    print(f"  +{'=' * (len(inner) + 2)}+")
    print(f"  ||{inner}||")
    print(f"  +{'=' * (len(inner) + 2)}+")

    HOME_DIR.mkdir(parents=True, exist_ok=True)

    root = HOME_DIR
    paths = get_paths(root)
    paths["friends"].mkdir(parents=True, exist_ok=True)
    checkpoint_path = root / ".init-checkpoint.json"
    _checkpoint_mod.CHECKPOINT_PATH = checkpoint_path

    print(f"  Data directory: {root}")

    # Step 0: run any pending migrations
    try:
        from wizard.migrations import runner as _mig_runner
        if not _mig_runner.check_and_run_pending(root):
            print()
            print("  Wizard halted due to pending mandatory migration.")
            sys.exit(1)
    except ImportError as e:
        print(f"  (Migrations system unavailable: {e})")

    cp = load_checkpoint()

    # Detect completed setup: .env exists and friends exist
    existing_friends = get_existing_friend_names(paths["friends"])
    setup_complete = paths["env"].exists() and len(existing_friends) > 0

    if setup_complete:
        print(f"\n  Found {len(existing_friends)} friend(s): {', '.join(existing_friends)}")
        print()
        has_incomplete = cp.get("step") and cp["step"] not in ("start", "done", "deploy")
        if has_incomplete:
            print(f"  (In-progress setup at step: {cp['step']})")
            choice = input("  [r]esume, [a]djust, [s]tart over, or [d]eploy? [r/a/s/d]: ").strip().lower()
        else:
            choice = input("  [a]djust, [s]tart over, or [d]eploy? [a/s/d]: ").strip().lower()
        if choice == "s":
            _offer_delete_docker_volume()
            clear_checkpoint()
            cp = {"step": "start"}
        elif choice == "a":
            _offer_delete_docker_volume()
            # Walk through all steps, each will offer to keep or redo
            cp["step"] = "start"
        elif choice == "r" and has_incomplete:
            pass  # continue from current checkpoint step
        else:
            cp["step"] = "deploy"

    elif cp["step"] != "start":
        print(f"\n  In-progress setup at step: {cp['step']}")
        choice = input("  [r]esume, or [s]tart over? [r/s]: ").strip().lower()
        if choice == "s":
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
