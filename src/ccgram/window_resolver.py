"""Window ID resolution, format helpers, and startup migration.

Provides shared window ID helpers used across session, tmux_manager, and
handler modules (no intra-package imports — safe from circular dependencies):
  - is_window_id(): validate tmux window ID format (@0, @12).
  - is_foreign_window(): detect foreign session IDs (emdash-...:@N).
  - EMDASH_SESSION_PREFIX: shared constant for emdash session naming.
  - resolve_stale_ids(): full startup recovery — remaps persisted window IDs
    against live tmux windows, handles old-format migration, prunes dead entries.
"""

from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class LiveWindow:
    """Minimal representation of a live tmux window for resolution."""

    window_id: str
    window_name: str


def is_window_id(key: str) -> bool:
    """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
    return key.startswith("@") and len(key) > 1 and key[1:].isdigit()


EMDASH_SESSION_PREFIX = "emdash-"


def is_foreign_window(window_id: str) -> bool:
    """Check if window_id refers to a foreign tmux session (e.g. emdash).

    Foreign IDs use the format "session_name:@N" (contain a colon and don't
    start with "@").
    """
    return ":" in window_id and not window_id.startswith("@")


def _resolve_window_states(
    window_states: dict,
    window_display_names: dict,
    live_by_name: dict[str, str],
    live_ids: set[str],
) -> bool:
    """Re-resolve window_states dict in-place. Returns True if changed."""
    changed = False
    new_states: dict = {}
    for key, ws in window_states.items():
        # Foreign windows (emdash) are managed externally — preserve as-is
        if is_foreign_window(key):
            new_states[key] = ws
            continue
        if is_window_id(key):
            if key in live_ids:
                new_states[key] = ws
            else:
                display = window_display_names.get(
                    key, getattr(ws, "window_name", "") or key
                )
                new_id = live_by_name.get(display)
                if new_id:
                    logger.debug("Re-resolved stale window_id %s -> %s", key, new_id)
                    new_states[new_id] = ws
                    ws.window_name = display
                    window_display_names[new_id] = display
                    window_display_names.pop(key, None)
                    changed = True
                else:
                    # Keep dead window state — recovery needs cwd/provider
                    new_states[key] = ws
        else:
            new_id = live_by_name.get(key)
            if new_id:
                logger.debug("Migrating window_state key %s -> %s", key, new_id)
                ws.window_name = key
                new_states[new_id] = ws
                window_display_names[new_id] = key
                changed = True
            else:
                logger.debug("Dropping old-format window_state: %s", key)
                changed = True
    window_states.clear()
    window_states.update(new_states)
    return changed


def _lookup_window_name(
    wid: str,
    window_display_names: dict,
    window_states: dict | None,
) -> str:
    """Find the canonical window name for a stale window_id.

    Checks multiple sources in priority order:
    1. window_display_names[wid]
    2. window_states[wid].window_name
    3. The wid itself (fallback — matches the old behaviour)

    This lets us heal bindings even when one source dict has lost track
    of the display name. Without this, a thread binding to a dead @N
    whose name is only in window_states but not window_display_names
    would never heal and would keep showing the recovery UI.
    """
    name = window_display_names.get(wid)
    if name:
        return name
    if window_states is not None:
        state = window_states.get(wid)
        if state is not None:
            ws_name = getattr(state, "window_name", "") or ""
            if ws_name:
                return ws_name
    return wid


def _resolve_thread_bindings(
    thread_bindings: dict,
    window_display_names: dict,
    live_by_name: dict[str, str],
    live_ids: set[str],
    window_states: dict | None = None,
) -> bool:
    """Re-resolve thread_bindings dict in-place. Returns True if changed."""
    changed = False
    for uid, bindings in thread_bindings.items():
        new_bindings: dict[int, str] = {}
        for tid, val in bindings.items():
            # Foreign windows (emdash) — preserve as-is
            if is_foreign_window(val):
                new_bindings[tid] = val
                continue
            if is_window_id(val):
                if val in live_ids:
                    new_bindings[tid] = val
                else:
                    name = _lookup_window_name(val, window_display_names, window_states)
                    new_id = live_by_name.get(name)
                    if new_id:
                        logger.info(
                            "Healed thread binding %d: %s -> %s (name=%s)",
                            tid,
                            val,
                            new_id,
                            name,
                        )
                        new_bindings[tid] = new_id
                        window_display_names[new_id] = name
                        changed = True
                    else:
                        # Keep dead window binding — /restore needs it
                        new_bindings[tid] = val
            elif new_id := live_by_name.get(val):
                logger.debug("Migrating thread binding %s -> %s", val, new_id)
                new_bindings[tid] = new_id
                window_display_names[new_id] = val
                changed = True
            else:
                logger.debug(
                    "Dropping old-format thread binding: user=%d, thread=%d, name=%s",
                    uid,
                    tid,
                    val,
                )
                changed = True
        bindings.clear()
        bindings.update(new_bindings)

    empty_users = [uid for uid, b in thread_bindings.items() if not b]
    for uid in empty_users:
        del thread_bindings[uid]
    return changed


def _resolve_offsets(
    user_window_offsets: dict,
    window_display_names: dict,
    live_by_name: dict[str, str],
    live_ids: set[str],
) -> bool:
    """Re-resolve user_window_offsets dict in-place. Returns True if changed."""
    changed = False
    for _uid, offsets in user_window_offsets.items():
        new_offsets: dict[str, int] = {}
        for key, offset in offsets.items():
            # Foreign windows (emdash) — preserve as-is
            if is_foreign_window(key):
                new_offsets[key] = offset
                continue
            if is_window_id(key):
                if key in live_ids:
                    new_offsets[key] = offset
                elif new_id := live_by_name.get(window_display_names.get(key, key)):
                    new_offsets[new_id] = offset
                    changed = True
                else:
                    changed = True
            elif new_id := live_by_name.get(key):
                new_offsets[new_id] = offset
                changed = True
            else:
                changed = True
        offsets.clear()
        offsets.update(new_offsets)
    return changed


def _purge_corrupt_display_names(window_display_names: dict) -> bool:
    """Remove display-name entries for non-canonical window keys.

    CCGram can accumulate stale entries in window_display_names when it's
    restarted from inside a web-terminal mirror session (keys like
    "web-...:@N" or "ccgram:@N"). These entries pollute the name lookup
    and prevent healing from working correctly. We drop any key that
    isn't a plain "@N" window id.
    """
    changed = False
    for key in list(window_display_names.keys()):
        if not is_window_id(key):
            del window_display_names[key]
            changed = True
            logger.info("Purged corrupt display name entry: %s", key)
    return changed


def resolve_stale_ids(
    live_windows: list[LiveWindow],
    window_states: dict,
    thread_bindings: dict,
    user_window_offsets: dict,
    window_display_names: dict,
) -> bool:
    """Re-resolve persisted window IDs against live tmux windows.

    Mutates all dicts in-place. Returns True if any changes were made.

    Handles three cases:
    1. Old-format migration: window_name keys -> window_id keys
    2. Stale IDs: window_id no longer exists but display name matches a live window
    3. Corrupt display name entries (web-* / ccgram:* prefixes) — purged
    """
    live_by_name: dict[str, str] = {w.window_name: w.window_id for w in live_windows}
    live_ids: set[str] = {w.window_id for w in live_windows}

    # Pre-clean corrupt display name entries before they confuse healing.
    changed = _purge_corrupt_display_names(window_display_names)

    changed |= _resolve_window_states(
        window_states, window_display_names, live_by_name, live_ids
    )
    changed |= _resolve_thread_bindings(
        thread_bindings,
        window_display_names,
        live_by_name,
        live_ids,
        window_states=window_states,
    )
    changed |= _resolve_offsets(
        user_window_offsets, window_display_names, live_by_name, live_ids
    )
    return changed
