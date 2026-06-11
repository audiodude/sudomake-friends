# Friend Revamp: Personality Spread + Model Split

**Date:** 2026-06-10
**Status:** Approved

## Problem

The current generation pipeline produces friends that are copies of the user
with different job titles. All four existing friends are tech-adjacent coastal
millennial creatives who share the user's interests (coffee, farmers markets,
vintage shopping, sci-fi, craft hobbies) and temperament (sensitive
overthinker). With no real personality spread, the only distinguishing voice
left is joke cadence — which is why the chat collapsed into repetitive jokey
message formats with the subject matter swapped out.

Root cause: the prompts *ask* for diversity but nothing *enforces* it. All
candidates come from one model call anchored on the user profile, and the
profile compiler's "friend chemistry notes" select for vibe-compatibility.
The model's prior for "compatible friend" is "the user with a different job."

## Decisions

- Full replacement: nothing survives but the Telegram bot IDs. User wipes
  chat history and re-runs setup from scratch after engine changes land.
- Group size drops from 4 to 3 (user retires one bot on the Telegram side).
- Friction level: real friction — friends who disagree with the user and each
  other, tease, push back, and hold sincere bad takes. Not engineered-to-annoy.
- Diversity is enforced structurally in Python (approach B), not requested in
  prompts (approach A, already failed) or post-hoc filtered (approach C).

## Design

### 1. Model split

| Call site | Model |
|---|---|
| `scripts/wizard/claude.py` `MODEL` (candidates, souls, profile) | `claude-opus-4-8` |
| `config.yaml` runtime chat model | `claude-sonnet-4-6` |
| `src/config.py` fallback default (currently malformed `claude-sonnet-4-6-20250514`) | `claude-sonnet-4-6` |

Rationale: soul generation is a handful of one-shot calls whose quality
compounds into every downstream chat message — frontier model is cheap
insurance. Chat volume stays on the budget model.

### 2. Axis system — `scripts/wizard/axes.py` (new)

Python deterministically samples a *difference profile* per candidate before
any LLM call. The LLM's job shrinks from "invent a compatible group" to "make
a believable human who fits THIS assigned profile."

Axes and pools:

- **World** (job/milieu): trades, healthcare, food service, corporate/finance,
  education, arts, science, civic/government, retail, agriculture, logistics,
  hospitality. Tech allowed for at most ONE candidate per batch.
- **Temperament**: blunt, sunny, anxious, stoic, chaotic, fussy, intense,
  laid-back, competitive, nurturing.
- **Relationship to tech**: indifferent normie, curious amateur, enthusiastic
  first adopter, professional.
- **Life stage** (with attached age range — not everyone is 29-34): single,
  new parent, divorced, caretaking a parent, empty-nester, perpetual student,
  recently relocated, settled homeowner.
- **Friction hook**: one concrete way they rub against the user. Examples:
  "thinks AI is overhyped and says so", "lectures about money", "judges your
  sleep schedule", "music opinions with actual credentials behind them",
  "asks you to explain your job and remains unimpressed".

Sampling rules:

- Without replacement within a batch where the pool is large enough; for
  smaller pools (e.g. relationship to tech has 4 values), values are spread
  as evenly as possible — no value repeats until every value has been used.
  Either way, 8 candidates occupy 8 different corners by construction.
- Assigned axes are stored in `candidate.json` alongside the existing fields.
- Re-rolls exclude axes held by already-selected candidates, so the group
  cannot converge across rolls.
- The batch candidate prompt embeds the 8 assigned profiles; the model fills
  in name, vibe, backstory hooks, and dials to fit each one.

### 3. Profile compiler (`compile_profile`)

Remove "Friend chemistry notes" (selects for similarity). Replace with **foil
notes**: what the user is NOT — blind spots, taste boundaries, things they'd
argue about. The candidate generator uses foil notes to aim friction.

### 4. Soul prompt (`generate_soul`)

Each SOUL.md must now include:

- **2-3 concrete standing disagreements with the user** — recurring arguments,
  not hostility. The friend who has been wrong about something for years.
- **1-2 sincerely-held bad takes** they will defend in chat.
- **Interest exclusivity**: at most ONE shared touchpoint with the user;
  everything else from the friend's own world.
- **How they met the user**: a life collision (old job, college roommate,
  neighbor, friend-of-ex) — never interest compatibility.
- **Friction with the other friends** in the Relationships section, not just
  pairwise admiration.

### 5. Runtime prompt (`src/brain.py`) — minimal

One addition to the chat prompt: disagreeing, teasing, being unimpressed, and
not relating are normal moves. "eh" is a valid opinion. The souls carry the
actual friction; this just stops the runtime prompt from sanding it off. No
new config dials.

### 6. Group of 3

No code change. The TUI already supports holding any number; the user holds 3.

## Testing

- Unit test for axis sampling: batch of 8 → no duplicate axis values within
  any axis; re-roll honors exclusions from held candidates.
- Prompt changes are verified by running real generation and reading the
  output — the user re-runs setup from scratch as the acceptance test.

## Out of scope

- Migrations: none needed. Generator changes only affect new generation;
  the model change rides the normal repo update path.
- New personality dials in config.yaml.
- Changes to the anti-repetition runtime architecture (topic memory, nag
  detection) — unchanged.
