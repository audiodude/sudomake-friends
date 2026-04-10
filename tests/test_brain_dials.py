"""Regression tests for brain._describe_dials — ensures friends with legacy
configs (missing jokiness/whininess) still render correctly at runtime.

This is the safety net that lets the migration system ship additive field
changes without breaking existing deployments: even if a user hasn't run
the migration yet, the bot must not crash."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brain import _describe_dials


class TestDescribeDials:
    def test_handles_config_missing_both_fields(self):
        text = _describe_dials({})
        assert "Jokiness" in text
        assert "Whininess" in text
        assert text.count("\n") >= 1

    def test_handles_only_jokiness_present(self):
        text = _describe_dials({"jokiness": 0.8})
        assert "0.8" in text
        assert "Whininess" in text  # default kicks in

    def test_handles_only_whininess_present(self):
        text = _describe_dials({"whininess": 0.9})
        assert "0.9" in text
        assert "Jokiness" in text

    def test_low_jokiness_says_dry(self):
        text = _describe_dials({"jokiness": 0.1, "whininess": 0.5})
        assert "dry" in text.lower() or "literal" in text.lower() or "sincere" in text.lower()

    def test_high_jokiness_warns_against_bits(self):
        text = _describe_dials({"jokiness": 0.9, "whininess": 0.5})
        # The high-jokiness guidance explicitly bans setup-punchline comedy
        assert "bit" in text.lower() or "setup-punchline" in text.lower() or "stand-up" in text.lower()

    def test_high_whininess_mentions_variety(self):
        text = _describe_dials({"jokiness": 0.5, "whininess": 0.9})
        assert "vary" in text.lower() or "different" in text.lower() or "subject" in text.lower()

    def test_full_legacy_config_doesnt_crash(self):
        # A config shape that matches what existing friends had before the migration
        legacy = {
            "timezone": "America/New_York",
            "chattiness": 0.5,
            "bot_reply_chance": 0.3,
            "schedule": {
                "wake_up": "08:00",
                "sleep_at": "23:00",
                "work_start": "09:00",
                "work_end": "17:00",
                "days_off": [5, 6],
            },
        }
        text = _describe_dials(legacy)
        assert text  # non-empty
        assert "\n" in text
