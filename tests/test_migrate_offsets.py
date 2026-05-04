"""Tests for scripts/migrate_offsets_v1.py — replay-safe offset migration."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the script directly (not a package)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "migrate_offsets_v1.py"
spec = importlib.util.spec_from_file_location("migrate_offsets_v1", _SCRIPT)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)

build_plan = _mod.build_plan
apply_updates = _mod.apply_updates
check_james_canary = _mod.check_james_canary
main = _mod.main
JAMES_CANARY_SID = _mod.JAMES_CANARY_SID
JAMES_CANARY_MIN_OFFSET = _mod.JAMES_CANARY_MIN_OFFSET


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CREATE_SESSIONS_TABLE = """
CREATE TABLE sessions (
    session_id          TEXT    PRIMARY KEY,
    cwd                 TEXT    NOT NULL,
    agent               TEXT    NOT NULL,
    mode                TEXT,
    status              TEXT    NOT NULL
        CHECK (status IN ('pending','active','errored','retired')),
    window_id           TEXT,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    provider_session_id TEXT,
    transcript_offset   INTEGER NOT NULL DEFAULT 0,
    transcript_path     TEXT
);
"""


def _make_db(path: Path, rows: list[dict] | None = None) -> Path:
    """Create a minimal sessions DB at *path*, optionally pre-populated."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_CREATE_SESSIONS_TABLE)
    conn.commit()
    if rows:
        for r in rows:
            conn.execute(
                "INSERT INTO sessions"
                " (session_id, cwd, agent, status, created_at, updated_at,"
                "  transcript_offset, transcript_path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["session_id"],
                    r.get("cwd", "/tmp"),
                    r.get("agent", "claude"),
                    r.get("status", "active"),
                    r.get("created_at", 0),
                    r.get("updated_at", 0),
                    r.get("transcript_offset", 0),
                    r.get("transcript_path"),
                ),
            )
        conn.commit()
    conn.close()
    return path


def _make_monitor_state(path: Path, sessions: dict[str, dict]) -> Path:
    """Write a monitor_state.json in the real CCGram schema."""
    tracked = {
        sid: {
            "session_id": sid,
            "file_path": v.get("transcript_path", ""),
            "last_byte_offset": v.get("offset", 0),
        }
        for sid, v in sessions.items()
    }
    path.write_text(json.dumps({"tracked_sessions": tracked, "events_offset": 0}))
    return path


# ---------------------------------------------------------------------------
# Helper: load DB rows
# ---------------------------------------------------------------------------

def _read_offset(db_path: Path, sid: str) -> int | None:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT transcript_offset FROM sessions WHERE session_id = ?", (sid,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMaxMsDbWins:
    """max(ms_offset, db_offset) determines the after-value."""

    def test_ms_larger(self):
        ms = {"sid-a": {"offset": 5000, "transcript_path": ""}}
        db = [{"session_id": "sid-a", "status": "active", "window_id": "@1",
               "transcript_offset": 3000, "transcript_path": None}]
        plan, anomalies = build_plan(ms, db)
        assert anomalies == []
        entry = next(e for e in plan if e["sid"] == "sid-a")
        assert entry["after"] == 5000
        assert entry["reason"] == "max_of_ms_db"

    def test_db_larger(self):
        ms = {"sid-b": {"offset": 2000, "transcript_path": ""}}
        db = [{"session_id": "sid-b", "status": "active", "window_id": "@2",
               "transcript_offset": 8000, "transcript_path": None}]
        plan, anomalies = build_plan(ms, db)
        assert anomalies == []
        entry = next(e for e in plan if e["sid"] == "sid-b")
        assert entry["after"] == 8000
        assert entry["reason"] == "max_of_ms_db"


class TestOrphanInJsonSkipped:
    """Session present in JSON but absent from DB must be skipped."""

    def test_orphan_skipped(self):
        ms = {"orphan-sid": {"offset": 12345, "transcript_path": "/tmp/foo.jsonl"}}
        db: list[dict] = []  # not in DB
        plan, anomalies = build_plan(ms, db)
        assert anomalies == []
        assert len(plan) == 1
        entry = plan[0]
        assert entry["sid"] == "orphan-sid"
        assert entry["skip"] is True
        assert entry["reason"] == "orphan_no_db_row"


class TestSynthesizeFileEnd:
    """Active DB session with db_offset=0, not in monitor_state, gets file-end."""

    def test_synthesize(self, tmp_path):
        # Create a real transcript file with known content
        tfile = tmp_path / "transcript.jsonl"
        content = b'{"type":"hello"}\n' * 100
        tfile.write_bytes(content)
        expected_size = len(content)

        ms: dict = {}  # not in monitor_state
        db = [{"session_id": "active-sid", "status": "active", "window_id": "@5",
               "transcript_offset": 0, "transcript_path": str(tfile)}]
        plan, anomalies = build_plan(ms, db)
        assert anomalies == []
        entry = next(e for e in plan if e["sid"] == "active-sid")
        assert entry["after"] == expected_size
        assert entry["reason"] == "synthesize_file_end"
        assert entry["skip"] is False


