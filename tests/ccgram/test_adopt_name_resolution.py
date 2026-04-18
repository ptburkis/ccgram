"""Tests for _is_bare_window_id and _resolve_adopt_name in sync_command."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.sync_command import _is_bare_window_id, _resolve_adopt_name


# ---------------------------------------------------------------------------
# _is_bare_window_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("@7", True),
        ("@422", True),
        ("@0", True),
        ("memory-dreams", False),
        ("james-claude-hub", False),
        ("bulugo_lead_gen", False),
        ("@", False),
        ("@abc", False),
        ("7", False),
        ("", False),
    ],
)
def test_is_bare_window_id(name, expected):
    assert _is_bare_window_id(name) == expected


# ---------------------------------------------------------------------------
# _resolve_adopt_name — helpers
# ---------------------------------------------------------------------------


def _ws(window_name="", cwd=""):
    return SimpleNamespace(window_name=window_name, cwd=cwd)


# ---------------------------------------------------------------------------
# _resolve_adopt_name — cwd fallback when window_name is bare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_adopt_name_bare_window_name_uses_cwd():
    """When window_name is a bare @N ID, falls back to cwd basename."""
    ws = _ws(window_name="@19", cwd="/home/peter/memory-dreams")

    with (
        patch(
            "ccgram.handlers.sync_command.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ccgram.handlers.sync_command.tmux_manager.find_window_by_id",
            new=AsyncMock(return_value=None),
        ),
        patch("ccgram.store.connect", side_effect=RuntimeError("no db in tests")),
    ):
        result = await _resolve_adopt_name("@19", ws)

    assert result == "memory-dreams"


@pytest.mark.asyncio
async def test_resolve_adopt_name_good_window_name_returned_immediately():
    """When window_name is already meaningful, return it without fallback."""
    ws = _ws(window_name="bulugo-dev", cwd="/home/peter/projects/bulugo_lead_gen")
    result = await _resolve_adopt_name("@5", ws)
    assert result == "bulugo-dev"


@pytest.mark.asyncio
async def test_resolve_adopt_name_pty_marker_overrides_bare_id():
    """PTY marker window_name wins over bare window_name."""
    ws = _ws(window_name="@3", cwd="/tmp/unknown")

    async def fake_to_thread(fn, *args, **kwargs):
        return {"window_name": "pippa-hq"}

    with patch("ccgram.handlers.sync_command.asyncio.to_thread", side_effect=fake_to_thread):
        result = await _resolve_adopt_name("@3", ws)

    assert result == "pippa-hq"


@pytest.mark.asyncio
async def test_resolve_adopt_name_falls_back_to_window_id_when_cwd_empty():
    """Last resort: return window_id when nothing else is available."""
    ws = _ws(window_name="@99", cwd="")

    with (
        patch(
            "ccgram.handlers.sync_command.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ccgram.handlers.sync_command.tmux_manager.find_window_by_id",
            new=AsyncMock(return_value=None),
        ),
        patch("ccgram.store.connect", side_effect=RuntimeError("no db in tests")),
    ):
        result = await _resolve_adopt_name("@99", ws)

    assert result == "@99"
