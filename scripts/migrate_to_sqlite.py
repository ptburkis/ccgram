"""Migrate CCGram JSON state files to SQLite.

Reads all ``~/.ccgram/*.json`` state files and populates ``state.db`` using
the CRUD helpers in ``ccgram.store``.  The script is idempotent — running it
twice produces identical DB state.  Source JSON files are never modified.

Usage::

    python scripts/migrate_to_sqlite.py [--source DIR] [--db PATH] [--dry-run] [-v]

Missing source files are skipped with a log line.  Duplicate session bindings
(two thread_binding entries pointing to the same window) trigger a loud warning
and only the first is written.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

# Allow importing ccgram modules when running from repo root.
_REPO_SRC = Path(__file__).parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ccgram import store  # noqa: E402
from ccgram.utils import ccgram_dir  # noqa: E402

logger = logging.getLogger("migrate_to_sqlite")


# ---- Helpers -----------------------------------------------------------------


def _load_json(path: Path, label: str) -> Any:
    """Load JSON from *path*; return ``None`` if missing or malformed."""
    if not path.exists():
        logger.info("Skipping missing file: %s", label)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", label, exc)
        return None


def _iso_to_epoch(value: str | None) -> int | None:
    """Parse an ISO-8601 string to integer epoch seconds, or return ``None``."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


# ---- Per-table migration functions -------------------------------------------


def _migrate_sessions(  # noqa: C901
    conn,
    session_map: dict | None,
    window_states: dict | None,
    verbose: bool,
) -> dict[str, int]:
    """Populate ``sessions`` from session_map.json and state.json window_states."""
    counts = {"inserted": 0, "skipped": 0}
    seen: set[str] = set()

    def _upsert(session_id: str, cwd: str, agent: str, window_id: str | None) -> None:
        if not session_id or not cwd:
            return
        status = "active" if window_id else "retired"
        store.upsert_session(
            conn,
            session_id=session_id,
            cwd=cwd,
            agent=agent,
            status=status,
            window_id=window_id,
            created_at=0,
        )
        if session_id not in seen:
            counts["inserted"] += 1
            seen.add(session_id)
            if verbose:
                logger.debug("  session %s (%s)", session_id[:8], cwd)
        else:
            counts["skipped"] += 1

    # session_map.json: keys are "ccgram:@N"
    if session_map:
        for qualified_key, entry in session_map.items():
            parts = qualified_key.split(":", 1)
            window_id = parts[1] if len(parts) == 2 else None  # noqa: PLR2004
            session_id = entry.get("session_id", "")
            cwd = entry.get("cwd", "")
            agent = entry.get("provider_name", "claude")
            _upsert(session_id, cwd, agent, window_id)

    # window_states from state.json — may contain entries not in session_map
    if window_states:
        for window_id, ws in window_states.items():
            if not isinstance(ws, dict):
                continue
            session_id = ws.get("session_id", "")
            cwd = ws.get("cwd", "")
            agent = ws.get("provider_name", "claude")
            if session_id and session_id not in seen:
                _upsert(session_id, cwd, agent, window_id)

    return counts


