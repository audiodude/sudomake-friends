# Model evaluations

Offline comparisons use frozen friend personalities, saved memories, and retained chat. Nothing connects to Telegram or changes installed friend or runtime data. Running a comparison sends that private context through OpenRouter to the selected providers; scoring existing results makes no model calls.

Run these commands from the repository root. Keep generated evaluation data outside this repository.

## Prepare and run a comparison

The current lineup is Claude Sonnet 5, Claude Haiku 4.5, GPT-6 Luna, and Gemini 3.8 Flash. Each receives the same frozen runtime **writer** prompt, conditional on Jev accepting the opportunity. Capture assumes acceptance locally, without a Jev request. This compares writing, not social decisions or metadata extraction. Gemini requires reasoning and uses its lowest supported effort; its output allowance includes reasoning. The other models disable reasoning.

Prepare the default 25 cases without paid generation requests:

```bash
uv run python -m evaluation.compare_models
```

Preparation reads the installed friends and retained chat under `~/.sudomake-friends/`, captures actual runtime writer prompts, fetches current model pricing, and prints the private output directory. The default sample includes replies and quiet-chat initiation opportunities. No scheduling, probability, or social-decision gate is replayed.

Run that exact snapshot, using its printed `cases.json` path:

```bash
uv run python -m evaluation.compare_models --run --cases-file /path/to/prepared/cases.json
```

Previously frozen combined-JSON cases can still be run and scored under their original contract. New snapshots identify their plain-text writer contract explicitly; results and scorecards do not count writer drafts as social decisions.

The API key comes from exported `OPENROUTER_API_KEY` or the installation's `.env`. A shell variable must be exported to reach the Python process. Do not put credentials in evaluation files.

Options:

| Option | Meaning |
|---|---|
| `--count` | Number of cases to prepare; defaults to 25. Does not resample a supplied `--cases-file`. |
| `--home` | Source installation directory; defaults to `~/.sudomake-friends/`. |
| `--output` | New private output directory. Existing directories are rejected rather than overwritten. |
| `--budget` | Dollar allowance; defaults to $5 and cannot exceed $5. |
| `--cases-file` | Reuse a frozen snapshot instead of preparing new cases. |
| `--run` | Explicitly enable paid generation requests and private-context transmission. |

Requests run sequentially. Before starting a case, the runner reserves conservative charges for all four models. A returned charge replaces its reservation, releasing unused allowance. Unknown charges retain their full reservation. Requests are not retried automatically, and a returned charge above its reservation stops the run. This is a spending allowance, **not a transactional provider billing cap**. The full-run conservative reservation can exceed the allowance; released reservations may still allow the complete run to fit.

## Score existing results in the web app

No new model calls are needed:

```bash
uv run python -m evaluation.score_models /path/to/comparison-directory --serve
```

Open:

http://127.0.0.1:8787/

`--port` changes the port. The server binds to loopback only and serves only the scorecard—not neighboring files, credentials, or the reveal map. Requests are handled concurrently so a browser's idle preconnection cannot block page loading.

- Review one case at a time, with its conversation excerpt and all four candidates.
- Expand the full frozen prompt to check personality, memories, or context not shown in the excerpt.
- Give each candidate one overall score: **Would I want this response in the group?** Consider naturalness, personality fit, context coherence, restraint, and memory accuracy.
- **Select `0` explicitly.** Blank means unscored, not neutral. New comparisons contain conditional writer drafts, not silence decisions. In archived combined-JSON comparisons, explicit silence is valid and should be judged for appropriateness.
- Scores attach to stable case-and-letter response IDs. Letters A–D change models between cases; they are not model identities.
- Progress and your place save in the same browser and origin. Clearing browser storage, changing ports, or switching browsers may require restoring an export.
- Use **Export scores** for a backup. **Restore exported scores** restores scores and position. Exports from another comparison or changed letter map are rejected.
- Use **Next unfinished** to find remaining attempted responses. Unattempted requests cannot be scored as silence.
- Use **Reveal scored results** only when ready to see model identities and aggregates.

