"""Tests for split session_id / provider_session_id refactor.

Verifies that:
  - Claude sessions keep both ids equal (no change in behaviour).
  - Codex sessions store the provider UUID in provider_session_id without
    overwriting the ccgram DB session_id in session_id.
  - Router routes on ccgram session_id directly (no window_id_hint needed).
  - Old-format session_map.json without provider_session_id migrates cleanly.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from ccgram.session_map import parse_session_map
from ccgram.window_state_store import WindowState


# ── Test 1: Claude session — both ids equal ───────────────────────────────────


def test_claude_session_both_ids_equal() -> None:
    """For Claude, session_id == provider_session_id after register_hookless_session."""
    from ccgram.session_map import SessionMapSync

    sync = SessionMapSync.__new__(SessionMapSync)
    sync._schedule_save = lambda: None

    fake_state = WindowState(session_id="aaa")
    fake_ws = MagicMock()
    fake_ws.get_window_state.return_value = fake_state

    # window_store is imported lazily inside the method, patch at its home module
    with patch("ccgram.window_state_store.window_store", fake_ws):
        sync.register_hookless_session(
            window_id="@1",
            session_id="aaa",  # same as ccgram id — Claude case
            cwd="/tmp",
            transcript_path="/tmp/aaa.jsonl",
            provider_name="claude",
        )

    assert fake_state.session_id == "aaa"
    assert fake_state.provider_session_id == "aaa"


# ── Test 2: Codex session — provider_session_id differs from session_id ───────


def test_codex_session_provider_id_differs() -> None:
    """For Codex, register_hookless_session preserves ccgram id, stores provider id."""
    from ccgram.session_map import SessionMapSync

    sync = SessionMapSync.__new__(SessionMapSync)
    sync._schedule_save = lambda: None

    # session_id set by session_lifecycle.create_session (ccgram DB id)
    fake_state = WindowState(session_id="ccgram-uuid")
    fake_ws = MagicMock()
    fake_ws.get_window_state.return_value = fake_state

    with patch("ccgram.window_state_store.window_store", fake_ws):
        sync.register_hookless_session(
            window_id="@42",
            session_id="codex-provider-uuid",  # provider-internal id
            cwd="/tmp",
            transcript_path="/tmp/rollout.jsonl",
            provider_name="codex",
        )

    # ccgram routing id must be unchanged
    assert fake_state.session_id == "ccgram-uuid"
    # provider UUID stored separately for file tracking
    assert fake_state.provider_session_id == "codex-provider-uuid"


# ── Test 3: Router routes on ccgram session_id directly (no hint needed) ──────


def test_router_routes_on_ccgram_session_id() -> None:
    """find_users_for_session matches on ccgram session_id without window_id_hint."""
    from ccgram.session_resolver import SessionResolver

    resolver = SessionResolver.__new__(SessionResolver)

    fake_state = MagicMock()
    fake_state.session_id = "ccgram-uuid"

    fake_ws = MagicMock()
    fake_ws.window_states = {"@42": fake_state}

    fake_tr = MagicMock()
    fake_tr.iter_thread_bindings.return_value = [(42, 100, "@42")]

    with (
        patch("ccgram.session_resolver.window_store", fake_ws),
        patch("ccgram.session_resolver.thread_router", fake_tr),
    ):
        result = resolver.find_users_for_session("ccgram-uuid")  # no hint

    assert result == [(42, "@42", 100)]


# ── Test 4: Old-format session_map migration (absent provider_session_id) ─────


def test_old_format_session_map_migration() -> None:
    """Loading a session_map.json without provider_session_id uses session_id as fallback."""
    raw = {
        "ccgram:@42": {
            "session_id": "old-session-uuid",
            "cwd": "/tmp/project",
            # no provider_session_id field — old format
        }
    }
    result = parse_session_map(raw, "ccgram:")

    assert "@42" in result
    entry = result["@42"]
    # provider_session_id must fall back to session_id for back-compat
    assert entry["provider_session_id"] == entry["session_id"]
    assert entry["provider_session_id"] == "old-session-uuid"
