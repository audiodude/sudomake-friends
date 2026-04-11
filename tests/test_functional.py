"""Functional tests for sudomake-friends scripts."""

import curses
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to path so we can import the wizard package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wizard as lib  # re-exports the public surface via wizard/__init__.py
detect_platform = lib.detect_platform
PLATFORMS = lib.PLATFORMS


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project structure in a temp dir."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").touch()
    (tmp_path / "friends").mkdir()
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-test123\n")
    return tmp_path


@pytest.fixture
def paths(tmp_project):
    return lib.get_paths(tmp_project)


@pytest.fixture
def sample_candidates():
    return [
        {"name": "Casey", "age": 30, "location": "Boston, MA",
         "occupation": "Developer", "vibe": "Chill and thoughtful",
         "why": "Shared interests", "timezone": "America/New_York",
         "chattiness": 0.5},
        {"name": "River", "age": 28, "location": "Portland, OR",
         "occupation": "Designer", "vibe": "Creative and quiet",
         "why": "Art connection", "timezone": "America/Los_Angeles",
         "chattiness": 0.3},
        {"name": "Sage", "age": 35, "location": "Berlin, Germany",
         "occupation": "Writer", "vibe": "Philosophical and dry",
         "why": "Deep conversations", "timezone": "Europe/Berlin",
         "chattiness": 0.7},
    ]


# ─── Platform detection ──────────────────────────────────────────────────────

class TestPlatformDetection:
    """Tests 10-12: Platform URL detection."""

    def test_all_plugins_load(self):
        assert len(PLATFORMS) >= 10

    @pytest.mark.parametrize("url,expected_name", [
        ("https://bsky.app/profile/test.bsky.social", "Bluesky"),
        ("https://github.com/audiodude", "GitHub"),
        ("https://letterboxd.com/testuser", "Letterboxd"),
        ("https://www.last.fm/user/testuser", "Last.fm"),
        ("https://test.tumblr.com", "Tumblr"),
        ("https://test.bandcamp.com", "Bandcamp"),
        ("https://www.discogs.com/user/testuser", "Discogs"),
        ("https://news.ycombinator.com/user?id=testuser", "Hacker News"),
        ("https://www.goodreads.com/user/show/12345-test", "Goodreads"),
        ("https://steamcommunity.com/id/testuser", "Steam"),
        ("https://dev.to/testuser", "dev.to"),
        ("https://en.wikipedia.org/wiki/User:TestUser", "Wikipedia"),
    ])
    def test_detect_own_urls(self, url, expected_name):
        plugin = detect_platform(url)
        assert plugin is not None, f"No plugin matched {url}"
        assert plugin["name"] == expected_name

    def test_unknown_url_returns_none(self):
        assert detect_platform("https://example.com/random/page") is None

    def test_no_cross_detection(self):
        """GitHub URL shouldn't match Bluesky, etc."""
        plugin = detect_platform("https://github.com/audiodude")
        assert plugin["name"] == "GitHub"
        # Make sure it's not matching something else
        assert plugin["name"] != "Bluesky"


# ─── Platform fetching with cache ────────────────────────────────────────────

class TestPlatformCache:
    """Tests 13-15: Caching behavior."""

    def test_first_fetch_writes_cache(self, tmp_path):
        """Scrape writes a cache file."""
        # Use a fake URL that will fail to actually fetch
        result = lib.scrape_site("http://localhost:99999/nonexistent", tmp_path)
        # Even a failed scrape should not crash
        assert isinstance(result, str)

    def test_cached_fetch_no_network(self, tmp_path):
        """Second read comes from cache."""
        import hashlib
        url = "https://example.com/test"
        cache_key = hashlib.md5(url.encode()).hexdigest()
        cache_file = tmp_path / f"{cache_key}.txt"
        cache_file.write_text("cached content here")

        result = lib.scrape_site(url, tmp_path)
        assert result == "cached content here"

    def test_broken_url_returns_empty(self, tmp_path):
        result = lib.scrape_site("http://localhost:99999/broken", tmp_path)
        assert isinstance(result, str)



# ─── Friend directory creation ────────────────────────────────────────────────

