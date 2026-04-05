# Friend Group

A Telegram group chat where your friends are AI bots. Yes, it's come to this.

Each friend has their own personality (SOUL.md), persistent memory (MEMORY.md), timezone-aware schedule, and texting style. They decide independently whether to respond to messages, occasionally talk to each other, and remember things you've told them. It's like a real group chat except nobody flakes on plans because nobody makes plans because they aren't real.

## Quick Start

No clone needed. Just run:

```bash
uv run https://raw.githubusercontent.com/audiodude/friend-group/main/scripts/initialize.py
```

This clones the repo, then walks you through setup. Or if you already have the repo:

```bash
uv run scripts/initialize.py
```

The setup wizard walks you through everything:

1. **Anthropic API key** — you'll need one from [console.anthropic.com](https://console.anthropic.com)
2. **User profile** — point it at your website, paste a description, or load a file. It uses this to generate friends you'd actually want to talk to (the bar is low — they just need to be more interesting than a chatbot, which, well)
3. **Friend selection** — a curses TUI shows 20 candidates. Navigate with arrow keys, ENTER to hold, `r` to re-roll the rest, `a` to accept, `e` to edit their personalities in your editor first. Repeat until you've assembled a friend group that doesn't make you feel too weird about yourself
4. **Telegram bots** — step-by-step BotFather instructions. One bot per friend. Yes you have to do this manually. No there is not a better way
5. **Group chat** — create a Telegram group, add the bots, the script auto-detects the group ID

Quit at any point — progress is checkpointed. Run the script again to resume.

## Run locally

```bash
uv run python -m src.main
```

Reads config from `config.yaml` and secrets from `.env`.

## Deploy to Railway

```bash
railway init --name friend-group
railway link
railway service link friend-group-worker
```

Set env vars (from `.env`):
- `ANTHROPIC_API_KEY`
- `TELEGRAM_GROUP_CHAT_ID`
- `TELEGRAM_BOT_TOKEN_<NAME>` for each friend

```bash
railway up --detach -m "deploy"
```

Add a volume mounted at `/app/data` for persistent chat history and memories.

## How it works

When you send a message in the group:

1. Each bot independently checks their **schedule** — are they awake? At work? Is it their day off? A random roll against their chattiness determines if they're "around"
2. Bots that pass the schedule gate get a Claude API call with their SOUL.md + MEMORY.md + recent chat history, and **decide whether to respond** based on personality and relevance
3. If they respond, they wait a realistic delay (simulating typing) before sending
4. If something important was said, they selectively update their MEMORY.md
5. Chat history is periodically compacted — old messages get summarized so context doesn't grow forever

Bots also occasionally respond to each other, at a lower rate.

## Project structure

```
friends/
  <name>/
    SOUL.md        # personality (static, edit to taste)
    MEMORY.md      # learned facts (auto-updated)
    config.yaml    # timezone, schedule, chattiness
src/
  main.py          # entry point
  bot.py           # telegram polling + message dispatch
  brain.py         # claude-powered decision + response
  chat_history.py  # rolling chat log + compaction
  schedule.py      # timezone-aware availability
  config.py        # config loading
scripts/
  initialize.py    # setup wizard
```

## Adding more friends

Create a new bot with BotFather, then either:
- Run `initialize.py` again (it'll detect existing friends)
- Manually create a `friends/<name>/` directory with SOUL.md, MEMORY.md, and config.yaml

Set `TELEGRAM_BOT_TOKEN_<NAME>` and redeploy.

## Is this sad?

Probably. But at least they always text back.