The letter map is preserved when reports are regenerated. Exported scores are also bound to the exact context, outputs, case order, and letter map, preventing accidental reassignment.

### Scoring anchors

| Score | Anchor |
|---:|---|
| −3 | Unacceptable: seriously wrong, harmful, or fabricated personal facts. |
| −2 | Clearly makes the chat worse: robotic, pushy, repetitive, or out of character. |
| −1 | Noticeable flaw; would prefer it stayed silent. |
| 0 | Acceptable but unremarkable, or appropriate silence. |
| +1 | Good, natural, relevant. |
| +2 | Very good; distinctly fits this friend. |
| +3 | Excellent; exactly the response you would want. |

Format and request failures are shown separately from silence. You may score the conversational quality of an invalid response's raw text, but output-contract reliability remains a separate measure.

### Interpreting scores

The model summary shows scored, neutral, and unscored counts, plus:

- **Mean:** average of that model's recorded scores only. Unequal partial samples can be biased.
- **Matched-case mean:** average over cases where all four responses have scores. Use this for comparisons of the same cases.

Neither average fills missing entries with zeros. Old score sequences with omitted zeros and no case IDs cannot be aligned reliably to models; preserve them as unassigned notes and rescore the existing outputs with stable IDs.

To summarize an exported score file from the command line:

```bash
uv run python -m evaluation.score_models /path/to/comparison-directory --scores /path/to/export.json
```

This writes `score-summary.json` in the comparison directory. Omitting `--serve` and `--scores` generates a self-contained `scorecard.html` you can open directly; model summaries are available through the local app or command line.

## Development

Evaluation-only commands and helpers live in `evaluation/`; their regression tests live in `evaluation/tests/`. Application and setup-wizard tests remain in the root `tests/` directory.

Run just the evaluation tests:

```bash
uv run --extra dev pytest evaluation/tests
```

Or run the complete suite from the repository root:

```bash
uv run --extra dev pytest
```

## Files and privacy

Default output: `~/.sudomake-friends/evaluations/<timestamp>/`. Run directories are private, and generated result files are owner-readable/writable only.

| File | Contents |
|---|---|
| `cases.json` | Frozen prompts, personalities, memories, excerpts, and sampling limitations. |
| `plan.json` | Selected models, catalogue pricing, settings, and reservations. |
| `results.jsonl` | Raw completions, parsed outputs, usage, charges, latency, and failures. |
| `run.json` | Attempted requests, returned charges, unknown charges, and stopping reason. |
| `report.md` | Blinded text comparison, with independently shuffled letters per case. |
| `reveal.json` | Stable case-letter-to-model mapping. Keep closed while scoring. |
| `metrics.json` | Model-labelled cost, reliability, latency, writer-draft counts, and archived decision counts. |
| `scorecard.html` | Private browser scorecard with frozen context and outputs. |
| `score-summary.json` | Aggregates from a supplied identified score export. |

Browser exports contain response IDs, scores, position, and a comparison fingerprint. Treat exports and all generated result files as private: **do not commit, publish, or deploy them as static assets**. `evaluation/` in this repository contains evaluation code, tests, and instructions—not personal test data.

## Limits

- Current personalities, configuration, and memories are reused for historical excerpts. They may contain hindsight; this is not a reconstruction of what each friend knew then.
- Historical summaries, shared history, and news are included only when their current snapshot's modification time permits it. Modification times are availability proxies, not version history.
- Previously pruned topics cannot be reconstructed. Historical nag-classifier outputs, images, and fetched link previews are not replayed.
- Scheduling/probability gates, Jev social decisions, metadata extraction, echo filtering, helper validation, bot-to-bot feedback loops, and end-to-end chat frequency are not evaluated in new writer comparisons.
- One sample per case does not establish a model's typical behavior. Only archived combined-JSON cases compare silence and willingness to initiate.
- Trial charges reflect these requests and their cache behavior—not a prediction of daily running costs.
