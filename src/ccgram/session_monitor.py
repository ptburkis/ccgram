"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Loads the current session_map to know which sessions to watch.
  2. Detects session_map changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import os
import structlog
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import aiofiles
from telegram.error import TelegramError

from .claude_task_state import claude_task_state
from .config import config
from .monitor_state import MonitorState, TrackedSession, _PERIODIC_SAVE_INTERVAL
from .providers import (
    detect_provider_from_transcript_path,
    get_provider_for_window,
    registry,
)
from .session import parse_session_map
from .tmux_manager import tmux_manager
from .debug_timeline import get_timeline
from .utils import (
    log_throttle_reset,
    log_throttled,
    read_cwd_from_jsonl,
    task_done_callback,
)

_CallbackError = Exception
# Top-level loop resilience: catch any error to keep monitoring alive
_LoopError = (OSError, RuntimeError, json.JSONDecodeError, ValueError, TelegramError)

# Exponential backoff bounds for loop errors (seconds)
_BACKOFF_MIN = 2.0
_BACKOFF_MAX = 30.0

logger = structlog.get_logger()


def _log_mutation(event: str, **kwargs) -> None:
    import traceback
    logger.warning("MUTATION: %s %s caller=%s", event, kwargs, traceback.extract_stack(limit=4)[-2:])


_PathResolveError = (OSError, ValueError)
_SessionMapError = (json.JSONDecodeError, OSError)

_MSG_PREVIEW_LENGTH = 80

# Catch-up playback cap: when resuming from a non-zero offset and the batch
# of assistant-text messages exceeds this threshold, only ship the last N and
# emit one summary notice. Configurable via CCGRAM_CATCHUP_CAP env var.
_CATCHUP_CAP: int = int(os.environ.get("CCGRAM_CATCHUP_CAP", "10"))
# Per-session: monotonic time of last "caught up" notice (debounce 60s).
_catchup_notice_last: dict[str, float] = {}  # session_id -> monotonic
_CATCHUP_DEBOUNCE_SECS: float = 60.0

# How often reconcile_session_map() actually does work, regardless of how
# often the monitor loop calls it. Rate-limits project-dir scans so we don't
# stat the filesystem every 2 seconds. 30s is fast enough that drift heals
# before the user notices but slow enough not to be a hot loop.
_RECONCILE_INTERVAL_SECS = 30.0

# Maximum age (seconds) of a Claude jsonl file to consider it an active
# session for reconciliation. Older files are assumed stale and ignored.
_RECONCILE_MAX_JSONL_AGE_SECS = 3600.0

# System-wrapper markers that appear in user-role jsonl entries but are not
# real human messages (tool results, task notifications, etc.). The
# reconciler's backfill-offset-finder skips these when picking the "last
# real user turn" to rewind to.
_SYSTEM_WRAPPER_MARKERS = (
    "<task-notification>",
    "<tool_use_error>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)


def _count_lines_in_range(file_path: "Path", start: int, end: int) -> int:
    """Approximate line count between two byte offsets -- one line ~= one JSONL entry.
    Returns 0 on any read error (caller treats that as "can't count, allow")."""
    if start >= end:
        return 0
    try:
        with open(file_path, "rb") as f:
            f.seek(start)
            data = f.read(end - start)
    except OSError:
        return 0
    return data.count(b"\n")


def _clamp_backfill_offset(
    file_path: "Path",
    proposed_offset: int,
    file_size: int,
    session_id: str,
    cap: int,
    reason: str,
) -> int:
    """If queueing from proposed_offset to EOF exceeds cap messages, return
    file_size (skip-to-EOF) and warn. Otherwise return proposed_offset unchanged.
    reason describes the call site for logs."""
    if proposed_offset >= file_size or cap <= 0:
        return proposed_offset
    line_count = _count_lines_in_range(file_path, proposed_offset, file_size)
    if line_count > cap:
        logger.warning(
            "Backfill cap hit for session %s (%s): %d messages between "
            "offset %d and EOF %d > cap %d. Skipping to EOF; treat as bug.",
            session_id, reason, line_count, proposed_offset, file_size, cap,
        )
        return file_size
    return proposed_offset


def _find_last_user_turn_offset(file_path: "Path") -> int:
    """Find the byte offset of the last REAL human user message in a Claude jsonl.

    "Real" means type=user, content is non-empty text, and the text doesn't
    start with any of the system-wrapper markers (tool results, task
    notifications, etc.). This is used by the reconciler to pick an initial
    read offset when it inserts a recovered session_map entry: we rewind to
    the start of the last real turn so the user sees the most recent
    conversation context mirror to Telegram, without flooding them with
    the entire session history.

    Returns the byte offset of the last real user line, or the file size
    (no backfill) if no qualifying line is found or on any read error.
    """
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except OSError:
        return 0

    try:
        text = data.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, ValueError):
        return len(data)

    # Walk line starts so we can map back from parsed line index to byte offset
    raw_lines = text.split("\n")
    line_offsets: list[int] = []
    cum = 0
    for ln in raw_lines:
        line_offsets.append(cum)
        cum += len(ln.encode("utf-8")) + 1  # +1 for the \n delimiter

    for i in range(len(raw_lines) - 1, -1, -1):
        line = raw_lines[i].strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "user":
            continue
        msg = d.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            t = content
        elif isinstance(content, list):
            t = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    break
        else:
            t = ""
        if not t:
            continue
        stripped = t.lstrip()
        if any(stripped.startswith(m) for m in _SYSTEM_WRAPPER_MARKERS):
            continue
        return line_offsets[i]

    # No real user turn found — default to end of file (no backfill).
    return len(data)


