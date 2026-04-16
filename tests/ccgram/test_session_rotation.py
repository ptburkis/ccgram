"""Tests for session rotation handling — hook path + inotify path.

Covers:
- SessionStart hook payload → full state update (session_map, monitor_state, DB, binding FK)
- Inotify event on new jsonl when hook didn't fire → same convergence
- Idempotency: re-running same input is a no-op
- Race: hook + inotify both fire within 100ms → only one update lands
- Legacy window (no env marker, multiple same-cwd windows) → hook uses window_key;
  inotify picks most-recent-message window + logs WARNING
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.providers.base import HookEvent


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_hook_event(
    event_type: str = "SessionStart",
    window_key: str = "ccgram:@5",
    session_id: str = "new-sid-0001-0001-0001-000000000001",
    data: dict | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        window_key=window_key,
        session_id=session_id,
        data=data
        or {
            "cwd": "/home/peter/projects/james",
            "transcript_path": "/home/peter/.claude/projects/-home-peter-projects-james/new-sid-0001-0001-0001-000000000001.jsonl",
        },
        timestamp=time.time(),
    )


def _write_session_map(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


def _write_monitor_state(path: Path, tracked: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tracked_sessions": tracked}))


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def ccgram_dir(tmp_path, monkeypatch) -> Path:
    """Redirect config to a temp dir."""
    from ccgram import config as cfg_module

    d = tmp_path / ".ccgram"
    d.mkdir()
    monkeypatch.setattr(cfg_module.config, "config_dir", d)
    monkeypatch.setattr(cfg_module.config, "session_map_file", d / "session_map.json")
    monkeypatch.setattr(cfg_module.config, "monitor_state_file", d / "monitor_state.json")
    return d


@pytest.fixture()
def db_path(ccgram_dir) -> Path:
    from ccgram.store import init_db

    p = ccgram_dir / "state.db"
    init_db(p)
    return p


@pytest.fixture()
def bot() -> MagicMock:
    b = AsyncMock()
    b.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    return b


# ─── 1. SessionStart hook: full state update ──────────────────────────────────


class TestSessionStartHook:
    OLD_SID = "old-sid-0000-0000-0000-000000000000"
    NEW_SID = "new-sid-0001-0001-0001-000000000001"
    WINDOW_ID = "@5"
    CWD = "/home/peter/projects/james"

    def _seed_session_map(self, ccgram_dir: Path) -> None:
        _write_session_map(
            ccgram_dir / "session_map.json",
            {
                f"ccgram:{self.WINDOW_ID}": {
                    "session_id": self.OLD_SID,
                    "cwd": self.CWD,
                    "window_name": "james",
                    "transcript_path": f"/home/peter/.claude/projects/-home-peter-projects-james/{self.OLD_SID}.jsonl",
                    "provider_name": "claude",
                }
            },
        )
        _write_monitor_state(
            ccgram_dir / "monitor_state.json",
            {
                self.OLD_SID: {
                    "session_id": self.OLD_SID,
                    "file_path": f".../{self.OLD_SID}.jsonl",
                    "last_byte_offset": 1234,
                }
            },
        )

    def _seed_window_store(self) -> None:
        from ccgram.window_state_store import window_store

        state = window_store.get_window_state(self.WINDOW_ID)
        state.session_id = self.OLD_SID
        state.cwd = self.CWD
        state.provider_name = "claude"

    @pytest.mark.asyncio()
    async def test_session_map_updated(self, ccgram_dir, db_path, bot, monkeypatch):
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()

        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([]),
        )

        from ccgram.handlers.hook_events import _handle_session_start, _rotation_notice_last

        _rotation_notice_last.clear()
        event = _make_hook_event(
            session_id=self.NEW_SID,
            data={
                "cwd": self.CWD,
                "transcript_path": f"/home/peter/.claude/projects/-home-peter-projects-james/{self.NEW_SID}.jsonl",
            },
        )
        with patch("ccgram.session.session_manager.load_session_map", new_callable=AsyncMock):
            await _handle_session_start(event, bot)

        updated = json.loads((ccgram_dir / "session_map.json").read_text())
        entry = updated.get(f"ccgram:{self.WINDOW_ID}", {})
        assert entry.get("session_id") == self.NEW_SID

    @pytest.mark.asyncio()
    async def test_monitor_state_new_session_at_offset_zero(
        self, ccgram_dir, db_path, bot, monkeypatch, tmp_path
    ):
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([]),
        )
        # Create a fake transcript so file_size can be measured.
        projects_dir = tmp_path / ".claude" / "projects" / "-home-peter-projects-james"
        projects_dir.mkdir(parents=True)
        transcript = projects_dir / f"{self.NEW_SID}.jsonl"
        transcript.write_text('{"type":"system"}\n')

        from ccgram.handlers.hook_events import _handle_session_start, _rotation_notice_last

        _rotation_notice_last.clear()
        event = _make_hook_event(
            session_id=self.NEW_SID,
            data={"cwd": self.CWD, "transcript_path": str(transcript)},
        )
        with patch("ccgram.session.session_manager.load_session_map", new_callable=AsyncMock):
            await _handle_session_start(event, bot)

        state = json.loads((ccgram_dir / "monitor_state.json").read_text())
        tracked = state.get("tracked_sessions", {})
        assert self.NEW_SID in tracked
        assert self.OLD_SID not in tracked

    @pytest.mark.asyncio()
    async def test_db_session_upserted(self, ccgram_dir, db_path, bot, monkeypatch):
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([]),
        )
        from ccgram import store
        from ccgram.handlers.hook_events import _handle_session_start, _rotation_notice_last

        _rotation_notice_last.clear()

        with patch("ccgram.store.db_path", return_value=db_path):
            event = _make_hook_event(
                session_id=self.NEW_SID,
                data={
                    "cwd": self.CWD,
                    "transcript_path": f"/nonexistent/{self.NEW_SID}.jsonl",
                },
            )
            with patch("ccgram.session.session_manager.load_session_map", new_callable=AsyncMock):
                await _handle_session_start(event, bot)

            with store.connect(db_path) as conn:
                session = store.get_session(conn, self.NEW_SID)
            assert session is not None
            assert session.window_id == self.WINDOW_ID
            assert session.status == "active"

    @pytest.mark.asyncio()
    async def test_db_binding_repointed(self, ccgram_dir, db_path, bot, monkeypatch):
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([]),
        )
        from ccgram import store

        # Seed the DB with old session + binding.
        with store.connect(db_path) as conn:
            store.upsert_session(
                conn,
                session_id=self.OLD_SID,
                cwd=self.CWD,
                agent="claude",
                status="active",
                window_id=self.WINDOW_ID,
            )
            store.upsert_topic_binding(
                conn,
                group_id=-100111,
                topic_id=42,
                session_id=self.OLD_SID,
                topic_title="james",
            )

        from ccgram.handlers.hook_events import _handle_session_start, _rotation_notice_last

        _rotation_notice_last.clear()
        with patch("ccgram.store.db_path", return_value=db_path):
            event = _make_hook_event(
                session_id=self.NEW_SID,
                data={
                    "cwd": self.CWD,
                    "transcript_path": f"/nonexistent/{self.NEW_SID}.jsonl",
                },
            )
            with patch("ccgram.session.session_manager.load_session_map", new_callable=AsyncMock):
                await _handle_session_start(event, bot)

            with store.connect(db_path) as conn:
                binding = store.get_binding_for_session(conn, self.NEW_SID)
        assert binding is not None
        assert binding.topic_id == 42

    @pytest.mark.asyncio()
    async def test_idempotent_same_session_id(self, ccgram_dir, db_path, bot, monkeypatch):
        """Delivering the same SessionStart twice is a no-op on the second call."""
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([]),
        )
        from ccgram.handlers.hook_events import _handle_session_start, _rotation_notice_last
        from ccgram.window_state_store import window_store

        _rotation_notice_last.clear()
        event = _make_hook_event(session_id=self.NEW_SID)

        update_calls: list[str] = []
        async def fake_apply(**kwargs):
            update_calls.append(kwargs["new_sid"])
            # Also update window_store so second call sees unchanged session
            state2 = window_store.get_window_state(self.WINDOW_ID)
            state2.session_id = kwargs["new_sid"]

        with (
            patch("ccgram.session_autoheal.apply_session_update", side_effect=fake_apply),
            patch("ccgram.session.session_manager.load_session_map", new_callable=AsyncMock),
        ):
            await _handle_session_start(event, bot)
            await _handle_session_start(event, bot)

        assert update_calls == [self.NEW_SID], "apply_session_update must be called exactly once"


# ─── 2. Inotify path: same convergence when hook didn't fire ──────────────────


class TestInotifyPath:
    OLD_SID = "old-sid-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    NEW_SID = "new-sid-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    WINDOW_ID = "@7"
    CWD = "/home/peter/projects/bulugo"
    SLUG = "-home-peter-projects-bulugo"

    def _seed(self, ccgram_dir: Path) -> None:
        _write_session_map(
            ccgram_dir / "session_map.json",
            {
                f"ccgram:{self.WINDOW_ID}": {
                    "session_id": self.OLD_SID,
                    "cwd": self.CWD,
                    "window_name": "bulugo",
                    "transcript_path": f".../{self.OLD_SID}.jsonl",
                    "provider_name": "claude",
                }
            },
        )
        _write_monitor_state(ccgram_dir / "monitor_state.json", {})

    @pytest.mark.asyncio()
    async def test_inotify_updates_same_state_as_hook(self, ccgram_dir, monkeypatch, tmp_path):
        self._seed(ccgram_dir)

        # Build the fake jsonl file.
        projects = tmp_path / ".claude" / "projects" / self.SLUG
        projects.mkdir(parents=True)
        jsonl = projects / f"{self.NEW_SID}.jsonl"
        jsonl.write_bytes(b'{"type":"system"}\n')

        # Patch resolve_session_identity to return None (no env marker set).
        monkeypatch.setattr(
            "ccgram.session_watcher.resolve_session_identity", lambda wid: None
        )

        from ccgram.session_watcher import _find_window_for_jsonl

        result = await asyncio.to_thread(_find_window_for_jsonl, jsonl, b"")
        assert result is not None
        window_id, window_name, current_sid = result
        assert window_id == self.WINDOW_ID
        assert current_sid == self.OLD_SID

    @pytest.mark.asyncio()
    async def test_inotify_idempotent_already_tracked(self, ccgram_dir, monkeypatch, tmp_path):
        """If session_map already shows NEW_SID, _find_window_for_jsonl returns it
        and _process_new_jsonl bails out before calling apply_session_update."""
        # Seed session_map already at new_sid.
        _write_session_map(
            ccgram_dir / "session_map.json",
            {
                f"ccgram:{self.WINDOW_ID}": {
                    "session_id": self.NEW_SID,
                    "cwd": self.CWD,
                    "window_name": "bulugo",
                    "transcript_path": f".../{self.NEW_SID}.jsonl",
                    "provider_name": "claude",
                }
            },
        )
        _write_monitor_state(ccgram_dir / "monitor_state.json", {})

        projects = tmp_path / ".claude" / "projects" / self.SLUG
        projects.mkdir(parents=True)
        jsonl = projects / f"{self.NEW_SID}.jsonl"
        jsonl.write_bytes(b'{"type":"system"}\n')

        monkeypatch.setattr(
            "ccgram.session_watcher.resolve_session_identity", lambda wid: None
        )

        apply_calls: list = []

        async def fake_apply(**kwargs):
            apply_calls.append(kwargs)

        with patch("ccgram.session_autoheal.apply_session_update", side_effect=fake_apply):
            from ccgram.session_watcher import _process_new_jsonl
            await _process_new_jsonl(jsonl)

        # apply_session_update must NOT be called because new_sid == old_sid in session_map.
        assert apply_calls == []


# ─── 3. Race: hook + inotify within 100ms → only one update ──────────────────


class TestRaceCondition:
    OLD_SID = "old-sid-race-0000-0000-000000000000"
    NEW_SID = "new-sid-race-1111-1111-111111111111"
    WINDOW_ID = "@9"
    CWD = "/home/peter/projects/race"

    @pytest.mark.asyncio()
    async def test_concurrent_signals_single_update(self, ccgram_dir, monkeypatch, tmp_path):
        """Concurrent hook + inotify both calling apply_session_update → CAS guard
        in _update_session_map_sync ensures only one succeeds."""
        from ccgram.session_autoheal import _update_session_map_sync

        _write_session_map(
            ccgram_dir / "session_map.json",
            {
                f"ccgram:{self.WINDOW_ID}": {
                    "session_id": self.OLD_SID,
                    "cwd": self.CWD,
                    "window_name": "race",
                    "transcript_path": f".../{self.OLD_SID}.jsonl",
                    "provider_name": "claude",
                }
            },
        )

        # Run both "hook" and "inotify" updates concurrently.
        results = await asyncio.gather(
            asyncio.to_thread(
                _update_session_map_sync,
                self.WINDOW_ID,
                self.OLD_SID,
                self.NEW_SID,
                f".../{self.NEW_SID}.jsonl",
            ),
            asyncio.to_thread(
                _update_session_map_sync,
                self.WINDOW_ID,
                self.OLD_SID,
                self.NEW_SID,
                f".../{self.NEW_SID}.jsonl",
            ),
        )

        # Exactly one should succeed (True), the other should be a no-op (False),
        # because the CAS guard checks current_sid_in_map == old_sid.
        assert sorted(results) == [False, True]

        # Final session_map should show NEW_SID.
        updated = json.loads((ccgram_dir / "session_map.json").read_text())
        assert updated[f"ccgram:{self.WINDOW_ID}"]["session_id"] == self.NEW_SID


# ─── 4. Legacy window: multiple same-cwd, inotify picks most-recent ───────────


class TestLegacyWindowAmbiguous:
    CWD = "/home/peter/projects/shared"
    SLUG = "-home-peter-projects-shared"
    NEW_SID = "new-sid-cccc-cccc-cccc-cccccccccccc"

    @pytest.mark.asyncio()
    async def test_inotify_warns_and_picks_most_recent_on_ambiguous_cwd(
        self, ccgram_dir, monkeypatch, tmp_path, capsys
    ):
        """When multiple windows share cwd, inotify warns and picks max session_id.

        The warning goes to structlog (stdout), not stdlib logging, so we use capsys.
        """
        _write_session_map(
            ccgram_dir / "session_map.json",
            {
                "ccgram:@10": {
                    "session_id": "sid-aaaa-older",
                    "cwd": self.CWD,
                    "window_name": "shared-a",
                    "provider_name": "claude",
                },
                "ccgram:@11": {
                    "session_id": "sid-zzzz-newer",
                    "cwd": self.CWD,
                    "window_name": "shared-b",
                    "provider_name": "claude",
                },
            },
        )

        projects = tmp_path / ".claude" / "projects" / self.SLUG
        projects.mkdir(parents=True)
        jsonl = projects / f"{self.NEW_SID}.jsonl"
        jsonl.write_bytes(b'{"type":"system"}\n')

        # No env marker for any window.
        monkeypatch.setattr(
            "ccgram.session_watcher.resolve_session_identity", lambda wid: None
        )

        from ccgram.session_watcher import _find_window_for_jsonl

        result = await asyncio.to_thread(_find_window_for_jsonl, jsonl, b"")

        assert result is not None
        window_id, window_name, current_sid = result
        # Should pick @11 (sid-zzzz-newer > sid-aaaa-older lexicographically)
        assert window_id == "@11"
        # Warning is emitted via structlog to stdout — verify it was produced.
        captured = capsys.readouterr()
        assert "ambiguous" in captured.out.lower()
