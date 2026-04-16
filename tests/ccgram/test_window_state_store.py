"""Tests for WindowStateStore DB-backed lazy cache and DB write-through."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ccgram import store
from ccgram.window_state_store import WindowStateStore


def _seed_db(db_path: Path) -> None:
    store.init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        store.upsert_session(
            conn,
            session_id="test-sid",
            cwd="/projects/foo",
            agent="claude",
            status="active",
            window_id="@5",
            created_at=int(time.time()),
        )
        store.upsert_window_modes(
            conn,
            "@5",
            approval_mode="normal",
            batch_mode="verbose",
            notification_mode="all",
            provider_name="claude",
        )
        conn.commit()
    finally:
        conn.close()


def test_window_state_from_markers(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_db(db_path)

    marker = {
        "window_id": "@5",
        "session_id": "test-sid",
        "cwd": "/projects/foo",
        "transcript_path": "/path/t.jsonl",
        "window_name": "myproject",
        "provider": "claude",
    }

    wss = WindowStateStore()
    with (
        patch("ccgram.window_state_store._db_path", return_value=db_path),
        patch("ccgram.pty_markers.read_marker_for_window", return_value=marker),
    ):
        state = wss.get_window_state("@5")

    assert state.session_id == "test-sid"
    assert state.cwd == "/projects/foo"
    assert state.transcript_path == "/path/t.jsonl"
    assert state.window_name == "myproject"
    assert state.approval_mode == "normal"
    assert state.batch_mode == "verbose"
    assert state.notification_mode == "all"


def test_window_state_from_db_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_db(db_path)

    wss = WindowStateStore()
    with (
        patch("ccgram.window_state_store._db_path", return_value=db_path),
        patch("ccgram.pty_markers.read_marker_for_window", return_value=None),
    ):
        state = wss.get_window_state("@5")

    assert state.session_id == "test-sid"
    assert state.cwd == "/projects/foo"
    assert state.approval_mode == "normal"
    assert state.batch_mode == "verbose"
    assert state.notification_mode == "all"


def test_setter_writes_through_to_db(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store.init_db(db_path)

    wss = WindowStateStore()
    with patch("ccgram.window_state_store._db_path", return_value=db_path):
        wss.set_window_approval_mode("@7", "normal")
        wss.set_batch_mode("@7", "verbose")
        wss.set_notification_mode("@7", "all")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT approval_mode, batch_mode, notification_mode FROM window_modes WHERE window_id=?",
            ("@7",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["approval_mode"] == "normal"
    assert row["batch_mode"] == "verbose"
    assert row["notification_mode"] == "all"
