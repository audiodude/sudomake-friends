"""Tests for the migration runner: discovery, applied tracking, backup/restore."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from wizard.migrations import runner


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def home_dir(tmp_path):
    home = tmp_path / "sudomake-friends"
    home.mkdir()
    (home / "friends").mkdir()
    return home


@pytest.fixture
def with_friends(home_dir):
    """Home dir pre-populated with two synthetic friends."""
    for name in ("alex", "casey"):
        fdir = home_dir / "friends" / name
        fdir.mkdir()
        (fdir / "config.yaml").write_text(yaml.dump({"chattiness": 0.5, "timezone": "UTC"}))
        (fdir / "candidate.json").write_text(json.dumps({"name": name, "chattiness": 0.5}))
        (fdir / "SOUL.md").write_text(f"# {name}\n")
    return home_dir


def _write_migration(migrations_dir: Path, mig_id: str, body: str = ""):
    mdir = migrations_dir / mig_id
    mdir.mkdir()
    default_body = f"""
ID = "{mig_id}"
TITLE = "Test migration {mig_id}"
MANDATORY = False
DESCRIPTION = "A test migration."
def is_needed(friends_dir): return True
def run(friends_dir, interactive=True): return True
"""
    (mdir / "migration.py").write_text(body or default_body)
    return mdir


# ─── Applied tracker ─────────────────────────────────────────────────────────

class TestAppliedTracker:
    def test_load_applied_empty_when_missing(self, home_dir):
        assert runner.load_applied(home_dir) == set()

    def test_load_applied_skips_comments(self, home_dir):
        (home_dir / ".migrations-applied").write_text(
            "# comment line\nmig-a\t2026-01-01T00:00:00\nmig-b\t2026-01-02T00:00:00\n"
        )
        assert runner.load_applied(home_dir) == {"mig-a", "mig-b"}

    def test_mark_applied_appends(self, home_dir):
        runner.mark_applied(home_dir, "mig-a")
        runner.mark_applied(home_dir, "mig-b")
        applied = runner.load_applied(home_dir)
        assert applied == {"mig-a", "mig-b"}

    def test_mark_applied_creates_file(self, home_dir):
        path = home_dir / ".migrations-applied"
        assert not path.exists()
        runner.mark_applied(home_dir, "mig-x")
        assert path.exists()


# ─── Discovery ───────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_empty_dir_returns_empty(self, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        assert runner.discover_migrations(mdir) == []

    def test_finds_and_sorts_migrations(self, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(mdir, "2026-02-01_second")
        _write_migration(mdir, "2026-01-01_first")
        _write_migration(mdir, "2026-03-01_third")
        mods = runner.discover_migrations(mdir)
        assert [m.ID for m in mods] == [
            "2026-01-01_first",
            "2026-02-01_second",
            "2026-03-01_third",
        ]

    def test_skips_hidden_dirs(self, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(mdir, "2026-01-01_good")
        _write_migration(mdir, "_template")
        _write_migration(mdir, ".hidden")
        mods = runner.discover_migrations(mdir)
        assert [m.ID for m in mods] == ["2026-01-01_good"]

    def test_skips_dirs_without_migration_py(self, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        (mdir / "notamigration").mkdir()
        _write_migration(mdir, "2026-01-01_real")
        mods = runner.discover_migrations(mdir)
        assert len(mods) == 1


# ─── Backup & restore ────────────────────────────────────────────────────────

class TestBackup:
    def test_backup_copies_friends_dir(self, with_friends):
        backup = runner.backup_friends_dir(with_friends, "test-mig")
        assert backup.exists()
        assert (backup / "alex" / "config.yaml").exists()
        assert (backup / "casey" / "candidate.json").exists()

    def test_backup_unique_per_call(self, with_friends):
        import time
        b1 = runner.backup_friends_dir(with_friends, "test-mig")
        time.sleep(1.1)
        b2 = runner.backup_friends_dir(with_friends, "test-mig")
        assert b1 != b2
        assert b1.exists() and b2.exists()

    def test_restore_replaces_friends_dir(self, with_friends):
        backup = runner.backup_friends_dir(with_friends, "test-mig")
        # Corrupt the friends dir
        (with_friends / "friends" / "alex" / "config.yaml").write_text("corrupted")
        runner.restore_friends_dir(with_friends, backup)
        cfg = yaml.safe_load((with_friends / "friends" / "alex" / "config.yaml").read_text())
        assert cfg["chattiness"] == 0.5


# ─── Full run flow ───────────────────────────────────────────────────────────

class TestCheckAndRunPending:
    def test_no_pending_returns_true(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        assert runner.check_and_run_pending(with_friends, mdir) is True

    def test_skips_already_applied(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(mdir, "2026-01-01_done")
        runner.mark_applied(with_friends, "2026-01-01_done")
        # No input expected since nothing is pending
        assert runner.check_and_run_pending(with_friends, mdir, input_fn=lambda _: "") is True

    def test_skips_when_is_needed_false(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(
            mdir, "2026-01-01_notneeded",
            body='ID = "2026-01-01_notneeded"\nTITLE = "x"\nMANDATORY = False\nDESCRIPTION = ""\n'
                 'def is_needed(fd): return False\ndef run(fd, interactive=True): return True\n'
        )
        assert runner.check_and_run_pending(with_friends, mdir, input_fn=lambda _: "") is True
        assert runner.load_applied(with_friends) == set()

    def test_user_declines_optional(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(mdir, "2026-01-01_opt")
        inputs = iter(["n"])
        assert runner.check_and_run_pending(
            with_friends, mdir, input_fn=lambda _: next(inputs)
        ) is True
        assert runner.load_applied(with_friends) == set()

    def test_user_declines_mandatory_halts(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(
            mdir, "2026-01-01_req",
            body='ID = "2026-01-01_req"\nTITLE = "x"\nMANDATORY = True\nDESCRIPTION = ""\n'
                 'def is_needed(fd): return True\ndef run(fd, interactive=True): return True\n'
        )
        inputs = iter(["n"])
        result = runner.check_and_run_pending(
            with_friends, mdir, input_fn=lambda _: next(inputs)
        )
        assert result is False

    def test_successful_run_marks_applied(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(mdir, "2026-01-01_good")
        inputs = iter(["y"])  # "run these now?"
        result = runner.check_and_run_pending(
            with_friends, mdir, input_fn=lambda _: next(inputs)
        )
        assert result is True
        assert "2026-01-01_good" in runner.load_applied(with_friends)

    def test_exception_triggers_restore_prompt(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(
            mdir, "2026-01-01_broken",
            body='ID = "2026-01-01_broken"\nTITLE = "x"\nMANDATORY = False\nDESCRIPTION = ""\n'
                 'def is_needed(fd): return True\n'
                 'def run(fd, interactive=True):\n    raise RuntimeError("boom")\n'
        )
        inputs = iter(["y", "y", "n"])  # run? → yes; restore? → yes; continue? → no
        result = runner.check_and_run_pending(
            with_friends, mdir, input_fn=lambda _: next(inputs)
        )
        # Failed but not mandatory → wizard can continue
        assert result is True
        assert "2026-01-01_broken" not in runner.load_applied(with_friends)

    def test_mandatory_failure_halts(self, with_friends, tmp_path):
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        _write_migration(
            mdir, "2026-01-01_critical",
            body='ID = "2026-01-01_critical"\nTITLE = "x"\nMANDATORY = True\nDESCRIPTION = ""\n'
                 'def is_needed(fd): return True\n'
                 'def run(fd, interactive=True): return False\n'
        )
        inputs = iter(["y", "y"])  # run? → yes; restore? → yes
        result = runner.check_and_run_pending(
            with_friends, mdir, input_fn=lambda _: next(inputs)
        )
        assert result is False