def _migrate_topic_bindings(  # noqa: C901
    conn,
    thread_bindings: dict | None,
    window_display_names: dict | None,
    group_chat_ids: dict | None,
    verbose: bool,
) -> dict[str, int]:
    """Populate ``topic_bindings`` from state.json thread_bindings.

    The outer key in thread_bindings is a Telegram user_id, *not* the group
    chat_id.  The real group_id (negative chat_id) lives in group_chat_ids
    under the composite key ``"{user_id}:{topic_id}"``.
    """
    counts = {"inserted": 0, "skipped_no_session": 0, "skipped_duplicate": 0}
    if not thread_bindings:
        return counts

    display_names = window_display_names or {}
    chat_ids = group_chat_ids or {}
    claimed: dict[str, tuple[int, int]] = {}

    for group_id_str, topics in thread_bindings.items():
        if not isinstance(topics, dict):
            continue
        try:
            user_id = int(group_id_str)
        except (ValueError, TypeError):
            logger.warning("Skipping invalid user_id key: %r", group_id_str)
            continue

        for topic_id_str, window_id in topics.items():
            try:
                topic_id = int(topic_id_str)
            except (ValueError, TypeError):
                logger.warning("Skipping invalid topic_id: %r", topic_id_str)
                continue

            # Resolve real group_id (negative chat_id) from group_chat_ids map.
            composite_key = f"{user_id}:{topic_id}"
            raw_chat_id = chat_ids.get(composite_key)
            if raw_chat_id is not None and isinstance(raw_chat_id, int):
                group_id = raw_chat_id
            else:
                logger.warning(
                    "group_chat_ids missing key %r — falling back to user_id %s",
                    composite_key, user_id,
                )
                group_id = user_id

            session = store.get_session_by_window(conn, window_id)
            if session is None:
                logger.warning(
                    "No session for window %s (group=%s topic=%s) — skipping binding",
                    window_id, group_id, topic_id,
                )
                counts["skipped_no_session"] += 1
                continue

            session_id = session.session_id
            if session_id in claimed:
                prev_group, prev_topic = claimed[session_id]
                logger.warning(
                    "DUPLICATE BINDING: session %s already bound to "
                    "(group=%s, topic=%s); ignoring (group=%s, topic=%s)",
                    session_id[:8], prev_group, prev_topic, group_id, topic_id,
                )
                counts["skipped_duplicate"] += 1
                continue

            title = display_names.get(window_id, window_id)
            try:
                store.upsert_topic_binding(
                    conn,
                    group_id=group_id,
                    topic_id=topic_id,
                    session_id=session_id,
                    topic_title=title,
                    bound_at=0,
                )
                claimed[session_id] = (group_id, topic_id)
                counts["inserted"] += 1
                if verbose:
                    logger.debug(
                        "  binding (g=%s, t=%s) -> session %s",
                        group_id, topic_id, session_id[:8],
                    )
            except sqlite3.IntegrityError as exc:
                logger.warning(
                    "Failed to bind (group=%s, topic=%s): %s", group_id, topic_id, exc
                )
                counts["skipped_duplicate"] += 1

    return counts


def _resolve_cron_session(
    conn,
    window_name: str,
    name_to_wid: dict[str, str],
    cron_name: str,
) -> tuple[str | None, int | None, int | None]:
    """Return (target_session_id, target_topic_id, target_group_id) for a cron.

    Best-effort: looks up the session matching ``window_name`` via the display
    names map.  Logs a WARNING and returns all-None if no match.
    """
    if not window_name:
        return None, None, None
    wid = name_to_wid.get(window_name)
    if wid:
        session = store.get_session_by_window(conn, wid)
        if session:
            binding = store.get_binding_for_session(conn, session.session_id)
            t_id = binding.topic_id if binding else None
            g_id = binding.group_id if binding else None
            return session.session_id, t_id, g_id
    logger.warning(
        "cron %r: no session found for target_window=%r "
        "-- cron will use legacy window targeting",
        cron_name,
        window_name,
    )
    return None, None, None


def _migrate_crons(
    conn,
    crons_data: list | None,
    window_display_names: dict | None,
    verbose: bool,
) -> dict[str, int]:
    """Populate ``crons`` from crons.json.

    Best-effort: populates ``target_session_id``/``target_topic_id``/
    ``target_group_id`` by matching ``window_name`` against the display-names
    map + sessions table.  Logs a WARNING for any cron that cannot be resolved
    to a session (it will fall back to legacy window targeting at fire time).
    """
    counts = {"inserted": 0}
    if not crons_data:
        return counts

    # Build reverse map: display_name -> window_id (e.g. "@3")
    name_to_wid: dict[str, str] = {}
    if window_display_names:
        for wid, name in window_display_names.items():
            if isinstance(name, str):
                name_to_wid[name] = wid

    for entry in crons_data:
        if not isinstance(entry, dict):
            continue
        cron_id = entry.get("id")
        if cron_id is None:
            continue
        created_at = _iso_to_epoch(entry.get("created")) or 0
        last_run = _iso_to_epoch(entry.get("last_run"))
        tw = entry.get("window_name", "")
        target_session_id, target_topic_id, target_group_id = _resolve_cron_session(
            conn, tw, name_to_wid, entry.get("name", "?")
        )

        store.upsert_cron(
            conn,
            id=int(cron_id),
            name=entry.get("name", ""),
            schedule=entry.get("schedule", ""),
            target_window=tw,
            message=entry.get("message", ""),
            enabled=bool(entry.get("enabled", False)),
            created_at=created_at,
            last_run=last_run,
            target_session_id=target_session_id,
            target_topic_id=target_topic_id,
            target_group_id=target_group_id,
        )
        counts["inserted"] += 1
        if verbose:
            logger.debug("  cron %s: %s", cron_id, entry.get("name"))
    return counts


