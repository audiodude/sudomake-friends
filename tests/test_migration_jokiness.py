"""Tests for the 2026-04-10_add_jokiness_whininess template migration."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Add scripts/ to the path so `wizard.migrations` is importable
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))


def _load_migration():
    path = _REPO / "scripts" / "wizard" / "migrations" / "2026-04-10_add_jokiness_whininess" / "migration.py"
    spec = importlib.util.spec_from_file_location("jokiness_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


migration = _load_migration()


@pytest.fixture
def friends_dir(tmp_path):
    fd = tmp_path / "friends"
    fd.mkdir()
    for name, chat in [("alex", 0.3), ("casey", 0.5)]:
        p = fd / name
        p.mkdir()
        (p / "config.yaml").write_text(yaml.dump({
            "chattiness": chat, "timezone": "UTC",
            "schedule": {"wake_up": "08:00"},
        }))
        (p / "candidate.json").write_text(json.dumps({
            "name": name.capitalize(), "chattiness": chat, "traits": ["brave"],
        }))
        (p / "SOUL.md").write_text(f"# {name}\nSarcastic and witty")
    return fd


@pytest.fixture
def friends_already_migrated(tmp_path):
    fd = tmp_path / "friends"
    fd.mkdir()
    p = fd / "alex"
    p.mkdir()
    (p / "config.yaml").write_text(yaml.dump({
        "chattiness": 0.5, "jokiness": 0.6, "whininess": 0.4,
    }))
    (p / "candidate.json").write_text(json.dumps({
        "name": "Alex", "chattiness": 0.5, "jokiness": 0.6, "whininess": 0.4,
    }))
    (p / "SOUL.md").write_text("# alex")
    return fd


class TestIsNeeded:
    def test_true_when_fields_missing(self, friends_dir):
        assert migration.is_needed(friends_dir) is True

    def test_false_when_all_present(self, friends_already_migrated):
        assert migration.is_needed(friends_already_migrated) is False

    def test_true_when_one_friend_missing(self, friends_already_migrated):
        # Add a new friend without the fields
        new = friends_already_migrated / "newbie"
        new.mkdir()
        (new / "config.yaml").write_text(yaml.dump({"chattiness": 0.5}))
        (new / "candidate.json").write_text("{}")
        assert migration.is_needed(friends_already_migrated) is True


class TestRunDefaultsMode:
    def test_applies_defaults_via_input_patch(self, friends_dir):
        with patch("builtins.input", side_effect=["defaults"]):
            assert migration.run(friends_dir, interactive=True) is True
        for name in ("alex", "casey"):
            cfg = yaml.safe_load((friends_dir / name / "config.yaml").read_text())
            cand = json.loads((friends_dir / name / "candidate.json").read_text())
            assert cfg["jokiness"] == migration.DEFAULT_JOKINESS
            assert cfg["whininess"] == migration.DEFAULT_WHININESS
            assert cand["jokiness"] == migration.DEFAULT_JOKINESS
            assert cand["whininess"] == migration.DEFAULT_WHININESS

    def test_defaults_preserves_other_config(self, friends_dir):
        with patch("builtins.input", side_effect=["defaults"]):
            migration.run(friends_dir, interactive=True)
        cfg = yaml.safe_load((friends_dir / "alex" / "config.yaml").read_text())
        assert cfg["chattiness"] == 0.3
        assert cfg["timezone"] == "UTC"
        assert cfg["schedule"]["wake_up"] == "08:00"


class TestRunManualMode:
    def test_prompts_per_friend(self, friends_dir):
        # manual mode, then (jokiness, whininess) for alex, then casey
        inputs = iter(["manual", "0.8", "0.2", "0.3", "0.7"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            assert migration.run(friends_dir, interactive=True) is True
        alex = yaml.safe_load((friends_dir / "alex" / "config.yaml").read_text())
        casey = yaml.safe_load((friends_dir / "casey" / "config.yaml").read_text())
        assert alex["jokiness"] == 0.8
        assert alex["whininess"] == 0.2
        assert casey["jokiness"] == 0.3
        assert casey["whininess"] == 0.7


class TestRunLLMMode:
    def test_llm_accept_all(self, friends_dir, monkeypatch):
        # Mock anthropic to return tuned values
        fake_response = MagicMock()
        fake_response.content = [MagicMock(text='{"jokiness": 0.7, "whininess": 0.4, "reasoning": "sarcastic"}')]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        fake_module = MagicMock()
        fake_module.Anthropic.return_value = fake_client
        monkeypatch.setitem(sys.modules, "anthropic", fake_module)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # llm mode, then accept all
        with patch("builtins.input", side_effect=["llm", "a"]):
            assert migration.run(friends_dir, interactive=True) is True

        for name in ("alex", "casey"):
            cfg = yaml.safe_load((friends_dir / name / "config.yaml").read_text())
            assert cfg["jokiness"] == 0.7
            assert cfg["whininess"] == 0.4

    def test_llm_without_api_key_falls_back_to_defaults(self, friends_dir, monkeypatch):
        fake_module = MagicMock()
        monkeypatch.setitem(sys.modules, "anthropic", fake_module)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Ensure no .env file is picked up
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent_home_dir_xyz"))

        with patch("builtins.input", side_effect=["llm"]):
            assert migration.run(friends_dir, interactive=True) is True

        cfg = yaml.safe_load((friends_dir / "alex" / "config.yaml").read_text())
        assert cfg["jokiness"] == migration.DEFAULT_JOKINESS
        assert cfg["whininess"] == migration.DEFAULT_WHININESS


class TestNonInteractive:
    def test_applies_defaults(self, friends_dir):
        assert migration.run(friends_dir, interactive=False) is True
        for name in ("alex", "casey"):
            cfg = yaml.safe_load((friends_dir / name / "config.yaml").read_text())
            assert cfg["jokiness"] == migration.DEFAULT_JOKINESS
            assert cfg["whininess"] == migration.DEFAULT_WHININESS


class TestIdempotent:
    def test_second_run_is_noop(self, friends_dir):
        # First run: apply defaults
        assert migration.run(friends_dir, interactive=False) is True
        # Second run: is_needed should now be False
        assert migration.is_needed(friends_dir) is False
