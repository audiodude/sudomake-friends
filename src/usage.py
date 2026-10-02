"""Log OpenRouter's returned token usage and charged cost for each LLM call."""

import logging

logger = logging.getLogger(__name__)


def log_usage(label: str, model: str, usage: dict) -> None:
    """Missing cost is unknown, not free; prompt tokens include cached tokens."""
    details = usage.get("prompt_tokens_details") or {}
    cost = usage.get("cost")
    charged = f"${float(cost):.6f}" if cost is not None else "unknown"
    logger.info(
        "[usage] %-16s model=%s in=%d cache_read=%d cache_write=%d out=%d cost=%s",
        label, model, usage.get("prompt_tokens") or 0,
        details.get("cached_tokens") or 0, details.get("cache_write_tokens") or 0,
        usage.get("completion_tokens") or 0, charged,
    )