def _migrate_heartbeats(conn, health_state: dict | None, verbose: bool) -> dict[str, int]:
    """Populate ``heartbeats`` from health-state.json."""
    counts = {"inserted": 0}
    if not health_state:
        return counts
    for window_name, entry in health_state.items():
        if not isinstance(entry, dict):
            continue
        component = f"health:{window_name}"
        last_beat = _iso_to_epoch(entry.get("last_alerted")) or 0
        details = {"status": entry.get("status"), "wid": entry.get("wid")}
        store.record_heartbeat(conn, component, last_beat=last_beat, details=details)
        counts["inserted"] += 1
        if verbose:
            logger.debug("  heartbeat %s", component)
    return counts


def _migrate_user_prefs(  # noqa: C901, PLR0912
    conn,
    state_data: dict | None,
    bg_work_shown: dict | None,
    effort_shown: dict | None,
    monitor_state: dict | None,
    target_state: dict | None,
    verbose: bool,
) -> dict[str, int]:
    """Populate ``user_prefs`` from multiple sources."""
    counts = {"inserted": 0}

    def _set(scope: str, scope_id: str, key: str, value: Any) -> None:
        store.set_pref(conn, scope, key, value, scope_id=scope_id)
        counts["inserted"] += 1
        if verbose:
            logger.debug("  pref %s/%s/%s", scope, scope_id, key)

    if state_data:
        thread_bindings = state_data.get("thread_bindings", {})
        user_window_offsets = state_data.get("user_window_offsets", {})
        user_dir_favorites = state_data.get("user_dir_favorites", {})
        window_display_names = state_data.get("window_display_names", {})
        group_chat_ids = state_data.get("group_chat_ids", {})

        all_group_ids: set[str] = set()
        if isinstance(thread_bindings, dict):
            all_group_ids.update(thread_bindings.keys())
        if isinstance(user_window_offsets, dict):
            all_group_ids.update(user_window_offsets.keys())
        if isinstance(user_dir_favorites, dict):
            all_group_ids.update(user_dir_favorites.keys())

        for gid in all_group_ids:
            scope_id = str(gid)
            if isinstance(thread_bindings, dict) and gid in thread_bindings:
                _set("group", scope_id, "thread_bindings", thread_bindings[gid])
            if isinstance(user_window_offsets, dict) and gid in user_window_offsets:
                _set("group", scope_id, "user_window_offsets", user_window_offsets[gid])
            if isinstance(user_dir_favorites, dict) and gid in user_dir_favorites:
                _set("group", scope_id, "user_dir_favorites", user_dir_favorites[gid])

        if isinstance(group_chat_ids, dict):
            for composite_key, chat_id in group_chat_ids.items():
                _set("group_chat", "", composite_key, chat_id)

        if isinstance(window_display_names, dict):
            for win_id, name in window_display_names.items():
                _set("window", win_id, "display_name", name)

    if bg_work_shown and isinstance(bg_work_shown, dict):
        for win_id, val in bg_work_shown.items():
            _set("window", win_id, "bg_work_shown", val)

    if effort_shown and isinstance(effort_shown, dict):
        for win_id, val in effort_shown.items():
            _set("window", win_id, "effort_shown", val)

    if monitor_state and isinstance(monitor_state, dict):
        tracked = monitor_state.get("tracked_sessions", {})
        if isinstance(tracked, dict):
            for session_id, entry in tracked.items():
                if not isinstance(entry, dict):
                    continue
                last_byte_offset = entry.get("last_byte_offset")
                file_path = entry.get("file_path")
                if last_byte_offset is not None:
                    _set("monitor", session_id, "last_byte_offset", last_byte_offset)
                if file_path:
                    _set("monitor", session_id, "file_path", file_path)

    if target_state is not None:
        _set("target_state", "", "snapshot", target_state)

    return counts


