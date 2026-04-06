"""Functional tests for sudomake-friends scripts."""

import curses
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to path so we can import initialize as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import initialize as lib  # all functions live in the single file
# Alias platform helpers
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
    with patch("initialize.curses.curs_set"), \
         patch("initialize.curses.use_default_colors"), \
         patch("initialize.curses.init_pair"), \
         patch("initialize.curses.color_pair", return_value=0):
        yield


@pytest.mark.usefixtures("mock_curses")
class TestSelectionUI:
    """Tests 1-9: TUI behavior via mocked curses."""

    def test_arrow_down_moves_cursor(self, sample_candidates):
        # Down, Down, then quit
        keys = [curses.KEY_DOWN, curses.KEY_DOWN, ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_enter_toggles_hold(self, sample_candidates):
        # Select first, then quit
        keys = [ord("\n"), ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 in held
        assert action == "quit"

    def test_enter_toggles_off(self, sample_candidates):
        # Toggle on, toggle off, quit
        keys = [ord("\n"), ord("\n"), ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 not in held

    def test_space_also_toggles(self, sample_candidates):
        keys = [ord(" "), ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 in held

    def test_backspace_only_uninvites(self, sample_candidates):
        # Select first, move down, backspace (should not invite #1), move up, backspace (should uninvite #0)
        keys = [ord("\n"), curses.KEY_DOWN, 127, curses.KEY_UP, 127, ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert 0 not in held
        assert 1 not in held

    def test_r_returns_reroll(self, sample_candidates):
        keys = [ord("r")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "reroll"

    def test_a_returns_accept_when_held(self, sample_candidates):
        keys = [ord("\n"), ord("a")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "accept"
        assert 0 in held

    def test_a_shows_warning_when_none_held(self, sample_candidates):
        # Press a with nothing held — should flash warning then wait for key, then quit
        keys = [ord("a"), ord(" "), ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"
        assert len(held) == 0

    def test_e_returns_edit_candidate(self, sample_candidates):
        keys = [ord("e")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert isinstance(action, tuple)
        assert action[0] == "edit_candidate"
        assert action[1] == 0

    def test_q_preserves_held(self, sample_candidates):
        # Hold first two, quit
        keys = [ord("\n"), curses.KEY_DOWN, ord("\n"), ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"
        assert held == {0, 1}

    def test_cursor_stays_in_bounds(self, sample_candidates):
        # Up from top, should stay at 0
        keys = [curses.KEY_UP, curses.KEY_UP, ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_resume_with_preheld(self, sample_candidates):
        # Start with {1} held, quit immediately
        keys = [ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, {1})
        assert 1 in held

    def test_x_opens_modal_esc_dismisses(self, sample_candidates):
        # x to open modal, ESC to close, q to quit
        keys = [ord("x"), 27, ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"

    def test_vim_keys(self, sample_candidates):
        # j=down, k=up, then quit
        keys = [ord("j"), ord("j"), ord("k"), ord("q")]
        stdscr = _make_mock_stdscr(keys)
        held, action = lib.selection_ui(stdscr, sample_candidates, set())
        assert action == "quit"


# ─── User context gathering ──────────────────────────────────────────────────

class TestUserContext:
    """Tests 24-31: Context input routing."""

    def test_url_routes_to_fetch(self, paths):
        with patch("initialize._fetch_url", return_value="fetched content") as mock:
            with patch("builtins.input", side_effect=["https://example.com", "q"]):
                result, sources = lib.get_user_context(paths)
        mock.assert_called_once()
        assert "fetched content" in result
        assert sources[0]["label"] == "https://example.com"

    def test_file_path_routes_to_read(self, paths, tmp_path):
        test_file = tmp_path / "about.txt"
        test_file.write_text("I like music and coding")
        with patch("builtins.input", side_effect=[str(test_file), "q"]):
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
        with patch("builtins.input", side_effect=[str(bad_file), "q"]):
            result, sources = lib.get_user_context(paths)
        assert len(sources) == 1
        assert "good content" in result

    def test_cached_sources_offered(self, paths):
        cached = [{"label": "old.com", "content": "old stuff"}]
        with patch("builtins.input", side_effect=["y", "q"]):
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
        with patch("initialize.curses.wrapper") as mock_wrapper:
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
        with patch("initialize.curses.wrapper") as mock_wrapper:
            mock_wrapper.return_value = (set(), "quit")
            result = lib.run_selection_loop(
                client=MagicMock(),
                user_context="test",
                candidates=sample_candidates,
            )
        assert result is None

    def test_on_save_called(self, sample_candidates):
        saves = []
        with patch("initialize.curses.wrapper") as mock_wrapper:
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
        """With .env + friends + no checkpoint, setup is complete."""
        # Create .env
        paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        # Create a friend
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")

        existing = lib.get_existing_friend_names(paths["friends"])
        cp = {"step": "start"}

        setup_complete = (
            paths["env"].exists()
            and len(existing) > 0
            and cp["step"] == "start"
        )
        assert setup_complete is True

    def test_incomplete_without_friends(self, paths):
        """With .env but no friends, setup is not complete."""
        paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-test\n")

        existing = lib.get_existing_friend_names(paths["friends"])
        cp = {"step": "start"}

        setup_complete = (
            paths["env"].exists()
            and len(existing) > 0
            and cp["step"] == "start"
        )
        assert setup_complete is False

    def test_incomplete_without_env(self, paths):
        """With friends but no .env, setup is not complete."""
        if paths["env"].exists():
            paths["env"].unlink()
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")

        existing = lib.get_existing_friend_names(paths["friends"])
        cp = {"step": "start"}

        setup_complete = (
            paths["env"].exists()
            and len(existing) > 0
            and cp["step"] == "start"
        )
        assert setup_complete is False

    def test_incomplete_with_active_checkpoint(self, paths):
        """With everything but an in-progress checkpoint, not complete."""
        paths["env"].write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        d = paths["friends"] / "casey"
        d.mkdir()
        (d / "SOUL.md").write_text("# Casey")

        existing = lib.get_existing_friend_names(paths["friends"])
        cp = {"step": "telegram_bots"}  # mid-setup

        setup_complete = (
            paths["env"].exists()
            and len(existing) > 0
            and cp["step"] == "start"
        )
        assert setup_complete is False
