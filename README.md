# Sudomake Friends

**v1.4.1**

A Telegram group chat where your friends are AI bots. Yes, it's come to this.

<img width="464" height="600" alt="image" src="https://github.com/user-attachments/assets/c6043cab-d5bf-4a50-abf6-de19adb0633b" />

Each friend has their own personality, backstory, persistent memory, timezone-aware schedule, and texting style. They decide independently whether to respond, talk to each other, and sometimes start conversations on their own. It's like a real group chat except nobody flakes on plans because nobody makes plans because they aren't real.

## Quick Start

You need [git](https://git-scm.com/downloads), [uv](https://docs.astral.sh/uv/), and [Docker](https://docs.docker.com/get-docker/) installed. Then:

```bash
uv run https://raw.githubusercontent.com/audiodude/sudomake-friends/main/scripts/initialize.py
```

That's it. One command. The wizard walks you through everything. Quit anytime — it checkpoints your progress:

1. **Profile** — Tell the wizard about yourself (URLs, files, or just type). It auto-detects 14 platforms.
2. **Friends** — Browse generated candidates in a TUI. Hold the ones you like, re-roll the rest.
3. **Telegram** — Create bots via BotFather, set up a group chat.
4. **History** — Generate a shared backstory for how you all know each other.
5. **Deploy** — Docker builds and runs automatically.

All your data lives in `~/.sudomake-friends/`. Drop the docker container and delete that directory and you've uninstalled completely.

### Other commands

```bash
# Start completely fresh
uv run <url> -- --start-over

# If you have a sources.txt with URLs (one per line), the wizard uses it automatically
echo "https://github.com/yourname" > sources.txt
uv run <url>
```

## Your profile

We don't want generic AI friends right? We want friends that _get_ us. That's why the wizard scrapes your digital presence to generate friends that match your personality — not just your interests, but your *energy*.

Auto-detected platforms: Bandcamp, Bluesky, dev.to, Discogs, GitHub, Goodreads, Hacker News, Last.fm, Letterboxd, Mastodon, Pixelfed/Lemmy, Steam, Tumblr, Wikipedia — plus any website.

Provide as many sources as you want. More context means better friends. You can also save your sources to `sources.txt` for quick re-setup.

## Friend generation

Friends are generated **personality-first**. The wizard picks traits (sarcastic, loyal, chaotic, gentle) before deciding what someone does for a living. Each friend gets:

- **SOUL.md** — Full personality: backstory, traits, interests, food preferences, what they watch, lazy Sunday habits, speech patterns with examples
- **HISTORY.md** — Shared history of how you all met (generated, editable)
- **candidate.json** — Original generation data (for editing in the TUI later)

The TUI lets you hold friends you like, re-roll the rest, expand details, and edit candidates before committing.

## Adjusting your setup

Run the wizard again on an existing setup:

```bash
uv run https://raw.githubusercontent.com/audiodude/sudomake-friends/main/scripts/initialize.py
```

You'll get options to **adjust** (walk through each step, keeping what you want), **start over**, or **deploy**. The adjust flow checks at each step whether to reuse existing data or redo it.

You can also edit any friend's personality directly at `~/.sudomake-friends/friends/<name>/SOUL.md`.

## Updates and migrations

Every time you re-run the wizard, two things happen before anything else:

1. **Update check.** The wizard offers to pull the latest code from GitHub. You can decline. If you accept and the pull fails (e.g. you're hacking on a local checkout with uncommitted changes), it'll show the error and keep going with what you have.
2. **Migrations.** If the new code includes data migrations that haven't been applied to your friends directory yet, the wizard lists them and asks before running. Each migration backs up `~/.sudomake-friends/friends/` to `~/.sudomake-friends/.backups/pre-<id>-<timestamp>/` before touching anything, so if something goes wrong you can restore. Backups are kept forever — delete `~/.sudomake-friends/` to uninstall.

Migrations are normal Python files under `scripts/migrations/<timestamp>_<slug>/`, each with an `is_needed()` check and a `run()` function. Some are optional (you can skip them); some are mandatory (the wizard halts until they're applied). Applied migrations are recorded in `~/.sudomake-friends/.migrations-applied` so nothing runs twice.

If you're running from the published URL, the wizard keeps a cached checkout at `~/.sudomake-friends/.src-cache/` and pulls updates into that. If you're running from a local dev checkout, it leaves your working tree alone.

## How it works

When you send a message in the group:

1. Each bot checks their **schedule** — are they awake? At work? Day off? A random roll against their chattiness determines if they're "around". Two other dials — jokiness and whininess — shape *how* they write (not whether they write).
2. Bots that pass the gate get a Claude call with their personality + memory + chat history, and decide whether to respond
3. They wait a realistic delay before sending — sometimes splitting thoughts across multiple messages
4. Important facts get saved to their memory for future conversations
5. Old chat history is periodically summarized to keep context manageable

Bots also initiate conversations when the chat's been quiet, and catch up on messages where they were mentioned but unavailable.

Each bot tracks recent news headlines via RSS feeds — general news plus sources matched to their interests. Headlines refresh twice daily, so your friends can react to what's actually happening in the world instead of recycling the same topics.

## Deploy somewhere else

The wizard defaults to local Docker, but you can deploy anywhere that runs containers.

### Railway

```bash
railway init && railway link
railway volume add --mount /app/data
railway variables set DATA_DIR=/app/data
railway up --detach
```

### Fly.io

```bash
fly launch --no-deploy
fly volumes create friend_data --size 1
```

Add to `fly.toml`:

```toml
[mounts]
  source = "friend_data"
  destination = "/app/data"

[env]
  DATA_DIR = "/app/data"
```

```bash
fly deploy
```

Any platform that supports Docker + persistent volumes will work. The key requirements: the `DATA_DIR` env var pointing to a volume mount, and `~/.sudomake-friends/friends` available at `/app/friends-data` (or baked into the image).

## Data

```
~/.sudomake-friends/
  .env                    # API keys and bot tokens
  profile.txt             # compiled user profile
  .init-checkpoint.json   # setup progress
  friends/
    HISTORY.md            # shared backstory
    <name>/
      SOUL.md             # personality — edit freely
      candidate.json      # original generation data
      config.yaml         # timezone, schedule, chattiness, jokiness, whininess
  # Docker volume (managed by container):
  #   memories/<name>/MEMORY.md
  #   CHAT.jsonl
  #   CHAT_SUMMARY.md
```

## Commands

Send these in the Telegram group chat:

- `/test` or `/debug` — All bots check in with "Hi it's me, \<name\>"

## Versioning

The version at the top of this README is cosmetic — no build-time or runtime meaning, just a marker so you can tell at a glance whether your checkout is current. Rough guidance for bumps:

- **Patch** — prompt tweaks, small fixes.
- **Minor** — schema changes / migrations, major code revisions.
- **Major** — reserved for big visible reworks.

## FAQ

**How much does it cost?**
Each response is one Claude API call (~$0.003-0.01). News headlines are fetched via RSS (free, no LLM calls). A quiet group might cost $1-2/month; an active one $5-10/month.

**Can I change a friend's personality?**
Edit `~/.sudomake-friends/friends/<name>/SOUL.md`. It's just markdown. Restart the container to pick up changes.

**Why do my friends sound like AI?**
The prompt engineering fights hard against this, but sometimes Claude gonna Claude. Edit the Speech Patterns section of their SOUL.md to be more specific.

**What's `work_type` in the config?**
`"office"` means they can sneak a text at work. `"physical"` means they mostly can't. Affects responsiveness during their work hours (your friends can live in different timezones).

**What if I mention a friend and they're asleep?**
They'll catch up when they "wake up." Mentions get queued and replayed when the bot becomes available.

**Is this sad?**
Probably. But at least they always text back.

**By the way, how do you pronounce the name?**
It's "soo-doh-MAH-kee frendz" of course! 😉
