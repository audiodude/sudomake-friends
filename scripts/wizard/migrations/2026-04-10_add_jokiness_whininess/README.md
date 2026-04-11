# 2026-04-10: Add jokiness and whininess

Adds two new personality dials to every friend's `config.yaml` and `candidate.json`:

- **jokiness** (0.0–1.0): how often this friend reaches for humor vs. being plain and sincere. Low = dry, literal, earnest. High = playful, quippy.
- **whininess** (0.0–1.0): how often this friend complains about things. Low = stoic, positive. High = vents frequently.

These compose with the existing `chattiness` dial and are rendered into each friend's prompt as plain-language guidance. They exist to fight repetitive joke formats and complaint topics.

## How values are picked

When you run the migration interactively you'll see three options:

- **llm**: read each friend's `SOUL.md` and ask Claude to propose tuned values with a one-line reason. You can accept all, edit individually, or cancel. Requires `ANTHROPIC_API_KEY`.
- **manual**: prompt you for each friend's values one by one.
- **defaults**: set everyone to `jokiness=0.5`, `whininess=0.3`.

The LLM option reproduces how values were originally tuned when this feature was added — it reads the personality and picks numbers that match.

## Non-interactive mode

If `interactive=False` is passed, the migration applies defaults to all friends needing the fields.
