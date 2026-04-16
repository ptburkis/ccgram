"""Tests for SessionResolver.find_users_for_session DB-direct implementation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from ccgram import store
from ccgram.session_resolver import SessionResolver


def test_find_users_for_session_db_direct(tmp_path: Path) -> None:
    ccgram_home = tmp_path / ".ccgram"
    ccgram_home.mkdir()
    db_path = ccgram_home / "state.db"
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
        store.upsert_topic_binding_full(
            conn,
            100,
            42,
            "test-sid",
            1234,
            "@5",
            "myproject",
            int(time.time()),
        )
        conn.commit()
    finally:
        conn.close()

    resolver = SessionResolver()
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = resolver.find_users_for_session("test-sid")

    assert result == [(1234, "@5", 42)]
