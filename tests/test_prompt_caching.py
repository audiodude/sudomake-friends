"""The reply prompt is split into a per-call context half and a cached rules
half. The rules half must be byte-identical for every friend (no per-call
placeholders) or the shared cache prefix breaks; the context half must still
format with the same call kwargs.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain

PLACEHOLDER = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")


def test_rules_have_no_per_call_placeholders():
    # A {name} etc. here would make each friend a distinct cache prefix.
    assert PLACEHOLDER.findall(brain._DECIDE_RULES) == []


def test_context_formats_with_the_call_kwargs():
    keys = set(PLACEHOLDER.findall(brain._DECIDE_CONTEXT))
    out = brain._DECIDE_CONTEXT.format(**{k: f"<{k}>" for k in keys})
    assert "<name>" in out and "<chat_context>" in out


def test_split_is_clean():
    assert "===RULES===" not in brain._DECIDE_CONTEXT
    assert "===RULES===" not in brain._DECIDE_RULES
    assert brain._DECIDE_CONTEXT.strip() and brain._DECIDE_RULES.strip()


def test_missing_sentinel_raises():
    try:
        brain._split_cached_prompt("no boundary here")
    except ValueError:
        return
    raise AssertionError("expected ValueError when the sentinel is absent")