class TestAnomalyDetected:
    """When file is smaller than effective offset, anomaly is recorded, not plan."""

    def test_anomaly_file_smaller(self, tmp_path):
        tfile = tmp_path / "small.jsonl"
        tfile.write_bytes(b"x" * 100)  # only 100 bytes

        ms = {"anomaly-sid": {"offset": 9999, "transcript_path": str(tfile)}}
        db = [{"session_id": "anomaly-sid", "status": "active", "window_id": "@7",
               "transcript_offset": 0, "transcript_path": str(tfile)}]
        plan, anomalies = build_plan(ms, db)

        # Must NOT be in plan
        plan_sids = [e["sid"] for e in plan]
        assert "anomaly-sid" not in plan_sids

        # Must be in anomalies
        assert len(anomalies) == 1
        a = anomalies[0]
        assert a["sid"] == "anomaly-sid"
        assert a["file_size"] == 100
        assert a["effective"] == 9999

    def test_apply_refused_when_anomaly(self, tmp_path, monkeypatch, capsys):
        """main --apply must exit 2 when anomalies exist."""
        db_path = _make_db(tmp_path / "state.db", rows=[
            {"session_id": "anom-sid", "status": "active",
             "transcript_offset": 0, "transcript_path": None},
        ])
        # Create tiny transcript (10 bytes) but set ms_offset to a huge value
        tfile = tmp_path / "t.jsonl"
        tfile.write_bytes(b"x" * 10)

        ms_path = _make_monitor_state(
            tmp_path / "monitor_state.json",
            {"anom-sid": {"offset": 99999, "transcript_path": str(tfile)}},
        )
        # Also set transcript_path in DB so anomaly check fires
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE sessions SET transcript_path=? WHERE session_id=?",
                     (str(tfile), "anom-sid"))
        conn.commit()
        conn.close()

        monkeypatch.setattr(_mod, "CCGRAM_DIR", tmp_path)
        monkeypatch.setattr(_mod, "MONITOR_STATE_JSON", ms_path)
        monkeypatch.setattr(_mod, "STATE_DB", db_path)

        rc = main(["--apply"])
        assert rc == 2


class TestJamesCanaryRefused:
    """James canary regression: if after < 31137954, --apply must be refused."""

    def test_canary_error_string(self):
        plan = [{
            "sid": JAMES_CANARY_SID,
            "before_db": 0,
            "ms_offset": 0,
            "after": JAMES_CANARY_MIN_OFFSET - 1,
            "reason": "max_of_ms_db",
            "transcript_path": "",
            "skip": False,
        }]
        err = check_james_canary(plan)
        assert err is not None
        assert "canary" in err.lower() or JAMES_CANARY_SID[:8] in err

    def test_canary_ok_when_at_minimum(self):
        plan = [{
            "sid": JAMES_CANARY_SID,
            "before_db": JAMES_CANARY_MIN_OFFSET,
            "ms_offset": JAMES_CANARY_MIN_OFFSET,
            "after": JAMES_CANARY_MIN_OFFSET,
            "reason": "max_of_ms_db",
            "transcript_path": "",
            "skip": False,
        }]
        assert check_james_canary(plan) is None

    def test_main_apply_refused_on_low_canary(self, tmp_path, monkeypatch):
        """main(['--apply']) returns 2 when canary offset would be too low."""
        db_path = _make_db(tmp_path / "state.db", rows=[
            {"session_id": JAMES_CANARY_SID, "status": "active",
             "transcript_offset": 0, "transcript_path": None},
        ])
        ms_path = _make_monitor_state(
            tmp_path / "monitor_state.json",
            {JAMES_CANARY_SID: {"offset": JAMES_CANARY_MIN_OFFSET - 1,
                                 "transcript_path": ""}},
        )
        monkeypatch.setattr(_mod, "CCGRAM_DIR", tmp_path)
        monkeypatch.setattr(_mod, "MONITOR_STATE_JSON", ms_path)
        monkeypatch.setattr(_mod, "STATE_DB", db_path)

        rc = main(["--apply"])
        assert rc == 2


class TestApplyWritesSingleTx:
    """--apply writes all rows atomically and correctly."""

    def test_apply_updates_two_sessions(self, tmp_path, monkeypatch):
        db_path = _make_db(tmp_path / "state.db", rows=[
            {"session_id": "sess-1", "status": "active", "transcript_offset": 0,
             "transcript_path": None},
            {"session_id": "sess-2", "status": "active", "transcript_offset": 100,
             "transcript_path": None},
        ])
        ms_path = _make_monitor_state(
            tmp_path / "monitor_state.json",
            {
                "sess-1": {"offset": 5000, "transcript_path": ""},
                "sess-2": {"offset": 50, "transcript_path": ""},   # DB wins (100 > 50)
            },
        )
        monkeypatch.setattr(_mod, "CCGRAM_DIR", tmp_path)
        monkeypatch.setattr(_mod, "MONITOR_STATE_JSON", ms_path)
        monkeypatch.setattr(_mod, "STATE_DB", db_path)

        rc = main(["--apply"])
        assert rc == 0

        assert _read_offset(db_path, "sess-1") == 5000
        assert _read_offset(db_path, "sess-2") == 100  # max(50, 100)

    def test_apply_updates_directly(self, tmp_path):
        """Direct apply_updates call writes all rows in one TX."""
        db_path = _make_db(tmp_path / "state.db", rows=[
            {"session_id": "tx-1", "status": "active", "transcript_offset": 0},
            {"session_id": "tx-2", "status": "active", "transcript_offset": 0},
        ])
        plan = [
            {"sid": "tx-1", "after": 111, "skip": False},
            {"sid": "tx-2", "after": 222, "skip": False},
        ]
        counts = apply_updates(db_path, plan)
        assert counts["updated"] == 2
        assert counts["skipped"] == 0
        assert _read_offset(db_path, "tx-1") == 111
        assert _read_offset(db_path, "tx-2") == 222