# Tmux session-name prefix that indicates a temporary grouped mirror created
# by the web terminal (terminal-server.py spawns "web-<window>-<uuid>" sessions
# in the same tmux group as the canonical session). When Claude Code's stop
# hook resolves the pane, tmux's display-message can return the mirror name
# instead of the canonical, producing window_keys that look distinct but refer
# to the same pane. We canonicalize at read time so downstream lookups all
# converge on the canonical "<tmux_session_name>:@<id>" form.
_WEB_MIRROR_SESSION_PREFIX = "web-"


def _canonicalize_window_key(window_key: str) -> str:
    """Strip web-terminal grouped-mirror prefixes from a window_key.

    Replaces "web-<anything>:<window_id>" with "<canonical>:<window_id>", where
    canonical is config.tmux_session_name. Other formats pass through unchanged.
    """
    if not window_key or ":" not in window_key:
        return window_key
    session_part, _, window_part = window_key.rpartition(":")
    if not session_part.startswith(_WEB_MIRROR_SESSION_PREFIX):
        return window_key
    canonical = config.tmux_session_name or "ccgram"
    # Avoid recursive prefix if config itself was auto-detected to a mirror.
    if canonical.startswith(_WEB_MIRROR_SESSION_PREFIX):
        canonical = "ccgram"
    return f"{canonical}:{window_part}"


def _resolve_provider_for_file(window_id: str, file_path: Path):
    """Prefer transcript-path provider hints when a hookful state goes stale."""
    provider = get_provider_for_window(window_id)
    inferred = detect_provider_from_transcript_path(str(file_path))
    current = provider.capabilities.name
    if (
        inferred
        and inferred != current
        and provider.capabilities.supports_hook
        and registry.is_valid(inferred)
    ):
        logger.warning(
            "Provider mismatch for window %s: state=%s transcript=%s; using %s",
            window_id,
            current,
            file_path,
            inferred,
        )
        return registry.get(inferred)
    return provider


@dataclass
class SessionInfo:
    """Information about a Claude Code session."""

    session_id: str
    file_path: Path


@dataclass
class NewMessage:
    """A new message detected by the monitor."""

    session_id: str
    text: str
    is_complete: bool  # True when stop_reason is set (final message)
    content_type: str = "text"  # "text" or "thinking"
    tool_use_id: str | None = None
    role: str = "assistant"  # "user" or "assistant"
    tool_name: str | None = None  # For tool_use messages, the tool name
    window_id: str = ""  # Originating tmux window; used for per-window provider
    # resolution and timeline logging. session_id is now always the ccgram DB
    # routing id, so window_id is NOT needed as a routing fallback.


@dataclass
class NewWindowEvent:
    """A new tmux window detected via session_map changes."""

    window_id: str
    session_id: str
    window_name: str
    cwd: str


class SessionMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Uses simple async polling with aiofiles for non-blocking I/O.
    Emits both intermediate and complete assistant messages.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        self._new_window_callback: (
            Callable[[NewWindowEvent], Awaitable[None]] | None
        ) = None
        # Hook event callback (byte offset persisted in self.state.events_offset)
        from .providers.base import HookEvent

        self._hook_event_callback: Callable[[HookEvent], Awaitable[None]] | None = None
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # session_id -> pending
        # Track last known session_map for detecting changes
        # Keys may be window_id (@12) or window_name (old format) during transition
        self._last_session_map: dict[str, dict[str, str]] = {}  # window_key -> details
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # session_id -> last_seen_mtime
        # Transcript activity timestamps for status heuristic (monotonic time)
        self._last_activity: dict[str, float] = {}  # session_id -> monotonic time
        # Timestamp of the last successful reconcile_session_map pass.
        # Rate-limits drift healing so it doesn't run every poll cycle.
        self._last_reconcile_time: float = 0.0
        self._last_db_reload: float = 0.0
        self._last_periodic_save: float = 0.0

    def get_last_activity(self, session_id: str) -> float | None:
        """Get monotonic timestamp of last transcript activity for a session."""
        return self._last_activity.get(session_id)

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def set_new_window_callback(
        self, callback: Callable[[NewWindowEvent], Awaitable[None]]
    ) -> None:
        self._new_window_callback = callback

    def set_hook_event_callback(self, callback: Callable[..., Awaitable[None]]) -> None:
        self._hook_event_callback = callback

    def record_hook_activity(self, window_id: str) -> None:
        """Record hook-based activity for a window (resets idle timers)."""
        session_id = None
        for sid, details in self._last_session_map.items():
            if sid.endswith(f":{window_id}"):
                session_id = details.get("session_id")
                break
        if session_id:
            self._last_activity[session_id] = time.monotonic()

    async def _read_hook_events(self) -> None:
        """Read new lines from events.jsonl and dispatch via callback."""
        if not self._hook_event_callback:
            return

        events_file = config.events_file
        if not events_file.exists():
            return

        from .providers.base import HookEvent

        offset_before = self.state.events_offset
        try:
            async with aiofiles.open(events_file, "r", encoding="utf-8") as f:
                # Check file size for truncation detection
                await f.seek(0, 2)
                file_size = await f.tell()
                if self.state.events_offset > file_size:
                    self.state.events_offset = 0
                await f.seek(self.state.events_offset)

                async for line in f:
                    line = line.strip()
                    if not line:
                        self.state.events_offset = await f.tell()
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed event line")
                        self.state.events_offset = await f.tell()
                        continue

                    event = HookEvent(
                        event_type=data.get("event", ""),
                        window_key=_canonicalize_window_key(data.get("window_key", "")),
                        session_id=data.get("session_id", ""),
                        data=data.get("data", {}),
                        timestamp=data.get("ts", 0.0),
                    )
                    self.state.events_offset = await f.tell()

                    # Extract window_id and window_name from window_key for timeline
                    _wk = event.window_key
                    _hook_wid = _wk.rsplit(":", 1)[-1] if ":" in _wk else _wk
                    _hook_wname = ""
                    for _details in self._last_session_map.values():
                        if _details.get("session_id") == event.session_id:
                            _hook_wname = _details.get("window_name", "")
                            break
                    await get_timeline().log(
                        "hook",
                        _hook_wid,
                        _hook_wname,
                        {
                            "event_type": event.event_type,
                            "window_key": event.window_key,
                            "session_id": event.session_id,
                            "data": {
                                k: v
                                for k, v in (event.data or {}).items()
                                if k in ("tool_name", "tool_use_id", "exit_code")
                            },
                        },
                    )

                    try:
                        await self._hook_event_callback(event)
                    except _CallbackError:
                        logger.exception(
                            "Hook event callback error for %s", event.event_type
                        )
        except OSError:
            logger.debug("Could not read events file %s", events_file)

        if self.state.events_offset != offset_before:
            self.state._dirty = True

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows."""
        cwds = set()
        windows = await tmux_manager.list_windows()
        for w in windows:
            try:
                cwds.add(str(Path(w.cwd).resolve()))
            except _PathResolveError:
                cwds.add(w.cwd)
        return cwds

    def _scan_projects_sync(self, active_cwds: set[str]) -> list[SessionInfo]:
        """Scan filesystem for session files matching active cwds (sync, for to_thread)."""
        sessions: list[SessionInfo] = []

        if not self.projects_path.exists():
            return sessions

        for project_dir in self.projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            index_file = project_dir / "sessions-index.json"
            original_path = ""
            indexed_ids: set[str] = set()

            if index_file.exists():
                try:
                    index_data = json.loads(index_file.read_text())
                    entries = index_data.get("entries", [])
                    original_path = index_data.get("originalPath", "")

                    for entry in entries:
                        session_id = entry.get("sessionId", "")
                        full_path = entry.get("fullPath", "")
                        project_path = entry.get("projectPath", original_path)

                        if not session_id or not full_path:
                            continue

                        try:
                            norm_pp = str(Path(project_path).resolve())
                        except _PathResolveError:
                            norm_pp = project_path
                        if norm_pp not in active_cwds:
                            continue

                        indexed_ids.add(session_id)
                        file_path = Path(full_path)
                        if file_path.exists():
                            sessions.append(
                                SessionInfo(
                                    session_id=session_id,
                                    file_path=file_path,
                                )
                            )

                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Error reading index %s: %s", index_file, e)

            # Pick up un-indexed .jsonl files
            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    session_id = jsonl_file.stem
                    if session_id in indexed_ids:
                        continue

                    file_project_path = original_path
                    if not file_project_path:
                        file_project_path = read_cwd_from_jsonl(jsonl_file)
                    if not file_project_path:
                        continue

                    try:
                        norm_fp = str(Path(file_project_path).resolve())
                    except _PathResolveError:
                        norm_fp = file_project_path

                    if norm_fp not in active_cwds:
                        continue

                    sessions.append(
                        SessionInfo(
                            session_id=session_id,
                            file_path=jsonl_file,
                        )
                    )
            except OSError as e:
                logger.debug("Error scanning jsonl files in %s: %s", project_dir, e)

        return sessions

    async def scan_projects(self) -> list[SessionInfo]:
        """Scan projects that have active tmux windows.

        Filesystem scanning runs in a thread to avoid blocking the event loop.
        """
        active_cwds = await self._get_active_cwds()
        if not active_cwds:
            return []
        return await asyncio.to_thread(self._scan_projects_sync, active_cwds)

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path, window_id: str = ""
    ) -> list[dict]:
        """Read new lines from a session file using byte offset for efficiency.

        For providers with ``supports_incremental_read=False`` (e.g. Gemini),
        delegates to the provider's ``read_transcript_file()`` method which
        reads the entire JSON file and tracks progress by message count.

        Detects file truncation (e.g. after /clear) and resets offset.
        """
        provider = _resolve_provider_for_file(window_id, file_path)

        # Whole-file providers (Gemini): read entire JSON, track by message count
        if not provider.capabilities.supports_incremental_read:
            return await self._read_whole_file(session, file_path, provider)

        new_entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, jump
                # to current end-of-file. Resetting to 0 here re-shipped the
                # entire transcript when Claude /compact rewrote the JSONL
                # (replay storm 2026-05-04). The user wants forward progress,
                # not a backfill of every historical message.
                if session.last_byte_offset > file_size:
                    logger.warning(
                        "File truncated for session %s "
                        "(offset %d > size %d). Jumping to end-of-file.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = file_size

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Validate offset points to line start (guard against corruption)
                if session.last_byte_offset > 0:
                    first_byte = await f.read(1)
                    if first_byte and first_byte != "{":
                        logger.warning(
                            "Corrupted offset for session %s (byte %d is %r, not '{'). "
                            "Advancing to next line.",
                            session.session_id,
                            session.last_byte_offset,
                            first_byte,
                        )
                        await f.readline()  # consume rest of current (broken) line
                        session.last_byte_offset = await f.tell()
                    else:
                        # Re-seek to include the '{' we just consumed
                        await f.seek(session.last_byte_offset)

                # Read only new lines from the offset.
                # Track safe_offset: only advance past lines that parsed
                # successfully. A non-empty line that fails JSON parsing is
                # likely a partial write; stop and retry next cycle.
                safe_offset = session.last_byte_offset
                async for line in f:
                    data = provider.parse_transcript_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = await f.tell()
                    elif line.strip():
                        # Partial JSONL line — don't advance offset past it
                        log_throttled(
                            logger,
                            f"partial-jsonl:{session.session_id}",
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = await f.tell()

                session.last_byte_offset = safe_offset

        except OSError:
            logger.exception("Error reading session file %s", file_path)
        return new_entries

    async def _read_whole_file(
        self,
        session: TrackedSession,
        file_path: Path,
        provider: Any,
    ) -> list[dict]:
        """Read a whole-file transcript (e.g. Gemini JSON) via the provider.

        Uses ``last_byte_offset`` as a message count tracker (not a byte offset)
        since the entire file is re-read each time.
        """
        try:
            new_entries, new_offset = await asyncio.to_thread(
                provider.read_transcript_file,
                str(file_path),
                session.last_byte_offset,
            )
            session.last_byte_offset = new_offset
            return new_entries
        except OSError:
            logger.exception("Error reading transcript file %s", file_path)
            return []

    async def _process_session_file(
        self,
        session_id: str,
        file_path: Path,
        new_messages: list[NewMessage],
        window_id: str = "",
        ccgram_session_id: str = "",
    ) -> None:
        """Process a single session file for new messages.

        ``session_id`` is the provider-internal UUID used as the file-tracking
        key (TrackedSession key, mtime dict, pending_tools). For Claude sessions
        this equals the ccgram DB session_id. For hookless providers (Codex,
        Gemini) it differs.

        ``ccgram_session_id`` is the ccgram DB session_id used for routing
        (NewMessage.session_id). When empty, falls back to ``session_id``.

        Handles tracking initialization, mtime checking, incremental reading,
        and parsing. Appends any new messages to the provided list.
        """
        # Routing id for NewMessage: prefer ccgram_session_id when available.
        routing_sid = ccgram_session_id or session_id
        tracked = self.state.get_session(session_id)
        provider = _resolve_provider_for_file(window_id, file_path)

        if tracked is None:
            # For new sessions, initialize offset to skip old messages.
            # Incremental providers (JSONL) use byte offset; whole-file
            # providers (Gemini JSON) use message count.
            try:
                st = file_path.stat()
                file_size, current_mtime = st.st_size, st.st_mtime
            except OSError:
                file_size = 0
                current_mtime = 0.0

            if provider.capabilities.supports_incremental_read:
                initial_offset = file_size
                # MonitorState.load() already populates tracked_sessions from
                # user_prefs (canonical) at startup; if we reach here the session
                # is genuinely new. No DB lookup needed -- and _store is undefined
                # at module scope anyway (was a NameError, silently caught).
            else:
                # Whole-file provider: count existing messages to skip them
                _, initial_offset = await asyncio.to_thread(
                    provider.read_transcript_file, str(file_path), 0
                )

            initial_offset = _clamp_backfill_offset(
                file_path, initial_offset, file_size, session_id,
                config.max_initial_backfill_messages, "first_time_tracking",
            )
            tracked = TrackedSession(
                session_id=session_id,
                file_path=str(file_path),
                last_byte_offset=initial_offset,
            )
            self.state.update_session(tracked)
            if initial_offset < file_size:
                # Content exists before our offset — force a read on this pass
                # by setting mtime to 0 so the change-detection below fires.
                self._file_mtimes[session_id] = 0.0
            else:
                self._file_mtimes[session_id] = current_mtime
            if provider.capabilities.name == "claude" and window_id:
                await self._seed_claude_task_state(window_id, session_id, file_path)
            logger.debug("Started tracking session: %s (offset=%d)", session_id, initial_offset)
            if initial_offset >= file_size:
                return
            # Fall through to read existing content immediately

        # Check mtime and size to see if file has changed.
        # Size check catches writes within the same second (mtime granularity).
        # For whole-file providers (Gemini), last_byte_offset is a message count
        # so only mtime is meaningful for change detection.
        try:
            st = file_path.stat()
            current_mtime, current_size = st.st_mtime, st.st_size
        except OSError:
            return

        last_mtime = self._file_mtimes.get(session_id, 0.0)
        if provider.capabilities.supports_incremental_read:
            if current_mtime <= last_mtime and current_size <= tracked.last_byte_offset:
                return
        else:
            # Whole-file provider: only mtime is a valid change signal
            if current_mtime <= last_mtime:
                return

        # File changed, read new content from last offset.
        # Capture offset BEFORE reading so we can tell if this is a fresh-start
        # scan (offset was 0) vs. a catch-up after a gap (offset was non-zero).
        offset_before_read = tracked.last_byte_offset
        new_entries = await self._read_new_lines(tracked, file_path, window_id)
        self._file_mtimes[session_id] = current_mtime

        # Freshness gate: drop entries older than max_message_age_seconds.
        # Defence-in-depth -- even if an offset bug surfaces stale bytes, they
        # never reach _message_queues.
        max_age = config.max_message_age_seconds
        if max_age > 0 and new_entries:
            import time as _time
            from datetime import datetime, timezone
            cutoff_ts = _time.time() - max_age
            fresh: list[dict] = []
            dropped = 0
            for entry in new_entries:
                ts_str = entry.get("timestamp", "")
                if not ts_str:
                    fresh.append(entry)
                    continue
                try:
                    entry_ts = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, TypeError):
                    fresh.append(entry)
                    continue
                if entry_ts >= cutoff_ts:
                    fresh.append(entry)
                else:
                    dropped += 1
            if dropped:
                logger.warning(
                    "Freshness gate dropped %d stale entries for session %s "
                    "(older than %ds); offset advanced past them.",
                    dropped, session_id, max_age,
                )
            new_entries = fresh

        # Record transcript activity for status heuristic
        if new_entries:
            self._last_activity[session_id] = time.monotonic()

        # Parse new entries using the shared logic, carrying over pending tools
        if provider.capabilities.name == "claude" and window_id:
            claude_task_state.apply_entries(window_id, session_id, new_entries)

        carry = self._pending_tools.get(session_id, {})
        # Get cwd from session_map for path shortening in tool summaries
        session_cwd: str | None = None
        for _wkey, details in self._last_session_map.items():
            if details.get("session_id") == session_id:
                session_cwd = details.get("cwd")
                break

        agent_messages, remaining = provider.parse_transcript_entries(
            new_entries,
            pending_tools=carry,
            cwd=session_cwd,
        )
        if remaining:
            self._pending_tools[session_id] = remaining
        else:
            self._pending_tools.pop(session_id, None)

        # Resolve window_name for timeline logging
        _window_name = ""
        if window_id:
            for _wkey, _details in self._last_session_map.items():
                if _details.get("session_id") == session_id:
                    _window_name = _details.get("window_name", "")
                    break

        _tl = get_timeline()

        # Catch-up cap: when resuming after a gap, avoid flooding with stale msgs.
        with_text = [e for e in agent_messages if e.text]
        assistant_text_entries = [
            e for e in with_text if e.role == 'assistant' and e.content_type == 'text'
        ]

        notice: NewMessage | None = None
        if offset_before_read != 0 and len(assistant_text_entries) > _CATCHUP_CAP:
            skipped = len(assistant_text_entries) - _CATCHUP_CAP
            _now = time.monotonic()
            _last = _catchup_notice_last.get(session_id, 0.0)
            if _now - _last >= _CATCHUP_DEBOUNCE_SECS:
                _catchup_notice_last[session_id] = _now
                _last_active = self._last_activity.get(session_id)
                if _last_active is not None:
                    _mins = max(1, int((_now - _last_active) / 60))
                    _elapsed = f'{_mins}m'
                else:
                    _elapsed = 'a gap'
                notice = NewMessage(
                    session_id=routing_sid,
                    text=(
                        f'🔄 Caught up after {_elapsed} — skipped {skipped} earlier'
                        f' messages, showing last {_CATCHUP_CAP}:'
                    ),
                    is_complete=True,
                    content_type='text',
                    role='assistant',
                )
            # Trim: keep all non-assistant-text entries + last _CATCHUP_CAP assistant-text.
            keep_ids = {id(e) for e in assistant_text_entries[-_CATCHUP_CAP:]}
            with_text = [
                e
                for e in with_text
                if e.content_type != 'text' or e.role != 'assistant' or id(e) in keep_ids
            ]

        if notice is not None:
            notice.window_id = window_id
            new_messages.append(notice)

        for entry in with_text:
            new_messages.append(
                NewMessage(
                    session_id=routing_sid,
                    text=entry.text,
                    is_complete=True,
                    content_type=entry.content_type,
                    tool_use_id=entry.tool_use_id,
                    role=entry.role,
                    tool_name=entry.tool_name,
                    window_id=window_id,
                )
            )
            # Log transcript event to debug timeline
            await _tl.log(
                "transcript",
                window_id,
                _window_name,
                {
                    "session_id": session_id,
                    "role": entry.role,
                    "content_type": entry.content_type,
                    "text_preview": entry.text[:200],
                    "tool_name": entry.tool_name,
                    "is_complete": True,
                },
            )

        self.state.update_session(tracked)

    async def _seed_claude_task_state(
        self, window_id: str, session_id: str, file_path: Path
    ) -> None:
        """Build a Claude task snapshot from the full transcript once per session."""
        entries: list[dict[str, Any]] = []
        provider = registry.get("claude")
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    data = provider.parse_transcript_line(line)
                    if data:
                        entries.append(data)
        except OSError:
            logger.exception("Error seeding Claude task state from %s", file_path)
            return

        claude_task_state.rebuild_from_entries(window_id, session_id, entries)

    async def check_for_updates(
        self, current_map: dict[str, dict[str, str]]
    ) -> list[NewMessage]:
        """Check all sessions for new assistant messages.

        Reads from last byte offset. Emits both intermediate
        (stop_reason=null) and complete messages.

        Uses two paths:
        1. Primary: entries with transcript_path are read directly (no scanning).
        2. Fallback: entries without transcript_path use scan_projects() + session_id match.

        Args:
            current_map: Window key -> details from session_map
        """
        new_messages: list[NewMessage] = []

        # Build reverse maps for routing and file tracking.
        # provider_session_id is the key used for file/transcript tracking.
        # session_id (ccgram DB id) is the key used for routing (NewMessage).
        psid_to_wid: dict[str, str] = {}   # provider_session_id -> window_id
        psid_to_sid: dict[str, str] = {}   # provider_session_id -> ccgram session_id
        for window_id, details in current_map.items():
            psid = details.get("provider_session_id") or details["session_id"]
            psid_to_wid[psid] = window_id
            psid_to_sid[psid] = details["session_id"]

        # Separate entries with direct transcript_path from those needing scan
        direct_sessions: list[tuple[str, str, Path]] = []  # (provider_sid, ccgram_sid, path)
        fallback_provider_ids: set[str] = set()

        for details in current_map.values():
            psid = details.get("provider_session_id") or details["session_id"]
            ccgram_sid = details["session_id"]
            transcript_path = details.get("transcript_path", "")
            if transcript_path:
                path = Path(transcript_path)
                if path.exists():
                    direct_sessions.append((psid, ccgram_sid, path))
                    continue
            fallback_provider_ids.add(psid)

        # Primary path: read directly from transcript_path
        # Use provider_session_id as the file-tracking key; pass ccgram_session_id
        # so NewMessage carries the routing id.
        for provider_sid, ccgram_sid, file_path in direct_sessions:
            try:
                await self._process_session_file(
                    provider_sid,
                    file_path,
                    new_messages,
                    window_id=psid_to_wid.get(provider_sid, ""),
                    ccgram_session_id=ccgram_sid,
                )
            except Exception:
                logger.exception("Error processing session %s", provider_sid)

        # Fallback path: scan projects for sessions without transcript_path
        if fallback_provider_ids:
            sessions = await self.scan_projects()
            for session_info in sessions:
                if session_info.session_id not in fallback_provider_ids:
                    continue
                try:
                    await self._process_session_file(
                        session_info.session_id,
                        session_info.file_path,
                        new_messages,
                        window_id=psid_to_wid.get(session_info.session_id, ""),
                        ccgram_session_id=psid_to_sid.get(
                            session_info.session_id, session_info.session_id
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Error processing session %s", session_info.session_id
                    )

        self.state.save_if_dirty()
        return new_messages

    async def _load_current_session_map(self) -> dict[str, dict[str, str]]:
        """Load current session_map and return window_key -> details mapping.

        Keys in session_map are formatted as "tmux_session:window_id"
        (e.g. "ccgram:@12"). Old-format keys ("ccgram:window_name") are also
        accepted so that sessions running before a code upgrade continue
        to be monitored until the hook re-fires with new format.
        Only entries matching our tmux_session_name are processed.

        Returns {window_key: {"session_id": ..., "cwd": ..., "window_name": ...}}.
        """
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                raw = json.loads(content)
                prefix = f"{config.tmux_session_name}:"
                return parse_session_map(raw, prefix)
            except _SessionMapError:
                pass
        return {}

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up all tracked sessions not in current session_map (used on startup)."""
        current_map = await self._load_current_session_map()
        active_session_ids = {v["session_id"] for v in current_map.values()}

        # If session_map is empty (wrong tmux session name, file not yet
        # populated, etc.), don't wipe everything — that's destructive and
        # wrong.  The monitor will pick up sessions as hooks fire.
        if not active_session_ids and self.state.tracked_sessions:
            logger.warning(
                "[Startup cleanup] session_map is empty but %d sessions tracked "
                "— skipping cleanup (likely wrong tmux session prefix or cold start)",
                len(self.state.tracked_sessions),
            )
            return

        # Build set of session_ids belonging to system windows (never clean these up)
        system_session_ids: set[str] = set()
        for details in current_map.values():
            wname = details.get("window_name", "")
            if config.is_system_window(wname):
                system_session_ids.add(details["session_id"])

        stale_sessions = []
        for session_id in self.state.tracked_sessions:
            if session_id not in active_session_ids and session_id not in system_session_ids:
                stale_sessions.append(session_id)

        if stale_sessions:
            logger.info(
                "[Startup cleanup] Removing %d stale sessions", len(stale_sessions)
            )
            for session_id in stale_sessions:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._pending_tools.pop(session_id, None)
                self._last_activity.pop(session_id, None)
                log_throttle_reset(f"partial-jsonl:{session_id}")
            self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, dict[str, str]]:
        """Detect session_map changes, cleanup replaced/removed sessions, fire new window events.

        Returns current session_map for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()

        # Check for window session changes (window exists in both, but session_id changed)
        for window_id, old_details in self._last_session_map.items():
            new_details = current_map.get(window_id)
            if new_details and new_details["session_id"] != old_details["session_id"]:
                logger.info(
                    "Window '%s' session changed: %s -> %s",
                    window_id,
                    old_details["session_id"],
                    new_details["session_id"],
                )
                sessions_to_remove.add(old_details["session_id"])
                claude_task_state.clear_window(window_id)

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_session_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        for window_id in deleted_windows:
            old_sid = self._last_session_map[window_id]["session_id"]
            logger.info(
                "Window '%s' deleted, removing session %s",
                window_id,
                old_sid,
            )
            sessions_to_remove.add(old_sid)
            claude_task_state.clear_window(window_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._pending_tools.pop(session_id, None)
                self._last_activity.pop(session_id, None)
                log_throttle_reset(f"partial-jsonl:{session_id}")
            self.state.save_if_dirty()

        # Detect new windows: set provider from session_map if available, then fire callback
        new_windows = current_windows - old_windows
        if new_windows:
            from .session import session_manager as _sm

            for window_id in new_windows:
                details = current_map[window_id]
                provider_name = details.get("provider_name", "")
                if provider_name:
                    _sm.set_window_provider(window_id, provider_name)

                if self._new_window_callback:
                    event = NewWindowEvent(
                        window_id=window_id,
                        session_id=details["session_id"],
                        window_name=details.get("window_name", ""),
                        cwd=details.get("cwd", ""),
                    )
                    try:
                        await self._new_window_callback(event)
                    except _CallbackError:
                        logger.exception("New window callback error for %s", window_id)

        # Update last known map
        self._last_session_map = current_map

        return current_map

    async def reconcile_session_map(self) -> None:
        """Heal drift: ensure every thread-bound window has a session_map entry.

        Walks ``thread_router.iter_thread_bindings()`` and for each bound
        window_id checks that ``session_map.json`` has a matching entry.
        For any missing entries, tries to reconstruct the entry from the
        live tmux window (cwd, provider) plus the most recently modified
        Claude jsonl file in the window's project directory.

        This closes the failure mode where ``prune_session_map`` removes an
        entry due to a transient tmux ``list-windows`` miss (e.g. during a
        restart or ssh reconnect hiccup) but the window is still alive and
        the user expects it to mirror to Telegram. Without reconciliation,
        the entry stays missing until a fresh SessionStart hook fires,
        which only happens on ``claude`` startup — not on every turn.

        Conservative: only reconstructs when there's strong evidence:
          - tmux window is live
          - window cwd maps to an existing Claude projects directory
          - that directory contains a jsonl modified within the last hour
          - the jsonl's first line parses as a valid Claude entry
        Otherwise leaves the entry missing so SessionStart can populate it.

        Rate-limited to ``_RECONCILE_INTERVAL_SECS`` so this isn't a hot
        loop hitting the filesystem every 2 seconds.
        """
        import time as _time

        now = _time.monotonic()
        if now - self._last_reconcile_time < _RECONCILE_INTERVAL_SECS:
            return
        self._last_reconcile_time = now

        try:
            from .thread_router import thread_router
        except ImportError:
            return

        # Collect bare-id bindings (@N form, not qualified web-*:@N)
        bound_ids: set[str] = set()
        for _uid, _tid, wid in thread_router.iter_thread_bindings():
            if wid and wid.startswith("@") and ":" not in wid:
                bound_ids.add(wid)
        if not bound_ids:
            return

        # Load session_map
        if not config.session_map_file.exists():
            return
        try:
            sm = json.loads(config.session_map_file.read_text())
        except (json.JSONDecodeError, OSError):
            return

        canonical = config.tmux_session_name or "ccgram"
        if canonical.startswith("web-"):
            canonical = "ccgram"
        prefix = f"{canonical}:"
        # Find missing entries AND stale entries (transcript file gone)
        missing: list[str] = []
        for wid in bound_ids:
            key = f"{prefix}{wid}"
            if key not in sm:
                missing.append(wid)
            elif not Path(sm[key].get("transcript_path", "")).exists():
                del sm[key]  # stale — transcript gone, treat as missing
                missing.append(wid)
        if not missing:
            return

        # Pull live tmux windows once for lookup
        try:
            live_windows = await tmux_manager.list_windows()
        except _PathResolveError:
            return
        win_by_id = {w.window_id: w for w in live_windows}

        now_wall = _time.time()
        healed: list[tuple[str, str, str]] = []  # (wid, sid, file)

        for wid in missing:
            w = win_by_id.get(wid)
            if w is None:
                # Stale binding — window doesn't exist. Leave alone; other
                # cleanup paths handle zombie bindings.
                continue
            # Only reconcile Claude (hook-based) windows — hookless providers
            # (codex, gemini) have their own transcript discovery.
            from .session import session_manager as _sm2

            ws = _sm2.window_states.get(wid)
            if ws and ws.provider_name and ws.provider_name != "claude":
                continue
            cwd = w.cwd or ""
            if not cwd:
                continue

            # Compute project dir from cwd (Claude convention)
            project_slug = "-" + cwd.lstrip("/").replace("/", "-").replace("_", "-")
            project_dir = config.claude_projects_path / project_slug
            if not project_dir.is_dir():
                continue

            # Find the most recent jsonl in the project dir
            try:
                jsonls = sorted(
                    project_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            if not jsonls:
                continue

            chosen: Path | None = None
            chosen_sid: str = ""
            for j in jsonls[:5]:
                try:
                    mtime = j.stat().st_mtime
                except OSError:
                    continue
                if now_wall - mtime > _RECONCILE_MAX_JSONL_AGE_SECS:
                    # Sorted newest-first — everything after this is also old
                    break
                # Sanity check: first non-empty line must parse as JSON and
                # look like a Claude transcript entry
                try:
                    with open(j, encoding="utf-8") as fh:
                        first = ""
                        for raw in fh:
                            if raw.strip():
                                first = raw
                                break
                    if not first:
                        continue
                    d = json.loads(first)
                except (OSError, json.JSONDecodeError):
                    continue
                # Sanity check: the first line must be a recognisable Claude
                # transcript entry. Claude Code writes various metadata types
                # as the first line: "permission-mode", "summary", "user",
                # "assistant", "system", etc. Accept anything that has a
                # "type" key (proof it's structured Claude output, not a
                # random file). Reject files where the first line doesn't
                # parse or lacks a type field.
                if not d.get("type"):
                    continue
                chosen = j
                # Claude uses filename = session_id convention
                chosen_sid = j.stem
                break

            if chosen is None or not chosen_sid:
                continue

            initial_offset = _find_last_user_turn_offset(chosen)
            try:
                chosen_size = chosen.stat().st_size
            except OSError:
                chosen_size = initial_offset  # can't stat; clamp helper will no-op
            initial_offset = _clamp_backfill_offset(
                chosen, initial_offset, chosen_size, chosen_sid,
                config.max_initial_backfill_messages, "reconcile_session_map",
            )

            sm[f"{prefix}{wid}"] = {
                "session_id": chosen_sid,
                "cwd": cwd,
                "window_name": w.window_name or "",
                "transcript_path": str(chosen),
                "provider_name": "claude",
            }

            # Pre-populate tracked_sessions so the first poll cycle after
            # reconciliation actually backfills from initial_offset rather
            # than the default "jump to end of file" for fresh sessions.
            # CRITICAL: if we're already tracking this session at an offset
            # past initial_offset, DO NOT rewind — reconcile runs on every
            # tick and would otherwise replay the backlog forever.
            existing = self.state.tracked_sessions.get(chosen_sid)
            if existing and existing.last_byte_offset >= initial_offset:
                continue
            tracked = TrackedSession(
                session_id=chosen_sid,
                file_path=str(chosen),
                last_byte_offset=initial_offset,
            )
            self.state.update_session(tracked)

            healed.append((wid, chosen_sid, chosen.name))

        if healed:
            try:
                from .utils import atomic_write_json

                _log_mutation("session_map_write", window=str([w for w, _, _ in healed]), session=str([s for _, s, _ in healed]), details=f"reconcile_heal count={len(healed)}")
                atomic_write_json(config.session_map_file, sm)
            except OSError:
                logger.exception("Failed to persist reconciled session_map")
                return
            self.state.save_if_dirty()
            for wid, sid, fname in healed:
                logger.info(
                    "Reconciled session_map: %s -> %s (transcript=%s)",
                    wid,
                    sid,
                    fname,
                )

    async def _reload_topic_bindings_from_db(self) -> None:
        from .thread_router import thread_router as _tr
        reload_secs = float(os.environ.get("CCGRAM_DB_RELOAD_SECS", "60"))
        now = time.monotonic()
        if now - self._last_db_reload < reload_secs:
            return
        self._last_db_reload = now
        from . import session_repo as _session_repo
        counts = _session_repo.hydrate_in_memory(thread_router=_tr)
        logger.debug(
            "runtime reload: hydrate_in_memory complete",
            sessions=counts["sessions"],
            bindings=counts["bindings"],
            router_bindings=counts["updated_router_bindings"],
        )

    async def _monitor_loop(self) -> None:
        """Background loop for checking session updates.

        Uses simple async polling with aiofiles for non-blocking I/O.
        """
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        # Clean up all stale sessions on startup
        await self._cleanup_all_stale_sessions()
        # Initialize last known session_map
        self._last_session_map = await self._load_current_session_map()

        error_streak = 0
        while self._running:
            try:
                # Periodically reload topic bindings from DB (rebind sync)
                await self._reload_topic_bindings_from_db()
                # Read hook events first (lower latency than transcript polls)
                await self._read_hook_events()

                # Load hook-based session map updates
                await session_manager.load_session_map()

                # Heal drift between thread_bindings and session_map.
                # This runs at most every _RECONCILE_INTERVAL_SECS seconds;
                # intermediate calls are cheap no-ops. Placed BEFORE the
                # cleanup pass so reconciled entries don't get pruned in
                # the same iteration that inserts them.
                try:
                    await self.reconcile_session_map()
                except Exception:
                    logger.exception("reconcile_session_map failed")

                # Detect session_map changes and cleanup replaced/removed sessions
                current_map = await self._detect_and_cleanup_changes()

                # Detect unbound tmux windows (no Claude Code yet)
                all_windows = await tmux_manager.list_windows()
                external_windows = await tmux_manager.discover_external_sessions()
                # Defensive: filter out web-terminal grouped mirror windows.
                # discover_external_sessions has its own skip logic but it can
                # fail when CCGram auto-detected its own session as a mirror.
                external_windows = [
                    w
                    for w in external_windows
                    if not (
                        ":" in w.window_id
                        and w.window_id.split(":", 1)[0].startswith("web-")
                    )
                ]
                all_windows = all_windows + external_windows
                live_window_ids = {w.window_id for w in all_windows}
                session_manager.prune_session_map(live_window_ids)
                known_window_ids = set(current_map.keys())
                for window in all_windows:
                    if window.window_id in known_window_ids:
                        continue
                    from .thread_router import thread_router

                    already_bound = any(
                        wid == window.window_id
                        for _, _, wid in thread_router.iter_thread_bindings()
                    )
                    if not already_bound and self._new_window_callback:
                        event = NewWindowEvent(
                            window_id=window.window_id,
                            session_id="",
                            window_name=window.window_name,
                            cwd=window.cwd,
                        )
                        try:
                            await self._new_window_callback(event)
                        except _CallbackError:
                            logger.exception(
                                "New window callback error for %s",
                                window.window_id,
                            )

                # Check for new messages (all I/O is async)
                new_messages = await self.check_for_updates(current_map)

                for msg in new_messages:
                    structlog.contextvars.clear_contextvars()
                    structlog.contextvars.bind_contextvars(session_id=msg.session_id)
                    status = "complete" if msg.is_complete else "streaming"
                    preview = msg.text[:_MSG_PREVIEW_LENGTH] + (
                        "..." if len(msg.text) > _MSG_PREVIEW_LENGTH else ""
                    )
                    logger.debug("[%s] session=%s: %s", status, msg.session_id, preview)
                    if self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except _CallbackError:
                            logger.exception(
                                "Message callback error for session=%s",
                                msg.session_id,
                            )

            except _LoopError:
                logger.exception("Monitor loop error")
                backoff_delay = min(_BACKOFF_MAX, _BACKOFF_MIN * (2**error_streak))
                error_streak += 1
                await asyncio.sleep(backoff_delay)
                continue
            except Exception:
                # Catch-all: programming errors must not kill the monitor loop.
                logger.exception("Unexpected error in monitor loop")
                backoff_delay = min(_BACKOFF_MAX, _BACKOFF_MIN * (2**error_streak))
                error_streak += 1
                await asyncio.sleep(backoff_delay)
                continue

            error_streak = 0
            now = time.monotonic()
            if now - self._last_periodic_save >= _PERIODIC_SAVE_INTERVAL:
                self.state.save()
                self._last_periodic_save = now
            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.debug("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        self._task.add_done_callback(task_done_callback)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")


# Module-level holder for the active monitor instance.
# Set once by bot.py post_init before any polling starts.
_active_monitor: SessionMonitor | None = None


def set_active_monitor(monitor: SessionMonitor) -> None:
    """Set the active SessionMonitor instance (called by bot.py post_init)."""
    global _active_monitor  # noqa: PLW0603
    _active_monitor = monitor


def get_active_monitor() -> SessionMonitor | None:
    """Return the active SessionMonitor instance."""
    return _active_monitor
