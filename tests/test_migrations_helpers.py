"""Tests for migration helper utilities."""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from migrations import helpers


@pytest.fixture
def friends_dir(tmp_path):
    fd = tmp_path / "friends"
    fd.mkdir()
    for name, chat in [("alex", 0.3), ("casey", 0.5), ("river", 0.9)]:
        p = fd / name
        p.mkdir()
        (p / "config.yaml").write_text(yaml.dump({
            "chattiness": chat, "timezone": "UTC",
            "schedule": {"wake_up": "08:00"},
        }))
        (p / "candidate.json").write_text(json.dumps({
            "name": name.capitalize(), "chattiness": chat, "traits": ["x"],
        }))
        (p / "SOUL.md").write_text(f"# {name}\nPersonality text")
    # Add a file and hidden dir to test filtering
    (fd / "HISTORY.md").write_text("shared history")
    hidden = fd / ".cache"
    hidden.mkdir()
    return fd


class TestIterFriends:
    def test_yields_all_friends(self, friends_dir):
        names = [n for n, _ in helpers.iter_friends(friends_dir)]
        assert names == ["alex", "casey", "river"]

    def test_skips_files(self, friends_dir):
        names = [n for n, _ in helpers.iter_friends(friends_dir)]
        assert "HISTORY.md" not in names

    def test_skips_hidden(self, friends_dir):
        names = [n for n, _ in helpers.iter_friends(friends_dir)]
        assert ".cache" not in names

    def test_missing_dir_yields_nothing(self, tmp_path):
        assert list(helpers.iter_friends(tmp_path / "nope")) == []


class TestUpdateConfig:
    def test_preserves_existing_keys(self, friends_dir):
        helpers.update_config(friends_dir / "alex", {"jokiness": 0.6})
        cfg = helpers.load_friend_config(friends_dir / "alex")
        assert cfg["chattiness"] == 0.3
        assert cfg["timezone"] == "UTC"
        assert cfg["jokiness"] == 0.6
        # Nested dicts survive
        assert cfg["schedule"]["wake_up"] == "08:00"

    def test_overwrites_when_key_exists(self, friends_dir):
        helpers.update_config(friends_dir / "alex", {"chattiness": 0.9})
        cfg = helpers.load_friend_config(friends_dir / "alex")
        assert cfg["chattiness"] == 0.9


class TestUpdateCandidate:
    def test_preserves_existing_keys(self, friends_dir):
        helpers.update_candidate(friends_dir / "alex", {"whininess": 0.4})
        cand = helpers.load_friend_candidate(friends_dir / "alex")
        assert cand["name"] == "Alex"
        assert cand["chattiness"] == 0.3
        assert cand["whininess"] == 0.4
        assert cand["traits"] == ["x"]


class TestApplyToAll:
    def test_runs_fn_once_per_friend(self, friends_dir):
        seen = []
        def fn(name, path):
            seen.append(name)
            return {"dummy": True}
        result = helpers.apply_to_all(friends_dir, fn)
        assert set(seen) == {"alex", "casey", "river"}
        assert all(v == {"dummy": True} for v in result.values())

    def test_empty_dict_stored_when_fn_returns_nothing(self, friends_dir):
        result = helpers.apply_to_all(friends_dir, lambda n, p: None)
        assert all(v == {} for v in result.values())


class TestChooseOneByOne:
    def test_user_accepts_all(self, friends_dir):
        inputs = iter(["y", "y", "y"])
        def fn(name, path):
            return {"jokiness": 0.5}
        result = helpers.choose_one_by_one(friends_dir, fn, input_fn=lambda _: next(inputs))
        assert set(result.keys()) == {"alex", "casey", "river"}

    def test_user_skips_one(self, friends_dir):
        inputs = iter(["y", "n", "y"])
        def fn(name, path):
            return {"jokiness": 0.5}
        result = helpers.choose_one_by_one(friends_dir, fn, input_fn=lambda _: next(inputs))
        assert "casey" not in result

    def test_user_edits_value(self, friends_dir):
        inputs = iter(["e", "0.9"])  # edit, then new value for jokiness
        def fn(name, path):
            if name != "alex":
                return {}
            return {"jokiness": 0.5}
        result = helpers.choose_one_by_one(friends_dir, fn, input_fn=lambda _: next(inputs))
        assert result["alex"]["jokiness"] == "0.9"

    def test_empty_proposal_skipped(self, friends_dir):
        def fn(name, path):
            return {}
        # No inputs should be requested when nothing is proposed
        result = helpers.choose_one_by_one(friends_dir, fn, input_fn=lambda _: pytest.fail("no input expected"))
        assert result == {}


class TestPromptChoice:
    def test_returns_matched_option(self):
        inputs = iter(["llm"])
        r = helpers.prompt_choice(
            "pick?", ["llm", "manual", "defaults"],
            input_fn=lambda _: next(inputs),
        )
        assert r == "llm"

    def test_default_on_empty(self):
        inputs = iter([""])
        r = helpers.prompt_choice(
            "pick?", ["y", "n"], default="y",
            input_fn=lambda _: next(inputs),
        )
        assert r == "y"

    def test_single_char_prefix_match(self):
        inputs = iter(["m"])
        r = helpers.prompt_choice(
            "pick?", ["llm", "manual", "defaults"],
            input_fn=lambda _: next(inputs),
        )
        assert r == "manual"

    def test_reprompts_on_invalid(self):
        inputs = iter(["nope", "llm"])
        r = helpers.prompt_choice(
            "pick?", ["llm", "manual"],
            input_fn=lambda _: next(inputs),
        )
        assert r == "llm"


class TestPromptFloat:
    def test_default_on_empty(self):
        r = helpers.prompt_float("val?", default=0.5, input_fn=lambda _: "")
        assert r == 0.5

    def test_parses_valid(self):
        r = helpers.prompt_float("val?", default=0.5, input_fn=lambda _: "0.7")
        assert r == 0.7

    def test_reprompts_on_out_of_range(self):
        inputs = iter(["2.5", "0.4"])
        r = helpers.prompt_float("val?", default=0.5, input_fn=lambda _: next(inputs))
        assert r == 0.4

    def test_reprompts_on_nonsense(self):
        inputs = iter(["banana", "0.3"])
        r = helpers.prompt_float("val?", default=0.5, input_fn=lambda _: next(inputs))
        assert r == 0.3
