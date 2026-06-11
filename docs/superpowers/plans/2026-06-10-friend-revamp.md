# Friend Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce personality spread in friend generation via Python-sampled difference profiles, split models (Opus for generation, Sonnet 4.6 for chat), and add friction requirements to souls and runtime prompts.

**Architecture:** A new `scripts/wizard/axes.py` module deterministically samples orthogonal axis values (world, temperament, tech relationship, life stage, friction hook) per candidate before any LLM call. `generate_candidates` embeds the assigned profiles in its prompt and attaches them to returned candidates as `axes`, which flow into `candidate.json` and the soul prompt. Prompt updates in `claude.py` (foil notes, friction-laden souls) and `brain.py` (let friction breathe) complete the loop.

**Tech Stack:** Python 3.12, pytest, Anthropic SDK. Spec: `docs/superpowers/specs/2026-06-10-friend-revamp-design.md`.

**Conventions:** Tests run with `uv run pytest`. Test files import wizard modules via `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))` (see `tests/test_initialize_bootstrap.py`). All commits on `dev`. Do NOT deploy — the user re-runs setup manually as the acceptance test.

---

### Task 1: Model split

**Files:**
- Modify: `scripts/wizard/claude.py:7`
- Modify: `config.yaml:3`
- Modify: `src/config.py:89`

- [ ] **Step 1: Update the wizard model**

In `scripts/wizard/claude.py` line 7:

```python
MODEL = "claude-opus-4-8"
```

This covers candidates, souls, profile compilation, and HISTORY.md generation (`steps.py` imports this same `MODEL`).

- [ ] **Step 2: Update the runtime chat model**

In `config.yaml` line 3:

```yaml
model: "claude-sonnet-4-6"
```

- [ ] **Step 3: Fix the malformed fallback**

