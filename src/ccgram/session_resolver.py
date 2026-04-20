"""JSONL session resolution — window-to-session lookup and message history.

Resolves tmux windows to Claude Code session files on disk, reading JSONL
transcripts to extract session summaries and message history.

Key class: SessionResolver (singleton instantiated as ``session_resolver``).
Key type: ClaudeSession.

Responsibilities:
  - Build JSONL file paths from session_id + cwd.
  - Resolve window_id → ClaudeSession by reading the JSONL on disk.
  - Find users bound to a given session_id (cross-reference with thread_router).
  - Read paginated message history from a session's JSONL file.
"""

from __future__ import annotations

import json
import structlog
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiofiles

from .config import config
from .providers import get_provider_for_window
from .thread_router import thread_router
from .window_state_store import window_store

logger = structlog.get_logger()


@dataclass
class ClaudeSession:
    """Information about a Claude Code session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


def _session_map_fallback(
    session_id: str,
    conn: "Any",
    result: "list[tuple[int, str, int]]",
) -> None:
    """Populate result from session_map.json when the DB lookup found nothing.

    session_map.json is written by the SessionStart hook and is always current,
    so it bridges the gap when topic_bindings.session_id hasn't caught up to a
    rotation yet.  Also self-heals the DB row so the next call uses the fast path.
    """
    try:
        if not config.session_map_file.exists():
            return
        sm = json.loads(config.session_map_file.read_text())
        for key, details in sm.items():
            if details.get("session_id") != session_id:
                continue
            win_id = key.split(":", 1)[1] if ":" in key else key
            rows = conn.execute(
                "SELECT user_id, window_id, topic_id FROM topic_bindings "
                "WHERE window_id=? AND user_id IS NOT NULL",
                (win_id,),
            ).fetchall()
            if rows:
                result.extend((r[0], r[1], r[2]) for r in rows)
                conn.execute(
                    "UPDATE topic_bindings SET session_id=? WHERE window_id=?",
                    (session_id, win_id),
                )
                conn.commit()
                logger.info(
                    "find_users: self-healed session_id %s -> window %s",
                    session_id[:8], win_id,
                )
            break
    except Exception:
        logger.debug("find_users: session_map fallback failed", exc_info=True)


class SessionResolver:
    """Resolves tmux windows to Claude session files and reads message history."""

    def _build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Build the direct file path for a session from session_id and cwd."""
        if not session_id or not cwd:
            return None
        encoded_cwd = cwd.replace("/", "-")
        return config.claude_projects_path / encoded_cwd / f"{session_id}.jsonl"

    def _session_from_transcript_path(
        self,
        window_id: str,
        state: Any,
    ) -> ClaudeSession | None:
        """Build a lightweight session object from persisted transcript_path."""
        transcript = state.transcript_path
        if not transcript:
            return None
        file_path = Path(transcript)
        if not file_path.exists():
            return None
        summary = (
            state.window_name or thread_router.get_display_name(window_id) or "Untitled"
        )
        return ClaudeSession(
            session_id=state.session_id,
            summary=summary,
            message_count=-1,
            file_path=str(file_path),
        )

    def _resolve_session_file(
        self, session_id: str, cwd: str, window_id: str
    ) -> Path | None:
        """Return the JSONL path for a session, using glob fallback if needed.

        When the direct path is missing, searches via glob. If found and the
        decoded cwd is an existing directory, updates the window's stored cwd.
        """
        file_path = self._build_session_file_path(session_id, cwd)
        if file_path and file_path.exists():
            return file_path

        pattern = f"*/{session_id}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if not matches:
            return None

        file_path = matches[0]
        logger.debug("Found session via glob: %s", file_path)
        encoded_dir = file_path.parent.name
        decoded_cwd = encoded_dir.replace("-", "/")
        if window_id and decoded_cwd.startswith("/") and Path(decoded_cwd).is_dir():
            state = window_store.window_states.get(window_id)
            if state and state.cwd != decoded_cwd:
                logger.info(
                    "Glob fallback: updating cwd for window %s: %r -> %r",
                    window_id,
                    state.cwd,
                    decoded_cwd,
                )
                state.cwd = decoded_cwd
                window_store._schedule_save()
        return file_path

    async def _read_session_summary(
        self, file_path: Path, session_id: str, window_id: str
    ) -> ClaudeSession | None:
        """Read a JSONL session file and extract summary and message count."""
        summary = ""
        last_user_msg = ""
        message_count = 0
        provider = get_provider_for_window(window_id)
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        elif provider.is_user_transcript_entry(data):
                            parsed = provider.parse_history_entry(data)
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return ClaudeSession(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    async def _get_session_direct(
        self, session_id: str, cwd: str, window_id: str = ""
    ) -> ClaudeSession | None:
        """Get a ClaudeSession directly from session_id and cwd (no scanning)."""
        file_path = self._resolve_session_file(session_id, cwd, window_id)
        if not file_path:
            return None
        return await self._read_session_summary(file_path, session_id, window_id)

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Resolve a tmux window to the best matching Claude session.

        Uses persisted session_id + cwd to construct file path directly.
        Returns None if no session is associated with this window.
        """
        state = window_store.get_window_state(window_id)

        if not state.session_id or not state.cwd:
            return None

        direct = self._session_from_transcript_path(window_id, state)
        if direct:
            return direct

        session = await self._get_session_direct(state.session_id, state.cwd, window_id)
        if session:
            return session

        provider = get_provider_for_window(window_id)
        if not provider.capabilities.supports_hook:
            logger.debug(
                "Hookless session unresolved for window_id %s "
                "(sid=%s, transcript_path=%s); keeping state",
                window_id,
                state.session_id,
                state.transcript_path,
            )
            return None

        logger.debug(
            "Session file no longer exists for window_id %s (sid=%s, cwd=%s)",
            window_id,
            state.session_id,
            state.cwd,
        )
        state.session_id = ""
        state.cwd = ""
        window_store._schedule_save()
        return None

    def find_users_for_session(
        self,
        session_id: str,
        *,
        window_id_hint: str = "",
    ) -> list[tuple[int, str, int]]:
        """Find users whose thread-bound window maps to session_id.

        Marker-oracle routing: resolves session_id -> window_id via PTY marker,
        then queries topic_bindings by window_id. Insulates from session_id churn
        caused by Task subagent spawns reusing window_ids.

        Last-resort fallback: if the DB query returns nothing, searches
        session_map.json (always current — hooks update it on every SessionStart)
        and self-heals topic_bindings.session_id so subsequent lookups are fast.
        """
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        result: list[tuple[int, str, int]] = []
        try:
            resolved_window_id: str | None = None
            try:
                from .pty_markers import find_marker_by_session_id
                marker = find_marker_by_session_id(session_id)
                if marker:
                    resolved_window_id = marker.get('window_id')
            except Exception:
                pass

            if not resolved_window_id and window_id_hint:
                resolved_window_id = window_id_hint

            db_path = _Path.home() / ".ccgram" / "state.db"
            if not db_path.exists():
                return result
            conn = _sqlite3.connect(str(db_path))
            try:
                if resolved_window_id:
                    rows = conn.execute(
                        "SELECT user_id, window_id, topic_id FROM topic_bindings "
                        "WHERE window_id=? AND user_id IS NOT NULL AND window_id IS NOT NULL",
                        (resolved_window_id,),
                    ).fetchall()
                else:
                    # Fallback: query by session_id (old behaviour)
                    rows = conn.execute(
                        "SELECT user_id, window_id, topic_id FROM topic_bindings "
                        "WHERE session_id=? AND user_id IS NOT NULL AND window_id IS NOT NULL",
                        (session_id,),
                    ).fetchall()
                result = [(r[0], r[1], r[2]) for r in rows]

                if not result:
                    _session_map_fallback(session_id, conn, result)
            finally:
                conn.close()
        except Exception:
            logger.debug("find_users_for_session DB query failed", exc_info=True)
        return result

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        provider = get_provider_for_window(window_id)
        entries: list[dict[str, Any]] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = provider.parse_transcript_line(line)
                    if data:
                        entries.append(data)
        except OSError:
            logger.exception("Error reading session file %s", file_path)
            return [], 0

        agent_messages, _ = provider.parse_transcript_entries(entries, {})
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in agent_messages
        ]

        return all_messages, len(all_messages)


session_resolver = SessionResolver()