# ---- Core migrate function ---------------------------------------------------


def migrate(source: Path, db: Path, dry_run: bool, verbose: bool) -> None:
    """Run the full migration from JSON files in *source* to the SQLite DB at *db*.

    Args:
        source: Directory containing the JSON state files.
        db: Path to the SQLite database file.
        dry_run: If ``True``, roll back all changes at the end.
        verbose: If ``True``, emit per-row debug log lines.
    """
    store.init_db(db)

    session_map = _load_json(source / "session_map.json", "session_map.json")
    state_raw = _load_json(source / "state.json", "state.json")
    crons_data = _load_json(source / "crons.json", "crons.json")
    health_state = _load_json(source / "health-state.json", "health-state.json")
    monitor_state = _load_json(source / "monitor_state.json", "monitor_state.json")
    bg_work_shown = _load_json(source / "bg_work_shown.json", "bg_work_shown.json")
    effort_shown = _load_json(source / "effort_shown.json", "effort_shown.json")
    target_state = _load_json(source / "target-state.json", "target-state.json")

    state_data: dict | None = state_raw if isinstance(state_raw, dict) else None
    window_states = state_data.get("window_states") if state_data else None
    thread_bindings = state_data.get("thread_bindings") if state_data else None
    window_display_names = state_data.get("window_display_names") if state_data else None

    import sqlite3 as _sqlite3

    raw_conn = _sqlite3.connect(str(db))
    raw_conn.row_factory = _sqlite3.Row
    raw_conn.execute("PRAGMA journal_mode=WAL")
    raw_conn.execute("PRAGMA foreign_keys=ON")
    raw_conn.execute("PRAGMA synchronous=NORMAL")

    try:
        s_counts = _migrate_sessions(raw_conn, session_map, window_states, verbose)
        b_counts = _migrate_topic_bindings(
            raw_conn,
            thread_bindings,
            window_display_names,
            state_data.get("group_chat_ids") if state_data else None,
            verbose,
        )
        c_counts = _migrate_crons(raw_conn, crons_data, window_display_names, verbose)
        h_counts = _migrate_heartbeats(raw_conn, health_state, verbose)
        p_counts = _migrate_user_prefs(
            raw_conn,
            state_data,
            bg_work_shown if isinstance(bg_work_shown, dict) else None,
            effort_shown if isinstance(effort_shown, dict) else None,
            monitor_state if isinstance(monitor_state, dict) else None,
            target_state,
            verbose,
        )

        if dry_run:
            raw_conn.rollback()
            logger.info("DRY RUN -- all changes rolled back")
        else:
            raw_conn.commit()

    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()

    logger.info(
        "sessions: %d inserted, %d skipped",
        s_counts["inserted"], s_counts["skipped"],
    )
    logger.info(
        "topic_bindings: %d inserted, %d skipped (no session), %d skipped (duplicate)",
        b_counts["inserted"], b_counts["skipped_no_session"], b_counts["skipped_duplicate"],
    )
    logger.info("crons: %d inserted", c_counts["inserted"])
    logger.info("heartbeats: %d inserted", h_counts["inserted"])
    logger.info("user_prefs: %d inserted", p_counts["inserted"])


# ---- CLI entry point ---------------------------------------------------------


@click.command()
@click.option(
    "--source",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory with JSON state files (default: ccgram_dir())",
)
@click.option(
    "--db",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Target SQLite database path (default: <source>/state.db)",
)
@click.option("--dry-run", is_flag=True, help="Run without committing any changes")
@click.option("-v", "--verbose", is_flag=True, help="Emit per-row debug lines")
def main(source: Path | None, db: Path | None, dry_run: bool, verbose: bool) -> None:
    """Migrate CCGram JSON state to SQLite."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    resolved_source = source or ccgram_dir()
    resolved_db = db or (resolved_source / "state.db")

    logger.info(
        "Migrating %s -> %s%s",
        resolved_source,
        resolved_db,
        " (DRY RUN)" if dry_run else "",
    )
    migrate(resolved_source, resolved_db, dry_run, verbose)
    logger.info("Done.")


if __name__ == "__main__":
    main()
