"""Tests for telegram_audit.log_action."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest


class TestLogAction:
    def test_appends_jsonl_entry(self, tmp_path):
        """log_action writes a valid JSONL entry to the audit file."""
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action(
                "edit_forum_topic",
                chat_id=-100123,
                thread_id=42,
                window_id="@0",
                payload={"new_name": "james ⚡", "old_name": "james"},
                reason="subagent_suffix_add",
            )

        lines = audit_file.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "edit_forum_topic"
        assert entry["chat_id"] == -100123
        assert entry["thread_id"] == 42
        assert entry["window_id"] == "@0"
        assert entry["reason"] == "subagent_suffix_add"
        assert entry["payload"]["new_name"] == "james ⚡"
        assert "ts" in entry
        assert isinstance(entry["ts"], float)

    def test_never_raises(self, tmp_path):
        """log_action silently swallows all exceptions."""
        from ccgram.telegram_audit import log_action

        # Point to a path that can't be written (non-existent root dir)
        bad_path = Path("/nonexistent_root_dir/audit.jsonl")
        with patch("ccgram.telegram_audit._AUDIT_PATH", bad_path):
            # Must not raise
            log_action("send_message", chat_id=1, thread_id=None)

    def test_auto_detects_caller(self, tmp_path):
        """caller is auto-filled from inspect when not provided."""
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action("send_message", chat_id=1, thread_id=None, reason="test")

        entry = json.loads(audit_file.read_text().strip())
        assert ":" in entry["caller"]  # module:function format
        assert "test_telegram_audit" in entry["caller"]

    def test_explicit_caller_overrides_auto(self, tmp_path):
        """Explicit caller= overrides auto-detection."""
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action(
                "send_message", chat_id=1, thread_id=None, caller="mymod:myfunc"
            )

        entry = json.loads(audit_file.read_text().strip())
        assert entry["caller"] == "mymod:myfunc"

    def test_no_payload_key_when_none(self, tmp_path):
        """payload key is absent from entry when payload=None."""
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action("send_message", chat_id=1, thread_id=5)

        entry = json.loads(audit_file.read_text().strip())
        assert "payload" not in entry

    def test_multiple_entries_appended(self, tmp_path):
        """Multiple calls append multiple lines."""
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action("send_message", chat_id=1, thread_id=1, reason="a")
            log_action("edit_message", chat_id=1, thread_id=1, reason="b")
            log_action("edit_forum_topic", chat_id=1, thread_id=2, reason="c")

        lines = audit_file.read_text().splitlines()
        assert len(lines) == 3
        reasons = [json.loads(l)["reason"] for l in lines]
        assert reasons == ["a", "b", "c"]

    def test_rotation_on_size_exceeded(self, tmp_path):
        """Log file is rotated when it exceeds _MAX_SIZE."""
        from ccgram import telegram_audit
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        rotated = tmp_path / "telegram-audit.jsonl.1"

        # Pre-fill audit_file with data exceeding _MAX_SIZE
        big_content = "x" * (telegram_audit._MAX_SIZE + 1)
        audit_file.write_text(big_content)

        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action("send_message", chat_id=1, thread_id=1, reason="after_rotate")

        # Original should now be a fresh file with just our new entry
        lines = audit_file.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["reason"] == "after_rotate"

        # Rotated file should contain the old big content
        assert rotated.exists()
        assert rotated.stat().st_size == len(big_content)

    def test_thread_id_none_serialised(self, tmp_path):
        """thread_id=None is preserved as JSON null."""
        from ccgram.telegram_audit import log_action

        audit_file = tmp_path / "telegram-audit.jsonl"
        with patch("ccgram.telegram_audit._AUDIT_PATH", audit_file):
            log_action("send_message", chat_id=1, thread_id=None)

        entry = json.loads(audit_file.read_text().strip())
        assert entry["thread_id"] is None
