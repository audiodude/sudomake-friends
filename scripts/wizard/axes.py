"""Difference-profile sampling for friend candidate generation.

Diversity is enforced here, in code, not requested in prompts. Each candidate
gets an assigned profile sampled from orthogonal axis pools; the LLM's job is
to make a believable human who fits it.
"""

import random

WORLDS = [
    "trades", "healthcare", "food service", "corporate/finance", "education",
    "arts", "science", "civic/government", "retail", "agriculture",
    "logistics", "hospitality", "tech",
]

TEMPERAMENTS = [
    "blunt", "sunny", "anxious", "stoic", "chaotic", "fussy", "intense",
    "laid-back", "competitive", "nurturing",
]

TECH_RELATIONSHIPS = [
    "indifferent normie", "curious amateur", "enthusiastic first adopter",
    "professional",
]

# label -> (min_age, max_age)
LIFE_STAGES = {
    "single and dating badly": (25, 38),
    "new parent": (28, 42),
    "divorced, rebuilding": (35, 52),
    "caretaking an aging parent": (38, 55),
    "empty-nester": (48, 62),
    "perpetual student": (24, 40),
    "recently relocated for a partner's job": (27, 45),
    "settled homeowner deep in renovation": (33, 50),
}

FRICTION_HOOKS = [
    "thinks AI is overhyped and says so",
    "lectures friends about money and savings rates",
    "judges your sleep schedule out loud",
    "has strong music opinions with actual credentials behind them",
    "asks you to explain your job and remains unimpressed",
    "thinks social media is rotting everyone's brain, including yours",
    "believes everyone should know how to fix their own car/sink/bike",
    "is openly competitive about fitness and notices when you skip",
    "considers takeout a moral failing and will say it",
    "thinks your hobbies are ways of avoiding real commitments",
    "corrects people's grammar and facts mid-conversation",
    "evangelizes a lifestyle change (cold plunges, no-phone Sundays, whatever's current)",
]


def _draw(pool: list, count: int, used: set, rng: random.Random) -> list:
    """Draw `count` values from `pool`, avoiding `used`, no repeats until the
    pool is exhausted, then cycling as evenly as possible."""
    available = [v for v in pool if v not in used]
    rng.shuffle(available)
    out = available[:count]
    while len(out) < count:
        refill = list(pool)
        rng.shuffle(refill)
        out.extend(refill[: count - len(out)])
    return out[:count]


def sample_profiles(count: int, used_axes: list[dict] | None = None,
                    rng: random.Random | None = None) -> list[dict]:
    """Sample `count` difference profiles, avoiding axis values already used
    by held/existing candidates (pass their `axes` dicts as `used_axes`)."""
    rng = rng or random.Random()
    used = used_axes or []

    def used_vals(key):
        return {u.get(key) for u in used if u.get(key)}

    worlds = _draw(WORLDS, count, used_vals("world"), rng)
    temps = _draw(TEMPERAMENTS, count, used_vals("temperament"), rng)
    techs = _draw(TECH_RELATIONSHIPS, count, used_vals("tech_relationship"), rng)
    stages = _draw(list(LIFE_STAGES), count, used_vals("life_stage"), rng)
    hooks = _draw(FRICTION_HOOKS, count, used_vals("friction_hook"), rng)

    return [
        {
            "world": worlds[i],
            "temperament": temps[i],
            "tech_relationship": techs[i],
            "life_stage": stages[i],
            "age_range": list(LIFE_STAGES[stages[i]]),
            "friction_hook": hooks[i],
        }
        for i in range(count)
    ]


def render_profile(p: dict, n: int) -> str:
    lo, hi = p["age_range"]
    return (
        f"Profile {n}: works in {p['world']} "
        f"(relationship to tech: {p['tech_relationship']}); "
        f"temperament: {p['temperament']}; "
        f"life stage: {p['life_stage']} (age {lo}-{hi}); "
        f"friction with the user: {p['friction_hook']}"
    )
