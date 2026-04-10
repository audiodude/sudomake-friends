"""Tests for the step -1 bootstrap + update check in initialize.py."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import initialize as lib


class TestFindLocalRepoRoot:
    def test_finds_repo_when_scripts_migrations_exists(self, tmp_path):
        repo = tmp_path / "fake-repo"
        (repo / "scripts" / "migrations").mkdir(parents=True)
        script = repo / "scripts" / "initialize.py"
        script.write_text("")
        with patch.object(lib, "__file__", str(script)):
            assert lib._find_local_repo_root() == repo

    def test_finds_repo_when_git_dir_exists(self, tmp_path):
        repo = tmp_path / "fake-repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "scripts").mkdir()
        script = repo / "scripts" / "initialize.py"
        script.write_text("")
        with patch.object(lib, "__file__", str(script)):
            assert lib._find_local_repo_root() == repo

    def test_returns_none_when_no_markers(self, tmp_path):
        lonely = tmp_path / "lonely"
        (lonely / "scripts").mkdir(parents=True)
        script = lonely / "scripts" / "initialize.py"
        script.write_text("")
        with patch.object(lib, "__file__", str(script)):
            assert lib._find_local_repo_root() is None


class TestEnsureSrcCache:
    def test_returns_local_repo_when_available(self, tmp_path):
        fake_local = tmp_path / "dev-checkout"
        (fake_local / "scripts" / "migrations").mkdir(parents=True)
        with patch.object(lib, "_find_local_repo_root", return_value=fake_local):
            with patch("subprocess.run") as mock_run:
                result = lib.ensure_src_cache()
        assert result == fake_local
        mock_run.assert_not_called()

    def test_clones_when_cache_missing(self, tmp_path):
        cache = tmp_path / "cache"
        with patch.object(lib, "_find_local_repo_root", return_value=None):
            with patch.object(lib, "SRC_CACHE_DIR", cache):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    # git clone would create the dir; simulate by touching after the call
                    def fake_run(*args, **kwargs):
                        cache.mkdir(parents=True, exist_ok=True)
                        return MagicMock(returncode=0)
                    mock_run.side_effect = fake_run
                    result = lib.ensure_src_cache()
        assert result == cache
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "git"
        assert call_args[1] == "clone"
        assert str(cache) in call_args

    def test_skips_clone_when_cache_present(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch.object(lib, "_find_local_repo_root", return_value=None):
            with patch.object(lib, "SRC_CACHE_DIR", cache):
                with patch("subprocess.run") as mock_run:
                    result = lib.ensure_src_cache()
        assert result == cache
        mock_run.assert_not_called()

    def test_clone_failure_raises(self, tmp_path):
        cache = tmp_path / "cache"
        with patch.object(lib, "_find_local_repo_root", return_value=None):
            with patch.object(lib, "SRC_CACHE_DIR", cache):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.CalledProcessError(
                        1, ["git", "clone"], stderr=b"network error"
                    )
                    with pytest.raises(subprocess.CalledProcessError):
                        lib.ensure_src_cache()


class TestCheckForUpdates:
    def test_skips_dev_checkout(self, tmp_path):
        dev = tmp_path / "dev"
        dev.mkdir()
        with patch.object(lib, "SRC_CACHE_DIR", tmp_path / "cache"):
            with patch("builtins.input") as mock_input:
                with patch("subprocess.run") as mock_run:
                    lib.check_for_updates(dev)
        mock_input.assert_not_called()
        mock_run.assert_not_called()

    def test_user_declines_update(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch.object(lib, "SRC_CACHE_DIR", cache):
            with patch("builtins.input", side_effect=["n"]):
                with patch("subprocess.run") as mock_run:
                    lib.check_for_updates(cache)
        mock_run.assert_not_called()

    def test_user_accepts_and_pull_succeeds(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch.object(lib, "SRC_CACHE_DIR", cache):
            with patch("builtins.input", side_effect=["y"]):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(stdout="Already up to date.", returncode=0)
                    lib.check_for_updates(cache)
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args[0]
        assert "pull" in call_args

    def test_pull_failure_is_graceful(self, tmp_path, capsys):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch.object(lib, "SRC_CACHE_DIR", cache):
            with patch("builtins.input", side_effect=["y"]):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.CalledProcessError(
                        1, ["git", "pull"], stderr="conflict"
                    )
                    # Should not raise
                    lib.check_for_updates(cache)
        captured = capsys.readouterr()
        assert "failed" in captured.out.lower()
        assert "continuing" in captured.out.lower()

    def test_empty_input_treated_as_yes(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch.object(lib, "SRC_CACHE_DIR", cache):
            with patch("builtins.input", side_effect=[""]):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(stdout="Already up to date.", returncode=0)
                    lib.check_for_updates(cache)
        assert mock_run.called
