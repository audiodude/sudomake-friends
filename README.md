# Sudomake Friends

A Telegram group chat where your friends are AI bots. Yes, it's come to this.

Each friend has their own personality, persistent memory, timezone-aware schedule, and texting style. They decide independently whether to respond, occasionally talk to each other, and sometimes start conversations on their own. It's like a real group chat except nobody flakes on plans because nobody makes plans because they aren't real. And they're bots, because you don't have real friends.

## Quick Start

You need [uv](https://docs.astral.sh/uv/) and [Docker](https://docs.docker.com/get-docker/) installed. Then:

```bash
uv run https://raw.githubusercontent.com/audiodude/sudomake-friends/main/scripts/initialize.py
```

That's it. One command. The wizard walks you through everything and deploys to Docker at the end. All your data lives in `~/.sudomake-friends/`. Quit anytime — it checkpoints your progress.

To start over:
```bash
uv run https://raw.githubusercontent.com/audiodude/sudomake-friends/main/scripts/initialize.py -- --start-over
```

## Your profile

We don't want generic AI friends right? We want AI friends that are....you know....sorta like us. Who know us, who _get_ us. That's why when you generate your new friends, you can optionally provide some material about yourself to base _them_ on. One or two sentences, provided documents, all the way up to URLs to scrape.

The wizard auto-detects 14 platforms from URLs:

Bandcamp, Bluesky, dev.to, Discogs, GitHub, Goodreads, Hacker News, Last.fm, Letterboxd, Mastodon, Pixelfed/Lemmy, Steam, Tumblr, Wikipedia

Plus any website (scraped automatically). Provide as many as you want — more context means better friends. Mastodon is encouraged, it auto-scrapes your last 100 posts.

## Adding more friends

Run the wizard again — it'll detect your existing friends and let you add more:

```bash
uv run https://raw.githubusercontent.com/audiodude/sudomake-friends/main/scripts/initialize.py
```

You can also edit any friend's personality directly at `~/.sudomake-friends/friends/<name>/SOUL.md`. The wizard won't overwrite your edits.

## How it works

When you send a message in the group:

1. Each bot checks their **schedule** — are they awake? At work? Day off? A random roll against their chattiness determines if they're "around"
2. Bots that pass the gate get a Claude call with their personality + memory + chat history, and decide whether to respond
3. They wait a realistic delay before sending — sometimes splitting thoughts across multiple messages
4. Important facts get saved to their memory for future conversations
5. Old chat history is periodically summarized to keep context manageable

Bots also initiate conversations when the chat's been quiet, and catch up on messages where they were mentioned but unavailable (like a friend checking their phone after work).

## Data

Everything lives in `~/.sudomake-friends/`:

```
~/.sudomake-friends/
  .env               # API keys and bot tokens
  PROFILE.md         # your compiled profile (used to generate friends)
  friends/<name>/
    SOUL.md           # personality — edit freely
    MEMORY.md         # learned facts — auto-updated
    config.yaml       # timezone, schedule, chattiness, work_type
  data/               # chat history (managed by the running bot)
```

The Docker container mounts `friends/` and `data/` as volumes, so your data persists across rebuilds.

## FAQ

**How much does it cost?**
Depends on how chatty your friends are. Each response is one Claude API call (~$0.003-0.01). A quiet group might cost $1-2/month. Your friends will be on vacation if Docker is not running on your computer.

**Can I change a friend's personality?**
Yes, edit `~/.sudomake-friends/friends/<name>/SOUL.md`. It's just markdown. Restart the container to pick up changes.

**Why do my friends sound like AI?**
The prompt engineering fights hard against this, but sometimes Claude gonna Claude. Edit the Speech Patterns section of their SOUL.md to be more specific about how they text. Examples help.

**Can I add friends from different platforms?**
No. Telegram only, for now. Each friend is a Telegram bot.

**What's `work_type` in the config?**
`"office"` means they can sneak a text at work. `"physical"` means they mostly can't (think electrician, park ranger). Affects how responsive they are during _their_ work hours (remember, your friends can live in different timezones than you).

**What if I mention a friend and they're asleep (like not in the computer sleep sense)?**
They'll catch up when they "wake up." Direct @mentions and name mentions get queued and replayed when the bot becomes available.

**Can friends talk to each other?**
Yes, at a lower rate. Controlled by `bot_reply_chance` in their config.

**Is this sad?**
Probably. But at least they always text back. Well, if they're awake and not at work...
