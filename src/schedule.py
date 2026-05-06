"""Simulate realistic availability based on timezone and schedule."""

import random
from datetime import datetime
from zoneinfo import ZoneInfo


def get_availability(friend_config: dict) -> dict:
    """Return availability info for a friend right now.

    Returns dict with:
        - awake: bool
        - at_work: bool
        - day_off: bool
        - responsiveness: float 0.0-1.0 (how likely to respond)
    """
    tz_key = friend_config.get("timezone", "UTC").replace(" ", "_")
    tz = ZoneInfo(tz_key)
    now = datetime.now(tz)
    hour_min = now.strftime("%H:%M")
    weekday = now.weekday()

    schedule = friend_config.get("schedule", {})
    wake_up = schedule.get("wake_up", "07:00")
    sleep_at = schedule.get("sleep_at", "23:00")
    work_start = schedule.get("work_start", "09:00")
    work_end = schedule.get("work_end", "17:00")
    days_off = schedule.get("days_off", [5, 6])

    awake = wake_up <= hour_min < sleep_at
    day_off = weekday in days_off
    at_work = not day_off and work_start <= hour_min < work_end

    chattiness = friend_config.get("chattiness", 0.5)

    # work_type: "office" can check phone at desk, "physical" can't
    work_type = friend_config.get("work_type", "office")

    if not awake:
        # Asleep — very unlikely to respond, but phones buzz sometimes
        responsiveness = 0.02
    elif at_work:
        # Office workers can sneak a text; physical workers mostly can't
        if work_type == "office":
            responsiveness = chattiness * 0.5
        else:
            responsiveness = chattiness * 0.1
    elif day_off:
        # Day off — more available
        responsiveness = chattiness * 1.1
    else:
        # Awake, not working
        responsiveness = chattiness * 0.85

    # Clamp
    responsiveness = max(0.0, min(1.0, responsiveness))

    return {
        "awake": awake,
        "at_work": at_work,
        "day_off": day_off,
        "responsiveness": responsiveness,
        "local_time": now.strftime("%A %H:%M %Z"),
    }


def should_respond(friend_config: dict, is_bot_message: bool = False,
                   mentioned: bool = False,
                   engagement_modifier: float = 0.0) -> bool:
    """Decide if this friend should respond right now based on schedule.

    engagement_modifier: 0.0-0.8 boost from recent conversation momentum.
    This is the first gate — personality/relevance is checked separately by the LLM.
    """
    availability = get_availability(friend_config)
    responsiveness = availability["responsiveness"]

    if mentioned:
        # Being addressed directly — much more likely to respond
        if not availability["awake"]:
            responsiveness = 0.15
        elif availability["at_work"]:
            work_type = friend_config.get("work_type", "office")
            responsiveness = 0.8 if work_type == "office" else 0.4
        else:
            responsiveness = 0.95
    elif is_bot_message:
        bot_reply_chance = friend_config.get("bot_reply_chance", 0.75)
        responsiveness *= bot_reply_chance
    else:
        responsiveness *= 0.75

    # Conversation momentum — engaged bots are more likely to keep going
    responsiveness = min(0.95, responsiveness + engagement_modifier)

    return random.random() < responsiveness
