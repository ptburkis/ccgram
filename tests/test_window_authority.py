"""Tests for ccgram.window_authority -- single liveness oracle."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccgram import window_authority


# ---- Helpers -----------------------------------------------------------------


def _make_marker(tmp_path: Path, window_id: str) -> Path:
    """Write a minimal PTY marker file and return its path."""
    d = tmp_path / "active-sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{window_id}.json"
    p.write_text(json.dumps({"window_id": window_id, "session_id": "sess-abc"}))
    return p


# ---- Tests -------------------------------------------------------------------


class TestIsWindowAlive:
    def test_alive_when_marker_and_tmux_agree(self, tmp_path, monkeypatch):
        """Both PTY marker present and tmux reports window -> True."""
        _make_marker(tmp_path, "@5")

        # Patch read_marker_for_window to return our marker
        monkeypatch.setattr(
            window_authority,
            "is_window_alive",
            window_authority.is_window_alive,  # don't replace; patch the dep instead
        )
        with patch("ccgram.pty_markers.read_marker_for_window", return_value={"window_id": "@5"}):
            with patch(
                "subprocess.check_output",
                return_value="@3\n@5\n@7\n",
            ):
                assert window_authority.is_window_alive("@5") is True

    def test_dead_when_neither(self, monkeypatch):
        """No PTY marker and tmux doesn't have it -> False."""
        with patch("ccgram.pty_markers.read_marker_for_window", return_value=None):
            assert window_authority.is_window_alive("@99") is False

    def test_dead_when_marker_but_not_in_tmux(self, monkeypatch):
        """Marker exists but tmux does not list the window -> False."""
        with patch("ccgram.pty_markers.read_marker_for_window", return_value={"window_id": "@5"}):
            with patch("subprocess.check_output", return_value="@3\n@7\n"):
                assert window_authority.is_window_alive("@5") is False

    def test_alive_fail_safe_when_tmux_unreachable(self, monkeypatch):
        """Subprocess raises -> fail-safe True (don't retire on transient failure)."""
        with patch("ccgram.pty_markers.read_marker_for_window", return_value={"window_id": "@5"}):
            with patch("subprocess.check_output", side_effect=subprocess.SubprocessError("timeout")):
                assert window_authority.is_window_alive("@5") is True

    def test_alive_fail_safe_when_oserror(self, monkeypatch):
        """OSError (tmux not on PATH) -> fail-safe True."""
        with patch("ccgram.pty_markers.read_marker_for_window", return_value={"window_id": "@5"}):
            with patch("subprocess.check_output", side_effect=OSError("no such file")):
                assert window_authority.is_window_alive("@5") is True


class TestConfirmDeadOrSkip:
    def test_confirm_dead_or_skip_alive(self, caplog):
        """Window still alive -> returns False and logs WARNING."""
        with patch.object(window_authority, "is_window_alive", return_value=True):
            with caplog.at_level(logging.WARNING, logger="ccgram.window_authority"):
                result = window_authority.confirm_dead_or_skip("@5", "test_reason")
        assert result is False
        assert "skipped retirement" in caplog.text.lower() or "skipped retirement" in caplog.text

    def test_confirm_dead_or_skip_dead(self):
        """Both signals absent -> returns True."""
        with patch.object(window_authority, "is_window_alive", return_value=False):
            result = window_authority.confirm_dead_or_skip("@99", "test_reason")
        assert result is True


class TestAssertAliveOrRaise:
    def test_raises_when_dead(self):
        """Dead window -> WindowDeadError."""
        with patch.object(window_authority, "is_window_alive", return_value=False):
            with pytest.raises(window_authority.WindowDeadError):
                window_authority.assert_alive_or_raise("@99")

    def test_no_raise_when_alive(self):
        """Alive window -> no exception."""
        with patch.object(window_authority, "is_window_alive", return_value=True):
            window_authority.assert_alive_or_raise("@5")  # should not raise