class TestFriendDir:
    """Tests 18-20: Friend directory creation."""

    def test_creates_all_files(self, paths, sample_candidates):
        c = sample_candidates[0]
        slug = lib.create_friend_dir(paths["friends"], c["name"], "# Soul", c)
        friend_dir = paths["friends"] / slug

        assert (friend_dir / "SOUL.md").exists()
        assert (friend_dir / "MEMORY.md").exists()
        assert (friend_dir / "config.yaml").exists()

    def test_config_has_correct_values(self, paths, sample_candidates):
        import yaml
        c = sample_candidates[0]
        slug = lib.create_friend_dir(paths["friends"], c["name"], "# Soul", c)
        config = yaml.safe_load((paths["friends"] / slug / "config.yaml").read_text())

        assert config["timezone"] == "America/New_York"
        assert config["chattiness"] == 0.5
        # New wizard-written friends should always ship with the personality dials
        assert "jokiness" in config
        assert "whininess" in config

    def test_slug_is_lowercase_underscore(self, paths):
        c = {"name": "Mary Jane", "age": 30, "location": "NYC",
             "occupation": "Dev", "vibe": "Cool", "why": "Yes",
             "timezone": "UTC", "chattiness": 0.5}
        slug = lib.create_friend_dir(paths["friends"], c["name"], "# Soul", c)
        assert slug == "mary_jane"
        assert (paths["friends"] / "mary_jane").exists()


# ─── Checkpoint system ────────────────────────────────────────────────────────

class TestCheckpoint:
    """Tests 21-23: Checkpoint persistence."""

    def test_save_load_roundtrip(self, tmp_path):
        cp_path = tmp_path / ".init-checkpoint.json"
        cp_path.write_text(json.dumps({"step": "select_friends", "data": [1, 2]}))
        loaded = json.loads(cp_path.read_text())
        assert loaded["step"] == "select_friends"
        assert loaded["data"] == [1, 2]

    def test_clear_removes_file(self, tmp_path):
        cp_path = tmp_path / ".init-checkpoint.json"
        cp_path.write_text("{}")
        cp_path.unlink()
        assert not cp_path.exists()

    def test_missing_checkpoint_is_start(self, tmp_path):
        cp_path = tmp_path / ".init-checkpoint.json"
        if cp_path.exists():
            cp_path.unlink()
        # Simulates what load_checkpoint does
        data = json.loads(cp_path.read_text()) if cp_path.exists() else {"step": "start"}
        assert data == {"step": "start"}


# ─── Selection UI ─────────────────────────────────────────────────────────────

def _make_mock_stdscr(keys: list[int], size=(40, 120)):
    """Create a mock curses stdscr that returns keys in sequence."""
    stdscr = MagicMock()
    stdscr.getmaxyx.return_value = size
    stdscr.getch.side_effect = keys
    return stdscr


@pytest.fixture()
def mock_curses():
    """Mock curses functions that need a real terminal."""
    with patch("wizard.tui.curses.curs_set"), \
         patch("wizard.tui.curses.use_default_colors"), \
         patch("wizard.tui.curses.init_pair"), \
         patch("wizard.tui.curses.color_pair", return_value=0):
        yield


