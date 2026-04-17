import json
from pathlib import Path

import yaml

from wizard.claude import generate_soul


def generate_souls_for_selected(
    client,
    selected: list[dict],
    user_context: str,
    friends_dir: Path,
    cached_souls: dict | None = None,
    on_save_soul=None,
) -> dict:
    """Generate SOUL.md for each selected candidate. Returns {name: soul_text}."""
    souls = dict(cached_souls or {})

    print("\n  Generating detailed personalities...")
    for c in selected:
        slug = c["name"].lower().replace(" ", "_")
        existing_soul = friends_dir / slug / "SOUL.md"
        if existing_soul.exists():
            souls[c["name"]] = existing_soul.read_text()
            print(f"    {c['name']}'s soul exists on disk (keeping)")
        elif c["name"] in souls:
            print(f"    {c['name']}'s soul already generated")
        else:
            print(f"    Writing {c['name']}'s soul...", end="", flush=True)
            souls[c["name"]] = generate_soul(client, c, selected, user_context)
            print(" done")
        if on_save_soul:
            on_save_soul(c["name"], souls[c["name"]])

    return souls


def _validate_timezone(tz_str: str, location: str = "") -> str:
    """Validate a timezone string. Try to fix it, fall back to America/New_York."""
    from zoneinfo import ZoneInfo, available_timezones

    # Basic cleanup
    tz_str = tz_str.strip().replace(" ", "_")

    # Try it directly
    try:
        ZoneInfo(tz_str)
        return tz_str
    except (KeyError, Exception):
        pass

    # Try common fixes
    for prefix in ["America/", "Europe/", "Asia/", "Australia/", "Pacific/", "Africa/"]:
        candidate = prefix + tz_str.split("/")[-1]
        try:
            ZoneInfo(candidate)
            return candidate
        except (KeyError, Exception):
            continue

    # Try to guess from location
    if location:
        loc_lower = location.lower()
        # Map common cities/regions to timezones
        city_map = {
            "new york": "America/New_York", "nyc": "America/New_York",
            "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
            "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
            "chicago": "America/Chicago", "denver": "America/Denver",
            "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles",
            "boston": "America/New_York", "philadelphia": "America/New_York",
            "austin": "America/Chicago", "houston": "America/Chicago",
            "atlanta": "America/New_York", "miami": "America/New_York",
            "london": "Europe/London", "berlin": "Europe/Berlin",
            "paris": "Europe/Paris", "amsterdam": "Europe/Amsterdam",
            "tokyo": "Asia/Tokyo", "sydney": "Australia/Sydney",
            "toronto": "America/Toronto", "vancouver": "America/Vancouver",
            "mexico city": "America/Mexico_City",
        }
        for city, tz in city_map.items():
            if city in loc_lower:
                return tz

    return "America/New_York"


def create_friend_dir(friends_dir: Path, name: str, soul: str,
                      candidate: dict) -> str:
    slug = name.lower().replace(" ", "_")
    friend_dir = friends_dir / slug
    friend_dir.mkdir(parents=True, exist_ok=True)

    (friend_dir / "SOUL.md").write_text(soul)

    config = {
        "timezone": _validate_timezone(
            candidate.get("timezone", "America/New_York"),
            candidate.get("location", ""),
        ),
        "schedule": {
            "wake_up": "08:00",
            "sleep_at": "23:00",
            "work_start": "09:00",
            "work_end": "17:00",
            "days_off": [5, 6],
        },
        "chattiness": candidate.get("chattiness", 0.5),
        "jokiness": candidate.get("jokiness", 0.5),
        "whininess": candidate.get("whininess", 0.3),
        "bot_reply_chance": 0.3,
    }
    with open(friend_dir / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    # Save candidate JSON for later editing in TUI
    with open(friend_dir / "candidate.json", "w") as f:
        json.dump(candidate, f, indent=2)

    return slug


def get_existing_friend_names(friends_dir: Path) -> list[str]:
    """Return names of friends that already have SOUL.md files."""
    names = []
    if friends_dir.exists():
        for d in sorted(friends_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "SOUL.md").exists():
                names.append(d.name)
    return names