In `src/config.py` line 89, replace `claude-sonnet-4-6-20250514` (a model ID that doesn't exist — 4.6 name with a stale date suffix) with:

```python
        self.model = self.global_config.get("model", "claude-sonnet-4-6")
```

- [ ] **Step 4: Verify nothing else references old model IDs**

Run: `grep -rn "claude-4-sonnet\|claude-sonnet-4-6-20250514" --include="*.py" --include="*.yaml" . | grep -v .venv | grep -v docs/`
Expected: no output. (`src/nag_detector.py` uses Haiku — that stays.)

- [ ] **Step 5: Run existing tests, then commit**

Run: `uv run pytest tests/ -q`
Expected: all pass.

```bash
git add scripts/wizard/claude.py config.yaml src/config.py
git commit -m "Split models: Opus 4.8 for generation, Sonnet 4.6 for chat"
```

---

### Task 2: Axis sampling module

**Files:**
- Create: `scripts/wizard/axes.py`
- Test: `tests/test_axes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_axes.py`:

```python
"""Tests for difference-profile axis sampling."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from wizard.axes import (
    WORLDS, TEMPERAMENTS, TECH_RELATIONSHIPS, LIFE_STAGES, FRICTION_HOOKS,
    sample_profiles, render_profile,
)

AXIS_KEYS = {"world", "temperament", "tech_relationship", "life_stage",
             "age_range", "friction_hook"}


class TestSampleProfiles:
    def test_returns_count_profiles_with_all_keys(self):
        profiles = sample_profiles(8, rng=random.Random(1))
        assert len(profiles) == 8
        for p in profiles:
            assert AXIS_KEYS == set(p.keys())

    def test_large_pools_have_no_duplicates_in_batch(self):
        profiles = sample_profiles(8, rng=random.Random(2))
        for key in ("world", "temperament", "life_stage", "friction_hook"):
            values = [p[key] for p in profiles]
            assert len(values) == len(set(values)), f"duplicate {key}: {values}"

    def test_small_pool_spreads_evenly(self):
        # TECH_RELATIONSHIPS has 4 values; 8 candidates -> each used exactly twice
        profiles = sample_profiles(8, rng=random.Random(3))
        values = [p["tech_relationship"] for p in profiles]
        for v in set(values):
            assert values.count(v) == 2

    def test_at_most_one_tech_world_per_batch(self):
        for seed in range(20):
            profiles = sample_profiles(8, rng=random.Random(seed))
            tech_count = sum(1 for p in profiles if p["world"] == "tech")
            assert tech_count <= 1

    def test_exclusions_respected(self):
        used = [{
            "world": "healthcare", "temperament": "blunt",
            "tech_relationship": "professional",
            "life_stage": "new parent", "age_range": [28, 42],
            "friction_hook": FRICTION_HOOKS[0],
        }]
        profiles = sample_profiles(5, used_axes=used, rng=random.Random(4))
        for p in profiles:
            assert p["world"] != "healthcare"
            assert p["temperament"] != "blunt"
            assert p["life_stage"] != "new parent"
            assert p["friction_hook"] != FRICTION_HOOKS[0]

    def test_age_range_matches_life_stage(self):
        profiles = sample_profiles(8, rng=random.Random(5))
        for p in profiles:
            assert p["age_range"] == list(LIFE_STAGES[p["life_stage"]])

    def test_pool_exhaustion_still_returns_count(self):
        # Exclude every life stage -> module must cycle, not crash
        used = [{"life_stage": s} for s in LIFE_STAGES]
        profiles = sample_profiles(3, used_axes=used, rng=random.Random(6))
        assert len(profiles) == 3


class TestRenderProfile:
    def test_renders_all_axes(self):
        p = sample_profiles(1, rng=random.Random(7))[0]
        text = render_profile(p, 1)
        assert p["world"] in text
        assert p["temperament"] in text
        assert p["tech_relationship"] in text
        assert p["life_stage"] in text
        assert p["friction_hook"] in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_axes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wizard.axes'`

- [ ] **Step 3: Implement the module**

Create `scripts/wizard/axes.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_axes.py -q`
Expected: all PASS.

Note: `test_at_most_one_tech_world_per_batch` passes structurally — "tech"
appears once in WORLDS and `_draw` doesn't repeat until exhaustion, so a batch
of 8 from 13 worlds can contain at most one "tech".

- [ ] **Step 5: Commit**

```bash
git add scripts/wizard/axes.py tests/test_axes.py
git commit -m "Add axis sampling module for enforced personality spread"
```

---

### Task 3: Candidate generation uses assigned profiles

**Files:**
- Modify: `scripts/wizard/claude.py` (`generate_candidates`, lines 21-81)

- [ ] **Step 1: Rewrite generate_candidates**

Replace the whole function with:

```python
def generate_candidates(client, context: str, held: list[dict],
                        existing_friends: list[str] | None = None,
                        count: int = CANDIDATE_COUNT) -> list[dict]:
    from wizard.axes import sample_profiles, render_profile

    used_axes = [h["axes"] for h in held if h.get("axes")]
    profiles = sample_profiles(count, used_axes=used_axes)
    profiles_desc = "\n".join(render_profile(p, i + 1)
                              for i, p in enumerate(profiles))

    held_desc = ""
    if held:
        held_desc = f"""
These friends have already been selected in this session (do NOT regenerate them;
new candidates should be clearly DIFFERENT people from them):
{json.dumps([{k: v for k, v in h.items() if k != "axes"} for h in held], indent=2)}
"""
    existing_desc = ""
    if existing_friends:
        existing_desc = f"""
These friends ALREADY EXIST in the group (do NOT regenerate them):
{', '.join(existing_friends)}
"""

    prompt = f"""Generate exactly {count} fictional friend candidates for a virtual group
chat. Each candidate has an ASSIGNED PROFILE below. Your job is to invent a
believable, specific human who fits their profile — NOT to invent people
similar to the user.

## Assigned profiles (one candidate each, in order)
{profiles_desc}

A friend is NOT someone with the same interests and temperament as you. Real
friends come from life collisions — an old job, a college roommate, a neighbor,
a friend's ex — and stick around because of who they are, not what they're into.
The user's profile below is context for the "how_met" field and for aiming each
candidate's friction hook. DO NOT mirror the user's interests, job, vocabulary,
or vibe. If the user is a tech person, candidates in non-tech worlds should
know roughly nothing about tech and not care.

Return ONLY a JSON array of {count} objects, in the SAME ORDER as the assigned
profiles. Each object must have:
- "name": first name only, capitalized (prefer gender-neutral names)
- "traits": array of 3-4 personality trait words consistent with the assigned temperament
- "age": integer WITHIN the assigned age range
- "location": city and region (vary these — not everyone lives in a coastal US city)
- "occupation": a specific job within the assigned world (e.g. world "trades" -> "elevator mechanic", not "tradesperson")
- "vibe": 1-3 sentences about who they are as a PERSON. Show traits through behavior, not adjectives.
- "how_met": one sentence — the life collision through which they know the user (an old job, school, a neighbor, a wedding, jury duty...). NOT shared interests.
- "friction": one sentence making the assigned friction hook concrete and personal to this character
- "timezone": IANA timezone string
- "chattiness": float 0.0-1.0 — how often they respond / initiate
- "jokiness": float 0.0-1.0 — low = dry/literal/earnest, high = playful/quippy (NEVER setup-punchline bit comedy)
- "whininess": float 0.0-1.0 — low = stoic/positive, high = often venting

Vary the dials meaningfully across candidates — a stoic tradesperson and a
chaotic line cook should not have the same numbers.
{held_desc}{existing_desc}
## Profile of the user (context only — do NOT mirror it)
{context}

JSON array only, no markdown fencing:"""

    response = client.messages.create(
        model=MODEL, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    candidates = json.loads(raw)
    for cand, prof in zip(candidates, profiles):
        cand["axes"] = prof
    return candidates
```

Notes for the implementer:
- The `axes` key rides along in the candidate dict: checkpoint saves it,
  `create_friend_dir` already dumps the full candidate to `candidate.json`,
  and `text_to_candidate` (`scripts/wizard/editor.py:55`) copies the original
  dict so TUI edits preserve it. No changes needed in those files.
- The old `"why"` field is replaced by `"how_met"` + `"friction"`. Display
  references to update: `scripts/wizard/tui.py:38` renders
  `_wrap(f"Why: {candidate.get('why', '')}")` — replace with two lines:
  `lines.extend(_wrap(f"How met: {candidate.get('how_met', '')}"))` and
  `lines.extend(_wrap(f"Friction: {candidate.get('friction', '')}"))`.
  `steps.py:186` prints `c['vibe']` (fine, no change). `editor.py` changes
  are Step 2 below.

- [ ] **Step 2: Update editor field mapping**

In `scripts/wizard/editor.py`, `candidate_to_text`: replace the `Why:` line with:

```python
        f"How met: {c.get('how_met', '')}",
        f"Friction: {c.get('friction', '')}",
```

In `text_to_candidate`, replace the `elif key == "why":` branch with:

```python
        elif key == "how met":
            c["how_met"] = value
        elif key == "friction":
            c["friction"] = value
```

In `scripts/wizard/tui.py:38`, replace the `Why:` line as described in
Task 3 Step 1 notes.

- [ ] **Step 3: Run tests, sanity-check imports**

Run: `uv run pytest tests/ -q`
Expected: all pass.

Run: `cd scripts && uv run python -c "from wizard.claude import generate_candidates; from wizard.axes import sample_profiles; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add scripts/wizard/claude.py scripts/wizard/editor.py scripts/wizard/tui.py
git commit -m "Generate candidates from assigned difference profiles"
```

---

### Task 4: Profile compiler — foil notes

**Files:**
- Modify: `scripts/wizard/claude.py` (`compile_profile`, lines 183-216)

- [ ] **Step 1: Replace the chemistry-notes section of the prompt**

In `compile_profile`, replace the paragraph beginning `End the profile with a
section called "Friend chemistry notes"` (through the two example bullet lines)
with:

```python
    prompt = f"""Based on the following raw content about a person, write a concise but
detailed profile summary (300-500 words). Cover:
- Who they are (name, location, job)
- Their personality traits — not just interests, but HOW they are as a person
  (e.g. sarcastic, earnest, self-deprecating, intense, goofy, loyal, anxious,
  competitive, nurturing, blunt, etc.)
- Their humor style (dry, absurdist, punny, dark, wholesome, etc.)
- Interests, hobbies, creative pursuits
- Social style and communication preferences

End the profile with a section called "Foil notes" that describes what this
person is NOT — the raw material for friends who contrast with them rather
than mirror them. List 4-6 bullets covering:
- Blind spots: worlds they know nothing about (job sectors, lifestyles, places)
- Taste boundaries: things they don't like or care about that others love
- Arguable opinions: stances a friend could genuinely push back on
- Temperament gaps: energies missing from their own personality (e.g. if
  they're an overthinker, a decisive blunt friend is a foil)

Be specific — names, places, projects, preferences. This profile will be used
to generate fictional friend characters who CONTRAST with this person, so the
Foil notes matter as much as the description.

Raw content:
{raw_context[:20000]}

Write the profile directly, no preamble:"""
```

(The rest of the function — the API call — is unchanged.)

- [ ] **Step 2: Run tests and commit**

Run: `uv run pytest tests/ -q`
Expected: all pass.

```bash
git add scripts/wizard/claude.py
git commit -m "Profile compiler: replace chemistry notes with foil notes"
```

---

### Task 5: Soul prompt — friction requirements

**Files:**
- Modify: `scripts/wizard/claude.py` (`generate_soul`, lines 84-180)

- [ ] **Step 1: Add axes context and friction requirements to the prompt**

In `generate_soul`, after the `traits_str` line, add:

```python
    axes = candidate.get("axes", {})
    friction_hook = axes.get("friction_hook") or candidate.get("friction", "")
    how_met = candidate.get("how_met", "")
```

Then make these prompt edits:

1. After the `## Their core personality traits:` block, add:

```
## Their friction with the user
{friction_hook}
This is a STANDING dynamic, not a one-off. It should surface in their
disagreements, their speech examples, and their boundaries.

## How they know the user
{how_met}
```

2. Replace the `CRITICAL INSTRUCTION` paragraph with:

```
CRITICAL INSTRUCTION: This is a WHOLE PERSON, not a walking job description.
Their occupation is ONE facet of who they are. You MUST flesh out their entire
life — where they grew up, their family, what they studied, what they eat,
what they watch, what they do on a lazy Sunday.

INTEREST EXCLUSIVITY: This character may share AT MOST ONE interest or
touchpoint with the user (described below). Everything else comes from the
character's own world. If the user likes coffee, farmers markets, vintage
shopping, or synthesizers, this character does NOT — unless that is their one
shared touchpoint. Friends are not interest-clones; they're people from
different worlds who stuck.

FRICTION IS REQUIRED: Real friendships contain standing disagreements. This
character must have:
- 2-3 concrete recurring disagreements with the user (the argument they've
  been having for years — about money, technology, lifestyle, music, food)
- 1-2 sincerely-held bad takes they will defend in chat (everyone has them)
These are affectionate friction, not hostility. The friend who is wrong about
something forever is more real than the friend who agrees with everything.
```

3. In the SOUL.md format template, after the `## Personality` section, add a new section:

```
## Friction
(REQUIRED: 2-3 standing disagreements with {{the user's name}}, each in one
sentence — what the argument is and this character's position. Then 1-2
sincerely-held bad takes they defend. These should be consistent with the
friction dynamic described above.)
```

4. In the `## Relationships` section description, replace the parenthetical with:

```
(how they relate to each of the other friends — include FRICTION, not just
harmony. Who do they tease? Whose lifestyle baffles them? Real groups have
edges. Also state how they met the user — use the life collision above.)
```

- [ ] **Step 2: Run tests and commit**

Run: `uv run pytest tests/ -q`
Expected: all pass.

```bash
git add scripts/wizard/claude.py
git commit -m "Soul prompt: require disagreements, bad takes, interest exclusivity"
```

---

### Task 6: Runtime — let friction breathe

**Files:**
- Modify: `src/brain.py` (DECIDE_AND_RESPOND_PROMPT, after the "Talk TO your friends" paragraph around line 168-170)

- [ ] **Step 1: Add the friction paragraph**

In `DECIDE_AND_RESPOND_PROMPT`, directly after the paragraph ending
`...sometimes the most natural response is to another friend.`, add:

```
DISAGREEING IS NORMAL. You don't have to find everything interesting or
relatable. "eh", "hard disagree", "that sounds miserable tbh", "you've been
saying this for years and you're still wrong" are all valid moves between
friends. Check your Friction section — your standing disagreements and bad
takes are part of who you are; defend them when they come up. Teasing,
pushing back, and being unimpressed are signs of a real friendship, not
rudeness. Do NOT manufacture agreement to be nice.
```

- [ ] **Step 2: Run tests and commit**

Run: `uv run pytest tests/ -q`
Expected: all pass.

```bash
git add src/brain.py
git commit -m "Runtime prompt: disagreeing and being unimpressed are normal moves"
```

---

### Task 7: Version bump + ship

**Files:**
- Modify: `README.md:3`

- [ ] **Step 1: Bump version**

`README.md` line 3: `**v1.5.0**` → `**v2.0.0**`

- [ ] **Step 2: Full test run**

Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 3: Commit and push dev, merge to main**

```bash
git add README.md
git commit -m "v2.0.0: axis-enforced friend generation, model split, friction"
git push origin dev
git checkout main && git merge --ff-only dev && git push origin main && git checkout dev
```

- [ ] **Step 4: STOP — manual acceptance test (user does this)**

Do NOT deploy or run the wizard automatically. The user will:
1. Retire one Telegram bot (4 → 3 friends)
2. Wipe chat history on their end
3. Run `scripts/initialize.py -- --start-over` from the dev checkout
4. Hold 3 candidates, review generated souls, deploy via the wizard

Acceptance criteria: the 3 generated friends occupy different worlds, have
distinct temperaments and life stages, each carries a concrete friction hook,
and souls contain a `## Friction` section with disagreements and bad takes.