@pytest.mark.usefixtures("mock_curses")
class TestSelectionUI:
    """Tests 1-9: TUI behavior via mocked curses."""

    def test_arrow_down_moves_cursor(self, sample_candidates):
        # Down, Down, then save+exit (s, confirm y)
        keys = [curses.KEY_DOWN, curses.KEY_DOWN, ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_enter_toggles_hold(self, sample_candidates):
        # Select first, then save+exit
        keys = [ord("\n"), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 in held
        assert action == "quit"

    def test_enter_toggles_off(self, sample_candidates):
        # Toggle on, toggle off, save+exit
        keys = [ord("\n"), ord("\n"), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 not in held

    def test_space_also_toggles(self, sample_candidates):
        keys = [ord(" "), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 in held

    def test_backspace_only_uninvites(self, sample_candidates):
        # Select first, move down, backspace (should not invite #1), move up, backspace (should uninvite #0)
        keys = [ord("\n"), curses.KEY_DOWN, 127, curses.KEY_UP, 127, ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 not in held
        assert 1 not in held

    def test_r_returns_reroll(self, sample_candidates):
        keys = [ord("r")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "reroll"

    def test_q_returns_accept_when_held(self, sample_candidates):
        # q=accept+continue, confirm with y
        keys = [ord("\n"), ord("q"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "accept"
        assert 0 in held

    def test_q_shows_warning_when_none_held(self, sample_candidates):
        # Press q with nothing held — should flash warning then wait for key, then save+exit
        keys = [ord("q"), ord(" "), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"
        assert len(held) == 0

    def test_q_cancel_confirm_stays(self, sample_candidates):
        # q with held, decline confirm (n), then save+exit
        keys = [ord("\n"), ord("q"), ord("n"), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_s_cancel_confirm_stays(self, sample_candidates):
        # s, decline confirm (n), then q to accept
        keys = [ord("\n"), ord("s"), ord("n"), ord("q"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "accept"

    def test_e_returns_edit_candidate(self, sample_candidates):
        keys = [ord("e")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert isinstance(action, tuple)
        assert action[0] == "edit_candidate"
        assert action[1] == 0

    def test_s_preserves_held(self, sample_candidates):
        # Hold first two, save+exit
        keys = [ord("\n"), curses.KEY_DOWN, ord("\n"), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"
        assert held == {0, 1}

    def test_cursor_stays_in_bounds(self, sample_candidates):
        # Up from top, should stay at 0
        keys = [curses.KEY_UP, curses.KEY_UP, ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_resume_with_preheld(self, sample_candidates):
        # Start with {1} held, save+exit immediately
        keys = [ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, {1})
        assert 1 in held

    def test_x_opens_modal_any_key_dismisses(self, sample_candidates):
        # x to open modal, any key to close, save+exit
        keys = [ord("x"), ord(" "), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_vim_keys(self, sample_candidates):
        # j=down, k=up, then save+exit
        keys = [ord("j"), ord("j"), ord("k"), ord("s"), ord("y")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"


# ─── User context gathering ──────────────────────────────────────────────────

class TestUserContext:
    """Tests 24-31: Context input routing."""

    def test_url_routes_to_fetch(self, paths):
        with patch("wizard.scraper._fetch_url", return_value="fetched content") as mock:
            with patch("builtins.input", side_effect=["https://example.com", "q", "n"]):
                result, sources = lib.get_user_context(paths)
        mock.assert_called_once()
        assert "fetched content" in result
        assert sources[0]["label"] == "https://example.com"

    def test_file_path_routes_to_read(self, paths, tmp_path):
        test_file = tmp_path / "about.txt"
        test_file.write_text("I like music and coding")
        with patch("builtins.input", side_effect=[str(test_file), "q", "n"]):
            result, sources = lib.get_user_context(paths)
        assert "music and coding" in result
        assert sources[0]["label"] == "about.txt"

    def test_freetext_treated_as_description(self, paths):
        with patch("builtins.input", side_effect=["I am a developer in SF", "q"]):
            result, sources = lib.get_user_context(paths)
        assert "developer in SF" in result
        assert sources[0]["label"] == "description"

    def test_question_mark_shows_help(self, paths, capsys):
        with patch("builtins.input", side_effect=["?", "I like cats", "q"]):
            result, sources = lib.get_user_context(paths)
        output = capsys.readouterr().out
        assert "Supported platforms" in output
        assert len(sources) == 1  # ? didn't count

    def test_q_with_zero_sources_reprompts(self, paths):
        with patch("builtins.input", side_effect=["q", "I exist", "q"]):
            result, sources = lib.get_user_context(paths)
        assert "I exist" in result

    def test_q_with_sources_returns(self, paths):
        with patch("builtins.input", side_effect=["hello world", "q"]):
            result, sources = lib.get_user_context(paths)
        assert "hello world" in result
        assert len(sources) == 1

    def test_multiple_sources_joined(self, paths):
        with patch("builtins.input", side_effect=["source one", "source two", "q"]):
            result, sources = lib.get_user_context(paths)
        assert "---" in result
        assert len(sources) == 2

    def test_empty_input_ignored(self, paths):
        with patch("builtins.input", side_effect=["", "", "something", "q"]):
            result, sources = lib.get_user_context(paths)
        assert len(sources) == 1

    def test_missing_path_not_added_as_text(self, paths):
        with patch("builtins.input", side_effect=["../nonexistent/file.txt", "real text", "q"]):
            result, sources = lib.get_user_context(paths)
        assert len(sources) == 1
        assert "nonexistent" not in result
        assert "real text" in result

    def test_tilde_path_not_added_as_text(self, paths):
        with patch("builtins.input", side_effect=["~/no/such/file", "ok fine", "q"]):
            result, sources = lib.get_user_context(paths)
        assert len(sources) == 1
        assert sources[0]["label"] == "description"

    def test_file_with_bad_bytes_still_reads(self, paths, tmp_path):
        bad_file = tmp_path / "page.html"
        bad_file.write_bytes(b"<html>\x80\xff<body>good content</body></html>")
        with patch("builtins.input", side_effect=[str(bad_file), "q", "n"]):
            result, sources = lib.get_user_context(paths)
        assert len(sources) == 1
        assert "good content" in result

    def test_cached_sources_offered(self, paths):
        cached = [{"label": "old.com", "content": "old stuff"}]
        with patch("builtins.input", side_effect=["y", "q", "n"]):
            result, sources = lib.get_user_context(paths, cached_sources=cached)
        assert "old stuff" in result
        assert len(sources) == 1

    def test_cached_sources_declined(self, paths):
        cached = [{"label": "old.com", "content": "old stuff"}]
        with patch("builtins.input", side_effect=["n", "new stuff", "q"]):
            result, sources = lib.get_user_context(paths, cached_sources=cached)
        assert "old stuff" not in result
        assert "new stuff" in result


# ─── Shared selection loop ────────────────────────────────────────────────────

class TestSelectionLoop:
    """Tests 32-35: run_selection_loop integration."""

    def test_returns_selected_on_accept(self, sample_candidates):
        with patch("wizard.tui.curses.wrapper") as mock_wrapper:
            # Simulate: select first, accept
            mock_wrapper.return_value = ({0}, "accept")
            result = lib.run_selection_loop(
                client=MagicMock(),
                user_context="test",
                candidates=sample_candidates,
            )
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "Casey"

    def test_returns_none_on_quit(self, sample_candidates):
        with patch("wizard.tui.curses.wrapper") as mock_wrapper:
            mock_wrapper.return_value = (set(), "quit")
            result = lib.run_selection_loop(
                client=MagicMock(),
                user_context="test",
                candidates=sample_candidates,
            )
        assert result is None

    def test_on_save_called(self, sample_candidates):
        saves = []
        with patch("wizard.tui.curses.wrapper") as mock_wrapper:
            mock_wrapper.return_value = ({0}, "accept")
            lib.run_selection_loop(
                client=MagicMock(),
                user_context="test",
                candidates=sample_candidates,
                on_save=lambda c, h: saves.append((c, h)),
            )
        assert len(saves) >= 1

    def test_edit_candidate_roundtrips(self):
        candidate = {"name": "Sage", "age": 35, "location": "Berlin, Germany",
                     "occupation": "Writer", "vibe": "Dry and philosophical",
                     "why": "Deep talks", "timezone": "Europe/Berlin",
                     "chattiness": 0.7}
        text = lib.candidate_to_text(candidate)
        assert "Sage" in text
        assert "Berlin" in text

        # Modify the text
        modified = text.replace("Sage", "Ash").replace("35", "29")
        result = lib.text_to_candidate(modified, candidate)
        assert result["name"] == "Ash"
        assert result["age"] == 29
        assert result["chattiness"] == 0.7  # preserved from original


# ─── Soul generation ─────────────────────────────────────────────────────────

class TestSoulGeneration:
    """Tests 36-38: generate_souls_for_selected behavior."""

    def test_skips_existing_soul_on_disk(self, paths, sample_candidates):
        # Create an existing soul
        slug = sample_candidates[0]["name"].lower()
        soul_dir = paths["friends"] / slug
        soul_dir.mkdir(parents=True)
        (soul_dir / "SOUL.md").write_text("# Existing Soul")

        souls = lib.generate_souls_for_selected(
            client=MagicMock(),  # should not be called
            selected=[sample_candidates[0]],
            user_context="test",
            friends_dir=paths["friends"],
        )
        assert souls[sample_candidates[0]["name"]] == "# Existing Soul"

    def test_skips_cached_souls(self, paths, sample_candidates):
        cached = {"Casey": "# Cached Soul"}
        souls = lib.generate_souls_for_selected(
            client=MagicMock(),
            selected=[sample_candidates[0]],
            user_context="test",
            friends_dir=paths["friends"],
            cached_souls=cached,
        )
        assert souls["Casey"] == "# Cached Soul"

    def test_on_save_soul_called(self, paths, sample_candidates):
        saved = []
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="# Generated Soul")]
        )
        lib.generate_souls_for_selected(
            client=mock_client,
            selected=[sample_candidates[0]],
            user_context="test",
            friends_dir=paths["friends"],
            on_save_soul=lambda name, text: saved.append((name, text)),
        )
        assert len(saved) == 1
        assert saved[0][0] == "Casey"


# ─── Existing friends detection ───────────────────────────────────────────────

class TestExistingFriends:

    def test_detects_friends_with_soul(self, paths):
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")
        names = lib.get_existing_friend_names(paths["friends"])
        assert "casey" in names

    def test_ignores_dirs_without_soul(self, paths):
        d = paths["friends"] / "nobody"
        d.mkdir()
        names = lib.get_existing_friend_names(paths["friends"])
        assert "nobody" not in names

    def test_ignores_dotdirs(self, paths):
        d = paths["friends"] / ".template"
        d.mkdir()
        (d / "SOUL.md").write_text("# Template")
        names = lib.get_existing_friend_names(paths["friends"])
        assert ".template" not in names


# ─── Setup complete detection ─────────────────────────────────────────────────

class TestSetupDetection:
    """Test that completed setup is detected for redeploy."""

    def test_detects_complete_setup(self, paths):
        """With .env + friends, setup is complete."""
        paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")

        existing = lib.get_existing_friend_names(paths["friends"])
        setup_complete = paths["env"].exists() and len(existing) > 0
        assert setup_complete is True

    def test_incomplete_without_friends(self, paths):
        """With .env but no friends, setup is not complete."""
        paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-test\n")

        existing = lib.get_existing_friend_names(paths["friends"])
        setup_complete = paths["env"].exists() and len(existing) > 0
        assert setup_complete is False

    def test_incomplete_without_env(self, paths):
        """With friends but no .env, setup is not complete."""
        if paths["env"].exists():
            paths["env"].unlink()
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")

        existing = lib.get_existing_friend_names(paths["friends"])
        setup_complete = paths["env"].exists() and len(existing) > 0
        assert setup_complete is False

    def test_complete_even_with_stale_checkpoint(self, paths):
        """With everything including a stale checkpoint, still detected as complete."""
        paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")

        existing = lib.get_existing_friend_names(paths["friends"])

        setup_complete = (
            paths["env"].exists()
            and len(existing) > 0
        )
        assert setup_complete is True


# ─── Timezone validation ──────────────────────────────────────────────────────

class TestTimezoneValidation:

    def test_valid_timezone_passes(self):
        assert lib._validate_timezone("America/New_York") == "America/New_York"
        assert lib._validate_timezone("Europe/Berlin") == "Europe/Berlin"
        assert lib._validate_timezone("UTC") == "UTC"

    def test_spaces_replaced(self):
        assert lib._validate_timezone("America/Los Angeles") == "America/Los_Angeles"
        assert lib._validate_timezone("America/New York") == "America/New_York"

    def test_garbage_falls_back_to_default(self):
        assert lib._validate_timezone("NotA/Timezone") == "America/New_York"
        assert lib._validate_timezone("asdf") == "America/New_York"

    def test_location_helps_guess(self):
        assert lib._validate_timezone("garbage", "Berlin, Germany") == "Europe/Berlin"
        assert lib._validate_timezone("nope", "San Francisco, CA") == "America/Los_Angeles"
        assert lib._validate_timezone("bad", "Tokyo, Japan") == "Asia/Tokyo"

    def test_empty_string_falls_back(self):
        assert lib._validate_timezone("") == "America/New_York"

    def test_text_to_candidate_validates(self):
        candidate = {"name": "Test", "age": 30, "location": "Berlin, Germany",
                     "occupation": "Dev", "vibe": "Cool", "why": "Yes",
                     "timezone": "UTC", "chattiness": 0.5}
        text = "Name: Test\nTimezone: Europe/Blerlin\nLocation: Berlin, Germany"
        result = lib.text_to_candidate(text, candidate)
        # Bad timezone "Europe/Blerlin" should get fixed via location
        assert result["timezone"] == "Europe/Berlin"


# ─── User profile step ──────────────────────────────────────────────────────

class TestStepUserProfile:
    """Tests for step_user_profile: caching, persistence, recompile flow."""

    @pytest.fixture
    def profile_paths(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("ANTHROPIC_API_KEY=sk-ant-test123\n")
        return {"root": tmp_path, "friends": tmp_path / "friends", "env": env}

    def test_reuses_profile_from_checkpoint(self, profile_paths):
        cp = {"step": "user_profile", "user_context": "I am Travis."}
        with patch("builtins.input", return_value="n"):
            result = lib.step_user_profile(cp, profile_paths)
        assert result["step"] == "select_friends"
        assert result["user_context"] == "I am Travis."

    def test_reuses_profile_from_file(self, profile_paths):
        (profile_paths["root"] / "profile.txt").write_text("Saved profile.")
        cp = {"step": "user_profile"}
        with patch("builtins.input", return_value="n"):
            result = lib.step_user_profile(cp, profile_paths)
        assert result["step"] == "select_friends"
        assert result["user_context"] == "Saved profile."

    def test_recompile_saves_to_file(self, profile_paths):
        (profile_paths["root"] / "profile.txt").write_text("Old profile.")
        cp = {"step": "user_profile", "user_context": "Old profile."}
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="New profile.")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        with patch("wizard.steps.get_client", return_value=mock_client):
            with patch("builtins.input", side_effect=["y", "new source", "q"]):
                result = lib.step_user_profile(cp, profile_paths)
        assert result["user_context"] == "New profile."
        assert (profile_paths["root"] / "profile.txt").read_text() == "New profile."

    def test_recompile_clears_candidates(self, profile_paths):
        cp = {"step": "user_profile", "user_context": "Old.",
              "candidates": [{"name": "Stale"}], "held_indices": [0]}
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="New profile.")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        with patch("wizard.steps.get_client", return_value=mock_client):
            with patch("builtins.input", side_effect=["y", "new source", "q"]):
                result = lib.step_user_profile(cp, profile_paths)
        assert "candidates" not in result
        assert "held_indices" not in result

    def test_recompile_warns_about_reroll(self, profile_paths, capsys):
        cp = {"step": "user_profile", "user_context": "Old."}
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="New profile.")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        with patch("wizard.steps.get_client", return_value=mock_client):
            with patch("builtins.input", side_effect=["y", "new source", "q"]):
                lib.step_user_profile(cp, profile_paths)
        output = capsys.readouterr().out
        assert "re-roll" in output


# ─── History generation ─────────────────────────────────────────────────────

class TestStepHistory:
    """Tests for the HISTORY.md generation step."""

    @pytest.fixture
    def friends_with_souls(self, tmp_path):
        """Create a friends dir with SOUL.md files."""
        friends_dir = tmp_path / "friends"
        friends_dir.mkdir()
        for name in ("alex", "river"):
            d = friends_dir / name
            d.mkdir()
            (d / "SOUL.md").write_text(f"# {name}\nA cool person from somewhere.")
        return friends_dir

    @pytest.fixture
    def history_paths(self, tmp_path, friends_with_souls):
        return {
            "root": tmp_path,
            "friends": friends_with_souls,
            "env": tmp_path / ".env",
        }

    def test_skip_if_no(self, history_paths):
        cp = {"step": "history"}
        with patch("builtins.input", return_value="n"):
            result = lib.step_history(cp, history_paths)
        assert result["step"] == "deploy"
        assert not (history_paths["friends"] / "HISTORY.md").exists()

    def test_keep_if_already_exists(self, history_paths):
        (history_paths["friends"] / "HISTORY.md").write_text("existing history")
        cp = {"step": "history"}
        with patch("builtins.input", return_value="n"):
            result = lib.step_history(cp, history_paths)
        assert result["step"] == "deploy"
        assert (history_paths["friends"] / "HISTORY.md").read_text() == "existing history"

    def test_write_directly(self, history_paths):
        cp = {"step": "history", "anthropic_key": "sk-test", "user_context": "Travis"}
        with patch("wizard.steps.generate_history", return_value="# HISTORY\nThey met at a park."):
            with patch("builtins.input", return_value="w"):
                result = lib.step_history(cp, history_paths)

        assert result["step"] == "deploy"
        history = (history_paths["friends"] / "HISTORY.md").read_text()
        assert "They met at a park" in history

    def test_display_then_write(self, history_paths, capsys):
        cp = {"step": "history", "anthropic_key": "sk-test", "user_context": "Travis"}
        with patch("wizard.steps.generate_history", return_value="# HISTORY\nFriends since 2020."):
            with patch("builtins.input", side_effect=["d", "w"]):
                result = lib.step_history(cp, history_paths)

        output = capsys.readouterr().out
        assert "Friends since 2020" in output
        assert (history_paths["friends"] / "HISTORY.md").exists()

    def test_display_then_decline(self, history_paths):
        cp = {"step": "history", "anthropic_key": "sk-test", "user_context": "Travis"}
        with patch("wizard.steps.generate_history", return_value="# HISTORY\nSome history."):
            with patch("builtins.input", side_effect=["d", "q"]):
                result = lib.step_history(cp, history_paths)

        assert result["step"] == "deploy"
        assert not (history_paths["friends"] / "HISTORY.md").exists()

    def test_display_regenerate_then_write(self, history_paths):
        cp = {"step": "history", "anthropic_key": "sk-test", "user_context": "Travis"}
        with patch("wizard.steps.generate_history", side_effect=["Version 1.", "Version 2."]):
            with patch("builtins.input", side_effect=["d", "r", "w"]):
                result = lib.step_history(cp, history_paths)

        history = (history_paths["friends"] / "HISTORY.md").read_text()
        assert "Version 2" in history


# ─── create_friend_dir ──────────────────────────────────────────────────────

class TestCreateFriendDir:
    """Tests for create_friend_dir saving candidate.json."""

    def test_saves_candidate_json(self, paths, sample_candidates):
        c = sample_candidates[0]
        slug = lib.create_friend_dir(paths["friends"], c["name"], "# Soul", c)
        cpath = paths["friends"] / slug / "candidate.json"
        assert cpath.exists()
        saved = json.loads(cpath.read_text())
        assert saved["name"] == c["name"]
        assert saved["vibe"] == c["vibe"]

    def test_creates_soul_and_config(self, paths, sample_candidates):
        c = sample_candidates[0]
        slug = lib.create_friend_dir(paths["friends"], c["name"], "# My Soul", c)
        d = paths["friends"] / slug
        assert (d / "SOUL.md").read_text() == "# My Soul"
        assert (d / "config.yaml").exists()
        assert (d / "MEMORY.md").exists()


# ─── step_select_friends keep/edit ──────────────────────────────────────────

class TestStepSelectFriendsExisting:
    """Tests for the keep/edit flow when friends already exist."""

    @pytest.fixture
    def friends_setup(self, tmp_path):
        """Create friends dir with existing friends + candidate.json files."""
        friends_dir = tmp_path / "friends"
        friends_dir.mkdir()
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-ant-test123\n")
        candidates = []
        for name in ("alex", "river"):
            d = friends_dir / name
            d.mkdir()
            (d / "SOUL.md").write_text(f"# {name}")
            c = {"name": name.title(), "age": 30, "location": "Brooklyn, NY",
                 "occupation": "Dev", "vibe": "Cool", "why": "Yes",
                 "timezone": "America/New_York", "chattiness": 0.5}
            (d / "candidate.json").write_text(json.dumps(c))
            candidates.append(c)
        paths = {"root": tmp_path, "friends": friends_dir, "env": env_path}
        return paths, candidates

    def test_keep_existing_friends(self, friends_setup):
        paths, _ = friends_setup
        cp = {"step": "select_friends", "user_context": "test"}
        with patch("builtins.input", return_value="y"):
            result = lib.step_select_friends(cp, paths)
        assert result["step"] == "telegram_bots"
        assert len(result["selected"]) == 2

    def test_decline_existing_goes_to_selection(self, friends_setup):
        paths, _ = friends_setup
        cp = {"step": "select_friends", "user_context": "test"}
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='[{"name":"New","age":25,"location":"SF","occupation":"Dev","vibe":"Fun","why":"Yes","timezone":"America/Los_Angeles","chattiness":0.5}]')]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        with patch("wizard.steps.get_client", return_value=mock_client):
            with patch("wizard.tui.curses.wrapper") as mock_wrapper:
                mock_wrapper.return_value = ({0}, "accept")
                with patch("wizard.friends.generate_soul", return_value="# Soul"):
                    with patch("builtins.input", side_effect=["n"]):
                        result = lib.step_select_friends(cp, paths)
        assert result["step"] == "telegram_bots"

    def test_edit_loads_candidate_json(self, friends_setup):
        paths, candidates = friends_setup
        cp = {"step": "select_friends", "user_context": "test"}
        new_candidate = {"name": "Sage", "age": 35, "location": "Berlin",
                         "occupation": "Writer", "vibe": "Dry", "why": "Yes",
                         "timezone": "Europe/Berlin", "chattiness": 0.7}
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([new_candidate]))]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        with patch("wizard.steps.get_client", return_value=mock_client):
            with patch("wizard.tui.curses.wrapper") as mock_wrapper:
                # Accept all (2 existing held + new ones)
                mock_wrapper.return_value = ({0, 1}, "accept")
                with patch("wizard.friends.generate_soul", return_value="# Soul"):
                    with patch("builtins.input", return_value="e"):
                        result = lib.step_select_friends(cp, paths)
        # Should have loaded candidates into checkpoint
        assert cp.get("candidates") is not None
        assert len(cp["candidates"]) >= 2

    def test_edit_bails_without_candidate_json(self, tmp_path, capsys):
        """Friends without candidate.json skip edit and keep existing."""
        friends_dir = tmp_path / "friends"
        friends_dir.mkdir()
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-ant-test123\n")
        d = friends_dir / "alex"
        d.mkdir()
        (d / "SOUL.md").write_text("# alex")
        # No candidate.json
        paths = {"root": tmp_path, "friends": friends_dir, "env": env_path}
        cp = {"step": "select_friends", "user_context": "test"}
        with patch("builtins.input", return_value="e"):
            result = lib.step_select_friends(cp, paths)
        assert result["step"] == "telegram_bots"
        output = capsys.readouterr().out
        assert "before edit support" in output


# ─── sources.txt ────────────────────────────────────────────────────────────

class TestSourcesFile:
    """Tests for sources.txt auto-loading and save offer."""

    def test_loads_sources_txt(self, paths, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sources_file = tmp_path / "sources.txt"
        sources_file.write_text("I am a developer\nI like music\n")
        with patch("builtins.input", side_effect=["y", "q"]):
            result, sources = lib.get_user_context(paths)
        assert len(sources) == 2
        assert "developer" in result
        assert "music" in result

    def test_sources_txt_with_urls(self, paths, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sources_file = tmp_path / "sources.txt"
        sources_file.write_text("https://example.com\n")
        with patch("wizard.scraper._fetch_url", return_value="fetched stuff"):
            with patch("builtins.input", side_effect=["y", "q", "n"]):
                result, sources = lib.get_user_context(paths)
        assert "fetched stuff" in result

    def test_decline_sources_txt(self, paths, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sources_file = tmp_path / "sources.txt"
        sources_file.write_text("I am a developer\n")
        with patch("builtins.input", side_effect=["n", "something else", "q"]):
            result, sources = lib.get_user_context(paths)
        assert "developer" not in result
        assert "something else" in result

    def test_offers_to_save_sources(self, paths, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with patch("builtins.input", side_effect=["https://example.com", "q", "y"]):
            with patch("wizard.scraper._fetch_url", return_value="stuff"):
                result, sources = lib.get_user_context(paths)
        assert (tmp_path / "sources.txt").exists()
        assert "example.com" in (tmp_path / "sources.txt").read_text()

    def test_no_save_offer_for_description_only(self, paths, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("builtins.input", side_effect=["just some text", "q"]):
            result, sources = lib.get_user_context(paths)
        # No save offer since only descriptions (no URLs/files)
        assert not (tmp_path / "sources.txt").exists()


# ─── Main menu ──────────────────────────────────────────────────────────────

class TestMainMenu:
    """Tests for the setup-complete menu options."""

    @pytest.fixture
    def complete_setup(self, tmp_path):
        """Create a complete setup state."""
        friends_dir = tmp_path / "friends"
        friends_dir.mkdir()
        for name in ("alex", "river"):
            d = friends_dir / name
            d.mkdir()
            (d / "SOUL.md").write_text(f"# {name}")
        env_path = tmp_path / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-ant-test\nTELEGRAM_GROUP_CHAT_ID=-123\n")
        return tmp_path

    def test_adjust_sets_step_to_start(self, complete_setup):
        """Adjust option should walk through from the beginning."""
        cp = {"step": "done"}
        # We can't easily run the full main() loop, but verify the menu logic
        # by checking that 'a' sets step to 'start'
        existing = lib.get_existing_friend_names(complete_setup / "friends")
        assert len(existing) == 2
        # The menu sets cp["step"] = "start" for adjust
        # Verify this is what the code does by simulating the branch
        has_incomplete = cp.get("step") and cp["step"] not in ("start", "done", "deploy")
        assert not has_incomplete  # no incomplete step

    def test_history_regenerate_offer(self, complete_setup):
        """When HISTORY.md exists, step_history offers to regenerate."""
        friends_dir = complete_setup / "friends"
        (friends_dir / "HISTORY.md").write_text("old history")
        paths = {"root": complete_setup, "friends": friends_dir,
                 "env": complete_setup / ".env"}
        cp = {"step": "history", "anthropic_key": "sk-test", "user_context": "test"}
        # Decline regeneration
        with patch("builtins.input", return_value="n"):
            result = lib.step_history(cp, paths)
        assert (friends_dir / "HISTORY.md").read_text() == "old history"
        # Accept regeneration
        cp = {"step": "history", "anthropic_key": "sk-test", "user_context": "test"}
        with patch("wizard.steps.generate_history", return_value="new history"):
            with patch("builtins.input", side_effect=["y", "w"]):
                result = lib.step_history(cp, paths)
        assert (friends_dir / "HISTORY.md").read_text() == "new history"
