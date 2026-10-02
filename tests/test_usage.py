"""Billing logs must distinguish unknown charges from explicitly free calls."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.usage import log_usage


def test_missing_charge_is_not_reported_as_free(caplog):
    with caplog.at_level(logging.INFO, logger="src.usage"):
        log_usage("decide:alex", "provider/new-model", {
            "prompt_tokens": 1000, "completion_tokens": 200,
        })
    line = caplog.records[-1].getMessage()
    assert "cost=unknown" in line
    assert "$0" not in line


def test_explicitly_free_call_is_not_unknown(caplog):
    with caplog.at_level(logging.INFO, logger="src.usage"):
        log_usage("decide:alex", "provider/free-model", {"cost": 0})
    line = caplog.records[-1].getMessage()
    assert "cost=$0.000000" in line
    assert "unknown" not in line
