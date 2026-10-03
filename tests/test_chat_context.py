"""Chat-context rendering: a reply must name WHO is being replied to.

Rendering "(replying to msg:12345)" forces the model to resolve a bare numeric
ID by scanning the log — an indirection weaker models routinely get wrong, which
shows up as friends being confused about who the human was addressing. The
context should name the sender and quote the message instead.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import chat_history
from src.chat_history import ChatMessage, get_chat_context


def _msg(**kw):
    base = dict(timestamp=0.0, sender="jordan", text="hi", message_id=1)
    base.update(kw)
    return ChatMessage(**base)


class TestReplyRendering:
    def test_reply_names_sender_and_quotes_target(self):
        target = _msg(message_id=100, sender="jordan", text="boxes are not progress")
        reply = _msg(message_id=101, sender="Travis", text="what do you mean", reply_to=100)
        out = reply.display({100: target})
        assert "jordan" in out
        assert "boxes are not progress" in out
        assert "msg:100" not in out          # the bare-ID indirection is gone
        assert "what do you mean" in out

    def test_falls_back_to_id_when_target_not_in_window(self):
        # Replying to a message that has scrolled out of the loaded window.
        reply = _msg(message_id=101, sender="Travis", text="huh", reply_to=999)
        out = reply.display({})
        assert "msg:999" in out

    def test_backward_compatible_without_lookup(self):
        reply = _msg(message_id=101, sender="Travis", text="huh", reply_to=999)
        assert "msg:999" in reply.display()

    def test_long_target_is_truncated(self):
        target = _msg(message_id=100, sender="quinn", text="x" * 300)
        reply = _msg(message_id=101, sender="Travis", text="ok", reply_to=100)
        out = reply.display({100: target})
        assert "..." in out
        assert len(out) < 300  # not the whole 300-char message inlined

    def test_reaction_also_names_target(self):
        target = _msg(message_id=100, sender="robin", text="peaked early")
        reaction = _msg(message_id=0, sender="Travis", text="😂",
                        reply_to=100, is_reaction=True)
        out = reaction.display({100: target})
        assert "robin" in out
        assert "msg:100" not in out

    def test_non_reply_unchanged(self):
        out = _msg(message_id=5, sender="quinn", text="morning").display({})
        assert out == "[msg:5][quinn]: morning"


class TestGetChatContextWiring:
    def test_context_resolves_replies_end_to_end(self, tmp_path):
        chat = tmp_path / "CHAT.jsonl"
        rows = [
            dict(timestamp=1.0, sender="jordan", text="boxes are not progress",
                 message_id=100, reply_to=0, is_reaction=False),
            dict(timestamp=2.0, sender="Travis", text="what do you mean",
                 message_id=101, reply_to=100, is_reaction=False),
        ]
        chat.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        with patch.object(chat_history, "CHAT_PATH", chat), \
             patch.object(chat_history, "CHAT_SUMMARY_PATH", tmp_path / "nope.md"):
            out = get_chat_context(limit=50)

        # The reply line must name jordan, not just an opaque ID.
        assert "jordan" in out
        assert "what do you mean" in out
        assert "replying to msg:100" not in out

    def test_supplied_snapshot_renders_exact_messages_with_summary_and_threading(self, tmp_path):
        chat = tmp_path / "CHAT.jsonl"
        chat.write_text(json.dumps(vars(_msg(sender="LiveOnly", text="not in this snapshot", message_id=999))) + "\n")
        summary = tmp_path / "CHAT_SUMMARY.md"
        summary.write_text("Earlier they planned a pottery show.")
        snapshot = [
            _msg(message_id=100, sender="jordan", text="boxes are not progress"),
            _msg(message_id=101, sender="Travis", text="what do you mean", reply_to=100),
            _msg(message_id=0, sender="robin", text="reaction", reply_to=101, is_reaction=True),
        ]
        with patch.object(chat_history, "CHAT_PATH", chat), \
             patch.object(chat_history, "CHAT_SUMMARY_PATH", summary):
            out = get_chat_context(limit=1, messages=snapshot)

        assert "Earlier they planned a pottery show." in out
        assert "[msg:100][jordan]: boxes are not progress" in out
        assert 'replying to jordan: "boxes are not progress"' in out
        assert "what do you mean" in out
        assert 'robin reacted reaction to Travis: "what do you mean"' in out
        assert "LiveOnly" not in out
        assert "not in this snapshot" not in out

    def test_empty_supplied_snapshot_does_not_fall_back_to_live_messages(self, tmp_path):
        chat = tmp_path / "CHAT.jsonl"
        chat.write_text(json.dumps(vars(_msg(text="live chat is not empty"))) + "\n")
        summary = tmp_path / "CHAT_SUMMARY.md"
        summary.write_text("Earlier they planned a pottery show.")
        with patch.object(chat_history, "CHAT_PATH", chat), \
             patch.object(chat_history, "CHAT_SUMMARY_PATH", summary):
            out = get_chat_context(messages=[])

        assert "Earlier they planned a pottery show." in out
        assert "live chat is not empty" not in out
