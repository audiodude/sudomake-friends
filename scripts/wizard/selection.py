import curses

from wizard.claude import CANDIDATE_COUNT, generate_candidates
from wizard.editor import candidate_to_text, edit_with_editor, text_to_candidate
from wizard.tui import selection_ui


def run_selection_loop(
    client,
    user_context: str,
    candidates: list[dict] | None = None,
    held_indices: set[int] | None = None,
    existing_friends: list[str] | None = None,
    on_save=None,
) -> list[dict] | None:
    """Shared TUI selection loop. Returns selected candidates or None if quit."""
    if candidates is None:
        print("\n  Generating candidates...")
        candidates = generate_candidates(client, user_context, [],
                                          existing_friends=existing_friends)
        if on_save:
            on_save(candidates, set())

    if held_indices is None:
        held_indices = set()

    while True:
        held_indices, action = curses.wrapper(
            selection_ui, candidates, held_indices
        )

        if on_save:
            on_save(candidates, held_indices)

        if isinstance(action, tuple) and action[0] == "edit_candidate":
            idx = action[1]
            text = candidate_to_text(candidates[idx])
            edited = edit_with_editor(text, label=candidates[idx]["name"].lower())
            candidates[idx] = text_to_candidate(edited, candidates[idx])
            if on_save:
                on_save(candidates, held_indices)
            continue

        if action == "quit":
            return None

        elif action == "reroll":
            held = [candidates[i] for i in sorted(held_indices)]
            n_new = CANDIDATE_COUNT - len(held)
            print(f"\n  Re-rolling {n_new} candidates (keeping {len(held)} invited)...")
            # Request extra to handle LLM returning fewer than asked
            new_candidates = generate_candidates(
                client, user_context, held,
                existing_friends=existing_friends, count=n_new,
            )
            # Held first, then new ones, cap at CANDIDATE_COUNT
            candidates = held + new_candidates
            candidates = candidates[:CANDIDATE_COUNT]
            new_held_indices = set(range(len(held)))
            held_indices = new_held_indices
            if on_save:
                on_save(candidates, held_indices)

        elif action == "accept":
            selected = [candidates[i] for i in sorted(held_indices)]
            return selected
