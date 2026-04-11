"""Discovery and execution of migrations.

Responsibilities:
- Find migration directories and load their modules
- Track which migrations have been applied (~/.sudomake-friends/.migrations-applied)
- Back up the friends directory before each migration and offer restore on failure
- Drive the interactive flow in step 0 of the setup wizard
"""

import datetime as _dt
import importlib.util
import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable


MIGRATIONS_DIR = Path(__file__).resolve().parent


def _applied_tracker_path(home_dir: Path) -> Path:
    return home_dir / ".migrations-applied"


def _backups_dir(home_dir: Path) -> Path:
    return home_dir / ".backups"


def load_applied(home_dir: Path) -> set[str]:
    """Read applied migration IDs from the tracker file."""
    path = _applied_tracker_path(home_dir)
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: "<id>\t<iso-timestamp>" — take just the ID
        parts = line.split()
        if parts:
            ids.add(parts[0])
    return ids


def mark_applied(home_dir: Path, migration_id: str) -> None:
    """Append a migration ID to the tracker with a timestamp."""
    path = _applied_tracker_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    with open(path, "a") as f:
        f.write(f"{migration_id}\t{ts}\n")


def discover_migrations(migrations_dir: Path | None = None) -> list:
    """Load all migration modules in ID-sorted order."""
    if migrations_dir is None:
        migrations_dir = MIGRATIONS_DIR
    if not migrations_dir.exists():
        return []
    modules = []
    for child in sorted(migrations_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        migration_py = child / "migration.py"
        if not migration_py.exists():
            continue
        mod = _load_module(migration_py)
        if mod is not None:
            modules.append(mod)
    # Sort by ID (which starts with a timestamp)
    modules.sort(key=lambda m: getattr(m, "ID", ""))
    return modules


def _load_module(path: Path):
    """Load a migration.py as a Python module."""
    mod_name = f"sudomake_migration_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"  Failed to load migration {path.parent.name}: {e}")
        return None
    return mod


def backup_friends_dir(home_dir: Path, migration_id: str) -> Path:
    """Copy the friends directory to a timestamped backup location."""
    friends = home_dir / "friends"
    backups_root = _backups_dir(home_dir)
    backups_root.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backups_root / f"pre-{migration_id}-{ts}"
    if friends.exists():
        shutil.copytree(friends, dest)
    else:
        dest.mkdir(parents=True)
    return dest


def restore_friends_dir(home_dir: Path, backup_path: Path) -> None:
    """Replace the friends directory with a prior backup."""
    friends = home_dir / "friends"
    if friends.exists():
        shutil.rmtree(friends)
    shutil.copytree(backup_path, friends)


def check_and_run_pending(
    home_dir: Path,
    migrations_dir: Path | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> bool:
    """Step 0 entry point — check for pending migrations and run them interactively.

    Returns True if the wizard should continue, False if a mandatory migration
    was skipped or failed and the wizard must halt.
    """
    if input_fn is None:
        input_fn = input
    friends_dir = home_dir / "friends"
    applied = load_applied(home_dir)
    all_migrations = discover_migrations(migrations_dir)

    pending = []
    for mod in all_migrations:
        mig_id = getattr(mod, "ID", None)
        if not mig_id or mig_id in applied:
            continue
        try:
            if mod.is_needed(friends_dir):
                pending.append(mod)
        except Exception as e:
            print(f"  Migration {mig_id} is_needed() raised: {e}")

    if not pending:
        return True

    print()
    print("  ┌" + "─" * 60 + "┐")
    print("  │  Pending migrations detected" + " " * 31 + "│")
    print("  └" + "─" * 60 + "┘")
    print()
    print(f"  {len(pending)} migration(s) want to run:")
    for mod in pending:
        tag = " [MANDATORY]" if getattr(mod, "MANDATORY", False) else ""
        print(f"    • {mod.ID}: {getattr(mod, 'TITLE', '(no title)')}{tag}")
    print()
    print("  Your friends directory will be backed up before each migration.")
    print("  Backups are kept at ~/.sudomake-friends/.backups/ forever.")
    print()
    answer = input_fn("  Run these migrations now? [Y/n]: ").strip().lower()
    if answer == "n":
        blockers = [m for m in pending if getattr(m, "MANDATORY", False)]
        if blockers:
            print()
            print("  Cannot skip — the following migrations are mandatory:")
            for m in blockers:
                print(f"    • {m.ID}: {getattr(m, 'TITLE', '')}")
            print("  Halting wizard. Re-run when you're ready to apply them.")
            return False
        print("  Skipping optional migrations.")
        return True

    all_ok = True
    for mod in pending:
        ok = _run_one(home_dir, mod, input_fn)
        if not ok:
            all_ok = False
            if getattr(mod, "MANDATORY", False):
                print()
                print(f"  Mandatory migration {mod.ID} did not complete. Halting wizard.")
                return False
            answer = input_fn("  Continue with remaining migrations? [y/N]: ").strip().lower()
            if answer != "y":
                return True
    return all_ok or True


def _run_one(home_dir: Path, mod, input_fn: Callable[[str], str]) -> bool:
    """Run a single migration inside a try/except with backup + restore support."""
    friends_dir = home_dir / "friends"
    mig_id = mod.ID
    title = getattr(mod, "TITLE", "(untitled)")
    description = getattr(mod, "DESCRIPTION", "").strip()

    print()
    print(f"  = Migration {mig_id}: {title}")
    print("  " + "─" * 60)
    if description:
        for line in description.splitlines():
            print(f"  {line}")
        print()

    print("  Backing up friends directory...")
    backup_path = backup_friends_dir(home_dir, mig_id)
    print(f"  Backup saved to: {backup_path}")
    print()

    try:
        ok = mod.run(friends_dir, interactive=True)
    except Exception:
        print()
        print(f"  Migration raised an exception:")
        traceback.print_exc()
        ok = False

    if ok:
        mark_applied(home_dir, mig_id)
        print()
        print(f"  ✓ Migration {mig_id} applied.")
        return True

    print()
    print(f"  ✗ Migration {mig_id} did NOT complete successfully.")
    answer = input_fn("  Restore friends directory from backup? [Y/n]: ").strip().lower()
    if answer != "n":
        restore_friends_dir(home_dir, backup_path)
        print("  Restored from backup.")
    else:
        print("  Leaving friends directory in its current state.")
        print(f"  Backup remains at: {backup_path}")
    return False
