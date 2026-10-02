"""The poll bot reads ALL group messages, so it must have Telegram privacy
mode OFF. Selection must prefer a privacy-off friend and warn loudly when none
qualify (the silent-friends failure mode from the 2026-06-26 incident)."""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

# project root on path so `import src.bot` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import FriendGroup


def _fake_bot(name, can_read):
    return SimpleNamespace(
        name=name,
        username=f"tm_{name}_bot",
        can_read_all_group_messages=can_read,
    )


def _group_with(bots):
    g = FriendGroup.__new__(FriendGroup)  # skip the heavy __init__ (config + LLM client)
    g.bots = {b.name: b for b in bots}
    return g


def test_prefers_privacy_off_bot_over_first():
    # First friend has privacy ON; a later one is OFF — must elect the OFF one.
    g = _group_with([
        _fake_bot("jordan", False),
        _fake_bot("quinn", True),
        _fake_bot("robin", True),
    ])
    assert g._select_poll_bot().name == "quinn"


def test_picks_first_privacy_off_when_several_qualify():
    g = _group_with([
        _fake_bot("alex", True),
        _fake_bot("casey", True),
    ])
    assert g._select_poll_bot().name == "alex"


def test_warns_and_falls_back_when_all_privacy_on(caplog):
    g = _group_with([
        _fake_bot("jordan", False),
        _fake_bot("quinn", False),
    ])
    with caplog.at_level(logging.WARNING):
        chosen = g._select_poll_bot()
    # Falls back to the first friend so the bot still runs...
    assert chosen.name == "jordan"
    # ...but warns loudly with actionable guidance.
    warnings = " ".join(r.getMessage() for r in caplog.records
                        if r.levelno >= logging.WARNING).lower()
    assert "privacy" in warnings
    assert "re-add" in warnings
