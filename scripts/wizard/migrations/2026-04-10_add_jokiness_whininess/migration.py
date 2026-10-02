"""Migration: add jokiness and whininess personality dials to existing friends.

Two new float fields in config.yaml and candidate.json that shape HOW a
friend writes (not whether they write). They compose with the existing
chattiness dial.

Users get three strategies to pick values:
  - LLM infer: read each friend's SOUL.md and ask OpenRouter to propose tuned values
  - Manual: prompt for each friend's values individually
  - Defaults: blanket 0.5 / 0.3 for everyone
"""

import json
import os
from pathlib import Path

from wizard.migrations import helpers
from src.llm import DEFAULT_MODEL
from wizard.paths import load_env


ID = "2026-04-10_add_jokiness_whininess"
TITLE = "Add jokiness and whininess personality dials"
MANDATORY = False
DESCRIPTION = """Adds two new personality dials to each friend's config:
  • jokiness  (0.0-1.0) — how often they reach for humor vs. plain/sincere
  • whininess (0.0-1.0) — how often they complain about things

These compose with chattiness to shape each friend's voice. You can either
infer values from each friend's SOUL.md via an LLM call, set them by hand,
or accept bland defaults for everyone."""

DEFAULT_JOKINESS = 0.5
DEFAULT_WHININESS = 0.3


def is_needed(friends_dir: Path) -> bool:
    for _name, path in helpers.iter_friends(friends_dir):
        cfg = helpers.load_friend_config(path)
        if "jokiness" not in cfg or "whininess" not in cfg:
            return True
    return False


def run(friends_dir: Path, interactive: bool = True) -> bool:
    friends = [(n, p) for n, p in helpers.iter_friends(friends_dir)]
    needing = []
    for name, path in friends:
        cfg = helpers.load_friend_config(path)
        if "jokiness" not in cfg or "whininess" not in cfg:
            needing.append((name, path))

    if not needing:
        print("  Nothing to do — all friends already have these fields.")
        return True

    print(f"  {len(needing)} friend(s) need updating:")
    for name, _ in needing:
        print(f"    • {name}")
    print()

    if not interactive:
        # Non-interactive: apply defaults
        for name, path in needing:
            _apply(path, DEFAULT_JOKINESS, DEFAULT_WHININESS)
        return True

    mode = helpers.prompt_choice(
        "Set values via [llm] infer from SOUL.md, [manual] per-friend, or use [defaults]?",
        ["llm", "manual", "defaults"],
        default="defaults",
    )

    if mode == "defaults":
        for name, path in needing:
            _apply(path, DEFAULT_JOKINESS, DEFAULT_WHININESS)
            print(f"    {name}: jokiness={DEFAULT_JOKINESS}, whininess={DEFAULT_WHININESS}")
        return True

    if mode == "manual":
        for name, path in needing:
            print(f"\n  — {name} —")
            j = helpers.prompt_float("jokiness", default=DEFAULT_JOKINESS)
            w = helpers.prompt_float("whininess", default=DEFAULT_WHININESS)
            _apply(path, j, w)
        return True

    # LLM mode
    return _run_llm_mode(needing)


def _apply(friend_path: Path, jokiness: float, whininess: float) -> None:
    helpers.update_config(friend_path, {"jokiness": jokiness, "whininess": whininess})
    helpers.update_candidate(friend_path, {"jokiness": jokiness, "whininess": whininess})


def _run_llm_mode(needing: list) -> bool:
    """Ask OpenRouter to tune each friend's dials from their SOUL.md, then confirm."""
    try:
        from wizard.llm import get_client
    except ImportError:
        print("  OpenRouter client not available. Falling back to manual mode.")
        for name, path in needing:
            print(f"\n  — {name} —")
            j = helpers.prompt_float("jokiness", default=DEFAULT_JOKINESS)
            w = helpers.prompt_float("whininess", default=DEFAULT_WHININESS)
            _apply(path, j, w)
        return True

    env_path = needing[0][1].parent.parent / ".env"
    client = get_client(env_path)
    if client is None:
        print("  No OPENROUTER_API_KEY found. Falling back to defaults.")
        for name, path in needing:
            _apply(path, DEFAULT_JOKINESS, DEFAULT_WHININESS)
        return True

    model = os.environ.get("OPENROUTER_MODEL") or load_env(env_path).get("OPENROUTER_MODEL") or DEFAULT_MODEL
    proposed = {}
    try:
        for name, path in needing:
            soul = helpers.load_friend_soul(path)
            print(f"  Inferring dials for {name}...")
            result = _llm_call(client, name, soul, model=model)
            if result is None:
                print(f"    LLM failed for {name}; using defaults.")
                proposed[name] = (DEFAULT_JOKINESS, DEFAULT_WHININESS, "fallback")
            else:
                proposed[name] = result
    finally:
        client.close()

    print()
    print("  Proposed values:")
    print(f"  {'friend':<12} {'jokiness':<10} {'whininess':<10} reasoning")
    print("  " + "─" * 60)
    for name, (j, w, reason) in proposed.items():
        print(f"  {name:<12} {j:<10} {w:<10} {reason[:40]}")
    print()

    choice = helpers.prompt_choice(
        "[a]ccept all, [e]dit individually, or [c]ancel?",
        ["a", "e", "c"],
        default="a",
    )
    if choice == "c":
        print("  Cancelled.")
        return False

    path_by_name = {n: p for n, p in needing}
    if choice == "a":
        for name, (j, w, _) in proposed.items():
            _apply(path_by_name[name], j, w)
        return True

    # Edit
    for name, (j, w, _) in proposed.items():
        print(f"\n  — {name} —")
        j2 = helpers.prompt_float("jokiness", default=j)
        w2 = helpers.prompt_float("whininess", default=w)
        _apply(path_by_name[name], j2, w2)
    return True


def _llm_call(client, name: str, soul_text: str, *, model: str = DEFAULT_MODEL):
    """Ask OpenRouter for tuned (jokiness, whininess) values based on a friend's SOUL."""
    prompt = f"""You are tuning personality dials for a character named {name} in a chat app.

Here is their personality:
---
{soul_text[:4000]}
---

Return two floats in [0.0, 1.0]:
- "jokiness": how often they reach for humor vs. being plain/sincere. Low = dry, literal, earnest. High = playful, quippy.
- "whininess": how often they complain. Low = stoic/positive, high = often venting.

Also return a very short "reasoning" (one short phrase, under 40 chars).

Respond with ONLY a JSON object, no fencing:
{{"jokiness": 0.X, "whininess": 0.Y, "reasoning": "..."}}
"""
    try:
        response = client.complete(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
            label=f"wizard:dials:{name}",
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        j = float(data["jokiness"])
        w = float(data["whininess"])
        reason = str(data.get("reasoning", ""))
        return (max(0.0, min(1.0, j)), max(0.0, min(1.0, w)), reason)
    except Exception as e:
        print(f"    LLM error: {e}")
        return None
