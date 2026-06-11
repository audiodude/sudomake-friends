import json
import os
from pathlib import Path

from wizard.axes import sample_profiles, render_profile
from wizard.paths import load_env

MODEL = "claude-opus-4-8"
CANDIDATE_COUNT = 8
SCRAPE_TIMEOUT = 180


def get_client(env_path: Path):
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY") or load_env(env_path).get("ANTHROPIC_API_KEY")
    if not key:
        return None
    os.environ["ANTHROPIC_API_KEY"] = key
    return anthropic.Anthropic(api_key=key)


def generate_candidates(client, context: str, held: list[dict],
                        existing_friends: list[str] | None = None,
                        count: int = CANDIDATE_COUNT) -> list[dict]:
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
    candidates = candidates[:count]
    for cand, prof in zip(candidates, profiles):
        cand["axes"] = prof
    return candidates


def generate_soul(client, candidate: dict, all_friends: list[dict],
                  user_context: str) -> str:
    others = [f for f in all_friends if f["name"] != candidate["name"]]
    others_desc = "\n".join(
        f"- {f['name']} ({', '.join(f.get('traits', []))}): {f['vibe']}"
        for f in others
    )
    traits = candidate.get("traits", [])
    traits_str = ", ".join(traits) if traits else "not specified"
    axes = candidate.get("axes", {})
    friction_hook = axes.get("friction_hook") or candidate.get("friction", "")
    how_met = candidate.get("how_met", "")

    prompt = f"""Write a detailed SOUL.md personality file for a virtual chat bot character.
This character will be in a group chat with friends.

## The character
{json.dumps(candidate, indent=2)}

## Their core personality traits: {traits_str}
These traits are the FOUNDATION of this character. Everything else — their backstory,
their interests, their speech patterns — should flow from and reinforce these traits.
A "sarcastic, loyal, impulsive" person texts differently than a "gentle, witty, stubborn"
person. They have different childhoods, different relationships, different coping mechanisms.

## Their friction with the user
{friction_hook}
This is a STANDING dynamic, not a one-off. It should surface in their
disagreements, their speech examples, and their boundaries.

## How they know the user
{how_met}

## Other friends in the group
{others_desc}

## Another friend in the group
{user_context}

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

The personality traits above should PERMEATE everything. If someone is "sarcastic",
their text examples should drip with sarcasm. If someone is "anxious", their backstory
should explain why. If someone is "chaotic", their interests should be all over the place.

Write the SOUL.md in this exact format:

# {{Name}}

## Identity
- **Age:** ...
- **Location:** ...
- **Hometown:** ... (where they grew up — should be different from current location)
- **Occupation:** ...
- **Traits:** {traits_str}
- **Timezone:** ...

## Backstory
(1-2 paragraphs: Where did they grow up? What was their family like — siblings,
parents' jobs, family dynamics? Where did they go to college (or not), what did
they study, what year did they graduate? How did they end up where they live now?
What's the arc of their life so far? The backstory should EXPLAIN the personality traits.)

## Personality
(2-3 paragraphs: core traits, emotional patterns, worldview, humor style.
This should go BEYOND their professional identity. Ground it in the traits: {traits_str}.
How do these traits manifest day-to-day? How do they interact with each other?
What's charming about this person and what's annoying?)

## Friction
(REQUIRED: 2-3 standing disagreements with the friend described under
"Another friend in the group" — refer to them by their actual name, never
as "the user". One sentence each: what the argument is and this character's
position. Then 1-2
sincerely-held bad takes they defend. These should be consistent with the
friction dynamic described above.)

## Interests & Life
(bullet list that covers their WHOLE life, not just their job niche:
- Professional/hobby interests
- Favorite foods, cooking habits, restaurants
- Movies, TV, books, podcasts they love
- Outdoor activities, sports, fitness habits
- Travel experiences or aspirations
- Guilty pleasures, comfort activities
- What they do on a Friday night or lazy Sunday)

## Relationships
(how they relate to each of the other friends — include FRICTION, not just
harmony. Who do they tease? Whose lifestyle baffles them? Real groups have
edges. Also state how they met the user — use the life collision above.)

## Speech Patterns
(very specific texting style: capitalization, punctuation, emoji usage, message length,
slang, verbal tics. This section is CRITICAL for making the character feel real.
The traits ({traits_str}) should be OBVIOUS from the text examples alone.
Include 5+ examples of how they'd actually text — covering different moods and topics,
NOT just about their job.)

## Boundaries
(topics they avoid, things that annoy them, conversational pet peeves — these should
also flow from their personality traits)

Be specific and vivid. Avoid generic traits. The character should feel like someone
you've known for years — you know their coffee order, that they hate cilantro, that
they call their mom every Sunday. The Speech Patterns section should make it possible
to distinguish this character's messages from any other character at a glance."""

    response = client.messages.create(
        model=MODEL, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def compile_profile(client, raw_context: str) -> str:
    """Distill raw scrape/text into a clean, reusable user profile."""
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

    response = client.messages.create(
        model=MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
