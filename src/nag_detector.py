"""Detect threads being beaten to death by multiple bots — pile-on prevention.

Scans recent chat for question-like messages, finds substantive content words
that appear across multiple distinct askers in a short window, and surfaces
them so prompts can warn bots not to pile on.
"""

import re
import time
from collections import defaultdict

from .chat_history import load_messages

NAG_WINDOW_SECONDS = 60 * 60
MIN_DISTINCT_ASKERS = 2

QUESTION_MARKERS = (
    "?",
    "wait did",
    "did anyone",
    "did you ever",
    "ever actually",
    "what happened with",
    "anyone ever",
    "still wondering",
    "never answered",
    "never said",
    "never did",
    "did we ever",
)

STOP_WORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its", "let",
    "put", "say", "she", "too", "use", "what", "with", "have", "from", "your",
    "that", "this", "they", "their", "them", "just", "like", "about", "still",
    "when", "would", "should", "could", "going", "gonna", "wanna", "doing",
    "really", "kind", "even", "actually", "ever", "wait", "yeah", "well", "make",
    "made", "thing", "things", "tho", "though", "stuff", "some", "anyone",
    "where", "back", "want", "more", "than", "into", "then", "much", "been",
    "down", "over", "only", "also", "very", "such", "most", "other", "those",
    "these", "here", "there", "every", "always", "never", "today", "tomorrow",
    "yesterday", "sure", "right", "wrong", "true", "false", "thinking", "talking",
    "saying", "honestly", "literally", "basically", "probably", "maybe", "good",
    "bad", "nice", "cool", "lol", "haha", "fwiw", "ngl", "tbh", "imo", "ive",
    "youre", "youve", "yall", "ones", "shit", "kinda", "sorta", "lot", "whole",
    "around", "still", "getting", "tried", "trying", "find", "found", "look",
    "looking", "look", "feel", "felt", "feels", "guess", "thought", "thoughts",
    "today", "tonight", "morning", "afternoon", "night", "since", "before",
    "after", "during", "while", "soon", "later", "earlier", "yet", "again",
    "anyway", "anyways", "actual", "kinda", "stuff", "another", "nothing",
    "something", "everything", "anything", "someone", "everyone", "people",
    "person", "place", "name", "names",
}


def _is_question(text: str) -> bool:
    t = text.lower().strip()
    return any(m in t for m in QUESTION_MARKERS)


def _content_terms(text: str) -> set[str]:
    """Substantive content tokens — at least 3 chars, alphabetic, not a stop word."""
    words = re.findall(r"[a-z][a-z0-9]+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS}


def get_overasked_terms(messages_limit: int = 100) -> list[tuple[str, int, list[str]]]:
    """Return [(term, ask_count, sorted_distinct_askers)] for threads being nagged.

    A "term" is any content word that ≥MIN_DISTINCT_ASKERS distinct senders
    have asked questions containing, within the last NAG_WINDOW_SECONDS.
    """
    messages = load_messages(limit=messages_limit)
    if not messages:
        return []

    cutoff = time.time() - NAG_WINDOW_SECONDS
    questions = [
        m for m in messages
        if m.timestamp >= cutoff and not m.is_reaction and _is_question(m.text)
    ]
    if not questions:
        return []

    askers_by_term: dict[str, set[str]] = defaultdict(set)
    count_by_term: dict[str, int] = defaultdict(int)

    for q in questions:
        for term in _content_terms(q.text):
            askers_by_term[term].add(q.sender.lower())
            count_by_term[term] += 1

    overasked = [
        (term, count_by_term[term], sorted(askers))
        for term, askers in askers_by_term.items()
        if len(askers) >= MIN_DISTINCT_ASKERS
    ]
    overasked.sort(key=lambda x: (-len(x[2]), -x[1]))
    return overasked[:8]


def render_overasked_block() -> str:
    """Format the overasked terms for prompt injection. Empty string if none."""
    overasked = get_overasked_terms()
    if not overasked:
        return ""
    lines = []
    for term, count, askers in overasked:
        lines.append(
            f'- "{term}" — asked {count}x by {len(askers)} different people '
            f'({", ".join(askers)}) in the last hour'
        )
    return "\n".join(lines)
