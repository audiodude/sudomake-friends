"""Credential upgrades preserve setup progress and never expose saved secrets."""

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wizard.checkpoint as checkpoint
from wizard.llm import get_client
from wizard.paths import get_paths, load_env, set_env_var
from wizard.steps import ensure_openrouter_key, step_openrouter_key

wizard_main = importlib.import_module("wizard.main")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # set_env_var writes os.environ too, so restore the entire environment after use.
    with patch.dict(os.environ, os.environ.copy(), clear=True):
        for name in ("OPENROUTER_API_KEY", "OPENROUTER_GENERATION_MODEL", "OPENROUTER_HELPER_MODEL"):
            monkeypatch.delenv(name, raising=False)
        yield


@pytest.fixture
def paths(tmp_path, monkeypatch):
    paths = get_paths(tmp_path)
    paths["friends"].mkdir()
    monkeypatch.setattr(checkpoint, "CHECKPOINT_PATH", tmp_path / ".init-checkpoint.json")
    return paths


def test_env_key_reuse_persists_without_secret_output(paths, monkeypatch, capsys):
    key = "sk-or-private-test-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    paths["env"].write_text("ANTHROPIC_API_KEY='sk-ant-keep-me'\nOTHER='value with spaces'\n")
    with patch("builtins.input", return_value="y"), patch("wizard.steps.getpass") as secret_input:
        result = step_openrouter_key({"step": "openrouter_key"}, paths)
    assert result["step"] == "user_profile"
    assert load_env(paths["env"]) == {
        "OPENROUTER_API_KEY": key,
        "ANTHROPIC_API_KEY": "sk-ant-keep-me",
        "OTHER": "value with spaces",
    }
    secret_input.assert_not_called()
    assert key not in capsys.readouterr().out


@pytest.mark.parametrize("destination", ["user_profile", "select_friends", "history", "deploy"])
def test_upgrade_preserves_resume_destination_and_cached_data(paths, destination, capsys):
    paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-old\n")
    cp = {"step": destination, "user_context": "Saved profile", "candidates": [{"name": "Sam"}],
          "held_indices": [0], "souls": {"Sam": "# Already generated"}}
    expected = json.loads(json.dumps(cp))
    with patch("wizard.steps.getpass", return_value="sk-or-new-private"):
        result = ensure_openrouter_key(cp, paths)
    assert result == expected
    assert json.loads(checkpoint.CHECKPOINT_PATH.read_text()) == expected
    assert load_env(paths["env"])["ANTHROPIC_API_KEY"] == "sk-ant-old"
    assert load_env(paths["env"])["OPENROUTER_API_KEY"] == "sk-or-new-private"
    assert "sk-or-new-private" not in capsys.readouterr().out


def test_quitting_credential_upgrade_resumes_same_destination(paths):
    cp = {"step": "select_friends", "candidates": [{"name": "Sam"}], "held_indices": [0]}
    with patch("wizard.steps.getpass", return_value="q"), pytest.raises(SystemExit):
        ensure_openrouter_key(cp, paths)
    saved = checkpoint.load_checkpoint()
    assert saved["step"] == "openrouter_key"
    assert saved["resume_step"] == "select_friends"
    with patch("wizard.steps.getpass", return_value="sk-or-new"):
        resumed = step_openrouter_key(saved, paths)
    assert resumed == {"step": "select_friends", "candidates": [{"name": "Sam"}], "held_indices": [0]}


@pytest.mark.parametrize("old_step", ["anthropic_key", "history"])
def test_legacy_checkpoint_discards_embedded_credential(paths, old_step):
    checkpoint.CHECKPOINT_PATH.write_text(json.dumps({
        "step": old_step, "anthropic_key": "sk-ant-secret", "user_context": "Saved profile",
    }))
    loaded = checkpoint.load_checkpoint()
    assert loaded == {
        "step": "openrouter_key" if old_step == "anthropic_key" else "history",
        "user_context": "Saved profile",
    }
    assert "sk-ant-secret" not in checkpoint.CHECKPOINT_PATH.read_text()


