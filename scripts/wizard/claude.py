import json
import os
from pathlib import Path

from wizard.paths import load_env

MODEL = "claude-4-sonnet-20250514"
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
    held_desc = ""
    if held:
        held_desc = f"""
These friends have already been selected in this session (do NOT regenerate them,
and make sure new candidates have good chemistry with them):
{json.dumps(held, indent=2)}
"""
    existing_desc = ""
    if existing_friends:
        existing_desc = f"""
These friends ALREADY EXIST in the group (do NOT regenerate them, generate
candidates who would fit in with this existing group):
{', '.join(existing_friends)}
"""

    prompt = f"""Based on this profile of a person, generate exactly {count} fictional friend
candidates for a virtual group chat.

START WITH PERSONALITY, NOT PROFESSION. Think about what kind of PERSON would be
a great friend — their energy, their humor, their emotional style — then figure out
what they do for a living as a secondary detail.

Return ONLY a JSON array of objects. Each object must have:
- "name": first name only, capitalized (prefer gender-neutral names or gender-ambiguous nicknames)
- "traits": array of 3-4 personality trait words (e.g. ["sarcastic", "loyal", "impulsive"] or ["gentle", "witty", "stubborn"]). These are the CORE of who this person is.
- "age": integer
- "location": city and country only (e.g. "Berlin, Germany" or "San Francisco, CA")
- "occupation": what they do (keep it brief — this is NOT the interesting part)
- "vibe": 1-3 sentences about who they are as a PERSON. Lead with personality and energy, not their job. How do they make you feel when you're around them? What's their deal? IMPORTANT: Show their traits through behavior and anecdotes, don't just list adjectives. "Will roast your music taste then make you a perfect playlist" not "sarcastic but caring".
- "why": why they'd be this person's friend — focus on personality chemistry, not shared hobbies (1 sentence)
- "timezone": IANA timezone string
- "chattiness": float 0.0-1.0 — how often they respond / initiate
- "jokiness": float 0.0-1.0 — how much they reach for jokes vs. being plain/sincere. Low = dry, literal, earnest. High = playful, quippy (but NEVER setup-punchline bit comedy)
- "whininess": float 0.0-1.0 — how much they complain about things. Low = stoic/positive, high = often venting. Match to the person's vibe

CRITICAL: A friend group needs PERSONALITY DIVERSITY, not just occupational diversity.
You need the snarky one, the sincere one, the chaotic one, the calm one, the one who
roasts everyone, the one who gives unsolicited advice, the one who sends memes at 2am.
Don't make everyone pleasant and supportive — real friend groups have friction, teasing,
and complementary energies.

Make the friends diverse in personality, location, and timezone.
Some should be local, some remote. Mix of introverts/extroverts, tech/non-tech.
{held_desc}{existing_desc}

## Profile of the person
{context}

JSON array only, no markdown fencing:"""

    response = client.messages.create(
        model=MODEL, max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def generate_soul(client, candidate: dict, all_friends: list[dict],
                  user_context: str) -> str:
    others = [f for f in all_friends if f["name"] != candidate["name"]]
    others_desc = "\n".join(
        f"- {f['name']} ({', '.join(f.get('traits', []))}): {f['vibe']}"
        for f in others
    )
    traits = candidate.get("traits", [])
    traits_str = ", ".join(traits) if traits else "not specified"

    prompt = f"""Write a detailed SOUL.md personality file for a virtual chat bot character.
This character will be in a group chat with friends.

## The character
{json.dumps(candidate, indent=2)}

## Their core personality traits: {traits_str}
These traits are the FOUNDATION of this character. Everything else — their backstory,
their interests, their speech patterns — should flow from and reinforce these traits.
A "sarcastic, loyal, impulsive" person texts differently than a "gentle, witty, stubborn"
person. They have different childhoods, different relationships, different coping mechanisms.

## Other friends in the group
{others_desc}

## Another friend in the group
{user_context}

CRITICAL INSTRUCTION: This is a WHOLE PERSON, not a walking job description. Their
occupation is ONE facet of who they are. You MUST flesh out their entire life — where
they grew up, their family, what they studied, what they eat, what they watch, what
they do on a lazy Sunday. A real friend is someone you know deeply, not a LinkedIn
profile with a texting style.

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
(how they relate to each of the other friends — think about
personality chemistry, not just shared interests)

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
- What personality TRAITS they'd vibe with in friends (not just shared interests —
  think about complementary energies. A sarcastic person might love a sincere friend.
  An anxious person might need a calm, grounding friend.)

End the profile with a section called "Friend chemistry notes" that lists 4-6
personality trait combos that would make good friends for this person, e.g.:
- "witty + slightly chaotic — someone who matches their banter energy"
- "calm + deeply sincere — a grounding presence when things get intense"

Be specific — names, places, projects, preferences. This profile will be reused
to generate fictional friend characters, so include anything that would help
determine compatibility.

Raw content:
{raw_context[:20000]}

Write the profile directly, no preamble:"""

    response = client.messages.create(
        model=MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
