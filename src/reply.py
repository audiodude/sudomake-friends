"""Prepared texts and delivery-owned persistence; generation never commits effects."""

from dataclasses import dataclass, field
from datetime import datetime
import logging

from .config import load_friend_memory, save_friend_memory
from .topics import record_topic, record_joke_format, record_complaint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyAtom:
    index: int
    text: str


@dataclass(frozen=True)
class ReplyEffect:
    kind: str
    value: str
    atom_ids: tuple[int, ...]
    source: str
    source_message_id: int | None


def _update_memory(friend_name: str, new_note: str) -> None:
    """Read current memory at commit, not the generation-time snapshot."""
    current = load_friend_memory(friend_name)
    entry = f"- [{datetime.now():%Y-%m-%d %H:%M}] {new_note}"
    updated = (current.rstrip() if current.strip() else "# Memory") + "\n" + entry
    lines = updated.split("\n")
    entries = [line for line in lines if line.startswith("- [")]
    if len(entries) > 50:
        updated = "\n".join([line for line in lines if not line.startswith("- [")] + entries[-30:])
    save_friend_memory(friend_name, updated)


@dataclass
class PreparedReply:
    atoms: tuple[ReplyAtom, ...]
    effects: tuple[ReplyEffect, ...]
    reply_to_message_id: int | None
    delay_seconds: float
    owner_name: str | None = field(default=None, repr=False)
    _delivered: set[int] = field(default_factory=set, init=False, repr=False)
    _committed: set[int] = field(default_factory=set, init=False, repr=False)
    _friend_name: str | None = field(default=None, init=False, repr=False)

    def commit_atom(self, friend_name: str, atom_index: int) -> None:
        """Call synchronously immediately after confirmed Telegram success.

        Effects supported by several atoms wait for all of them. Repeated commits
        are idempotent within this prepared reply; no crash-recovery claim is made.
        """
        if self.owner_name is not None and self.owner_name != friend_name:
            raise ValueError("Prepared reply belongs to another friend")
        if atom_index not in {atom.index for atom in self.atoms}:
            raise ValueError("Cannot commit an absent or filtered atom")
        if self._friend_name is not None and self._friend_name != friend_name:
            raise ValueError("Prepared reply cannot be committed for another friend")
        self._friend_name = friend_name
        self._delivered.add(atom_index)
        writers = {"memory": _update_memory, "topic": record_topic,
                   "joke_format": record_joke_format, "complaint_topic": record_complaint}
        for index, effect in enumerate(self.effects):
            if index in self._committed or not set(effect.atom_ids) <= self._delivered:
                continue
            try:
                writers[effect.kind](friend_name, effect.value)
            except OSError:
                logger.exception(
                    "Failed to persist %s effect for %s after atom %s",
                    effect.kind, friend_name, atom_index,
                )
                continue
            self._committed.add(index)