def test_quoted_credentials_and_model_overrides_are_loaded(paths):
    paths["env"].write_text(
        'OPENROUTER_API_KEY="sk-or-quoted"\n'
        'OPENROUTER_GENERATION_MODEL="anthropic/claude-opus-4.8"\n'
        "OPENROUTER_HELPER_MODEL='anthropic/claude-haiku-4.5'\n"
    )
    # Observe resolved client configuration, not upstream network traffic.
    client = get_client(paths["env"])
    try:
        assert client.generation_model == "anthropic/claude-opus-4.8"
        assert client.helper_model == "anthropic/claude-haiku-4.5"
        assert load_env(paths["env"])["OPENROUTER_API_KEY"] == "sk-or-quoted"
    finally:
        client.close()


@pytest.mark.parametrize("resume", [False, True])
def test_existing_install_onboards_before_deploy_without_regeneration(paths, monkeypatch, resume):
    friend = paths["friends"] / "sam"
    friend.mkdir()
    (friend / "SOUL.md").write_text("# Sam\nOriginal personality")
    (paths["root"] / "profile.txt").write_text("Original profile")
    (paths["friends"] / "HISTORY.md").write_text("Original history")
    paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-old\n")
    if resume:
        checkpoint.CHECKPOINT_PATH.write_text(json.dumps({
            "step": "history", "anthropic_key": "sk-ant-old", "user_context": "Original profile",
        }))
    monkeypatch.setattr(wizard_main, "HOME_DIR", paths["root"])
    monkeypatch.setattr(sys, "argv", ["initialize"])
    inputs = ["r", "n", "2", "n"] if resume else ["d", "2", "n"]
    with patch("wizard.migrations.runner.check_and_run_pending", return_value=True), \
            patch("builtins.input", side_effect=inputs), \
            patch("wizard.steps.getpass", return_value="sk-or-upgraded"), \
            patch("wizard.llm.OpenRouter", side_effect=AssertionError("must not regenerate")), \
            patch("wizard.steps.subprocess.run", side_effect=AssertionError("must not deploy")):
        wizard_main.main()
    assert load_env(paths["env"])["OPENROUTER_API_KEY"] == "sk-or-upgraded"
    assert load_env(paths["env"])["ANTHROPIC_API_KEY"] == "sk-ant-old"
    assert (friend / "SOUL.md").read_text() == "# Sam\nOriginal personality"
    assert (paths["root"] / "profile.txt").read_text() == "Original profile"
    assert (paths["friends"] / "HISTORY.md").read_text() == "Original history"
    assert not checkpoint.CHECKPOINT_PATH.exists()


def test_credentials_and_chat_id_are_safe_for_docker_envfile(paths):
    expected = {
        "OPENROUTER_API_KEY": "sk-or-private",
        "TELEGRAM_BOT_TOKEN_ALEX": "123456:synthetic-token",
        "TELEGRAM_GROUP_CHAT_ID": "-123456",
    }
    for key, value in expected.items():
        set_env_var(paths["env"], key, value)
    # Docker --env-file treats wrapping quotes as literal value bytes.
    raw_assignments = dict(line.split("=", 1) for line in paths["env"].read_text().splitlines())
    assert raw_assignments == expected
    assert int(raw_assignments["TELEGRAM_GROUP_CHAT_ID"]) == -123456


def test_adjust_after_interrupted_upgrade_does_not_resume_deployment(paths, monkeypatch):
    friend = paths["friends"] / "sam"
    friend.mkdir()
    (friend / "SOUL.md").write_text("# Sam\nOriginal personality")
    paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-old\n")
    checkpoint.CHECKPOINT_PATH.write_text(json.dumps({
        "step": "openrouter_key", "resume_step": "deploy", "user_context": "Saved profile",
    }))
    monkeypatch.setattr(wizard_main, "HOME_DIR", paths["root"])
    monkeypatch.setattr(sys, "argv", ["initialize"])
    visited = []

    def profile_step(cp, _paths):
        visited.append(cp["step"])
        assert cp["user_context"] == "Saved profile"
        assert "resume_step" not in cp
        cp["step"] = "done"
        return cp

    monkeypatch.setattr(wizard_main, "STEPS", {"user_profile": profile_step})
    with patch("wizard.migrations.runner.check_and_run_pending", return_value=True), \
            patch.object(wizard_main, "_offer_delete_docker_volume"), \
            patch.object(wizard_main, "step_done"), \
            patch("builtins.input", return_value="a"), \
            patch("wizard.steps.getpass", return_value="sk-or-upgraded"):
        wizard_main.main()
    assert visited == ["user_profile"]
    assert (friend / "SOUL.md").read_text() == "# Sam\nOriginal personality"
