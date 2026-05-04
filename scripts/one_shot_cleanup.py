#!/usr/bin/env python3
# Why: After a runaway watchdog cron created 83+ orphan active sessions overnight,
# we need a one-shot tool to retire them, rebind dead topic_bindings by window name,
# and regenerate session_map.json from scratch. Idempotent — safe to run twice.

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

CCGRAM_DIR = Path(os.environ.get("CCGRAM_DIR", Path.home() / ".ccgram"))
DB_PATH = CCGRAM_DIR / "state.db"
SESSION_MAP_PATH = CCGRAM_DIR / "session_map.json"
PTY_MARKERS_DIR = CCGRAM_DIR / "active-sessions"


def get_live_windows():
    """Return {window_id: window_name} for all windows in the ccgram tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-t", "ccgram", "-F", "#{window_id}\t#{window_name}"],
            capture_output=True, text=True, check=True
        )
        out = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1]
        return out
    except subprocess.CalledProcessError:
        print("WARNING: tmux session 'ccgram' not found — assuming no live windows", file=sys.stderr)
        return {}


def load_pty_markers():
    """Return {window_id: marker_dict} from active-sessions/*.json files."""
    markers = {}
    if not PTY_MARKERS_DIR.exists():
        return markers
    for f in PTY_MARKERS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            wid = data.get("window_id")
            if wid:
                markers[wid] = data
        except Exception:
            pass
    return markers


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Action 1 ──────────────────────────────────────────────────────────────────

def retire_orphan_sessions(conn, live_wids, dry_run):
    rows = conn.execute(
        "SELECT session_id, window_id FROM sessions WHERE status='active'"
    ).fetchall()

    orphans = []
    for row in rows:
        wid = row["window_id"]
        if not wid or wid not in live_wids:
            orphans.append(row["session_id"])

    if dry_run:
        for sid in orphans[:10]:
            print(f"  would retire {sid}")
        if len(orphans) > 10:
            print(f"  ... and {len(orphans) - 10} more")
    else:
        # retire in chunks of 500
        for i in range(0, len(orphans), 500):
            chunk = orphans[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE sessions SET status='retired', window_id=NULL WHERE session_id IN ({placeholders})",
                chunk
            )
        conn.commit()

    return len(orphans)


# ── Action 2 ──────────────────────────────────────────────────────────────────

def normalize_title(title):
    """Strip '[M] ' prefix and leading '⚡' then strip whitespace."""
    t = title
    if t.startswith("[M] "):
        t = t[4:]
    t = t.lstrip("⚡").strip()
    return t


def rebind_topic_bindings(conn, live_wids, pty_markers, dry_run):
    live_name_to_wid = {name: wid for wid, name in live_wids.items()}

    rows = conn.execute(
        "SELECT topic_id, topic_title, window_id, session_id FROM topic_bindings"
    ).fetchall()

    rebound = 0
    dropped = 0

    for row in rows:
        wid = row["window_id"]
        tid = row["topic_id"]
        title = row["topic_title"] or ""

        if wid and wid in live_wids:
            continue  # already healthy

        # Try to find a matching live window by name
        norm = normalize_title(title)
        new_wid = live_name_to_wid.get(norm) or live_name_to_wid.get(title)

        if new_wid:
            # Find active session for this window
            session_row = conn.execute(
                "SELECT session_id FROM sessions WHERE window_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
                (new_wid,)
            ).fetchone()
            new_sid = session_row["session_id"] if session_row else None

            # Fall back to PTY marker
            if not new_sid and new_wid in pty_markers:
                new_sid = pty_markers[new_wid].get("session_id")

            if new_sid:
                print(f"  REBIND topic={tid!r} title={title!r} -> wid={new_wid} sid={new_sid}")
                if not dry_run:
                    try:
                        conn.execute(
                            "UPDATE topic_bindings SET window_id=?, session_id=? WHERE topic_id=?",
                            (new_wid, new_sid, tid))
                        rebound += 1
                    except sqlite3.IntegrityError:
                        # Session not in DB yet (new window, no DB row) — drop and let ccgram recreate
                        conn.execute("DELETE FROM topic_bindings WHERE topic_id=?", (tid,))
                        dropped += 1
                        print(f"    -> FK failed, dropped instead")
                else:
                    rebound += 1
            else:
                print(f"  DROP   topic={tid!r} title={title!r} (window found but no active session)")
                if not dry_run:
                    conn.execute("DELETE FROM topic_bindings WHERE topic_id=?", (tid,))
                dropped += 1
        else:
            print(f"  DROP   topic={tid!r} title={title!r} (no live window match)")
            if not dry_run:
                conn.execute("DELETE FROM topic_bindings WHERE topic_id=?", (tid,))
            dropped += 1

    if not dry_run:
        conn.commit()

    return rebound, dropped


# ── Action 3 ──────────────────────────────────────────────────────────────────

def regenerate_session_map(conn, live_wids, pty_markers, dry_run):
    entries = {}

    for wid, name in live_wids.items():
        if name == "__main__":
            continue

        key = f"ccgram:{wid}"
        entry = None

        # PTY marker takes priority — but only if its transcript_path actually exists.
        # Stale markers (e.g. from a previous session on the same window) can reference
        # a JSONL that was never written; in that case the DB row is more reliable.
        if wid in pty_markers:
            m = pty_markers[wid]
            sid = m.get("session_id")
            tp = m.get("transcript_path", "")
            transcript_exists = bool(tp) and Path(tp).exists()
            if sid and transcript_exists:
                entry = {
                    "session_id": sid,
                    "provider_session_id": sid,
                    "cwd": m.get("cwd", ""),
                    "window_name": name,
                    "transcript_path": tp,
                    "provider_name": m.get("provider", ""),
                }

        # Fall back to DB
        if not entry:
            row = conn.execute(
                "SELECT session_id, cwd, transcript_path, agent FROM sessions"
                " WHERE window_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
                (wid,)
            ).fetchone()
            if row and row["session_id"]:
                sid = row["session_id"]
                entry = {
                    "session_id": sid,
                    "provider_session_id": sid,
                    "cwd": row["cwd"] or "",
                    "window_name": name,
                    "transcript_path": row["transcript_path"] or "",
                    "provider_name": row["agent"] or "claude",
                }

        if not entry:
            print(f"  SKIP {key} ({name}) — no session_id found")
            continue

        entries[key] = entry
        print(f"  MAP  {key} -> {entry['session_id'][:8]}... ({name})")

    if not dry_run and entries:
        fd, tmp = tempfile.mkstemp(dir=CCGRAM_DIR, prefix="session_map_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(entries, fh, indent=2)
            os.replace(tmp, SESSION_MAP_PATH)
        except Exception:
            os.unlink(tmp)
            raise

    return len(entries)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="One-shot CCGram DB cleanup tool")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print what would change, don't write")
    mode.add_argument("--apply", action="store_true", help="Apply all changes")
    args = parser.parse_args()
    dry_run = args.dry_run

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"CCGram dir : {CCGRAM_DIR}")
    print(f"DB         : {DB_PATH}")
    print(f"Mode       : {'dry-run' if dry_run else 'APPLY'}")
    print()

    live_wids = get_live_windows()
    print(f"Live tmux windows: {len(live_wids)}")
    pty_markers = load_pty_markers()
    print(f"PTY markers      : {len(pty_markers)}")
    print()

    conn = open_db()

    print("=== Action 1: Retire orphan active sessions ===")
    retired = retire_orphan_sessions(conn, live_wids, dry_run)
    print(f"  -> {retired} session(s) {'would be ' if dry_run else ''}retired")
    print()

    print("=== Action 2: Rebind dead-window topic_bindings ===")
    rebound, dropped = rebind_topic_bindings(conn, live_wids, pty_markers, dry_run)
    print(f"  -> {rebound} rebound, {dropped} dropped")
    print()

    print("=== Action 3: Regenerate session_map.json ===")
    map_count = regenerate_session_map(conn, live_wids, pty_markers, dry_run)
    print(f"  -> {map_count} entries {'would be ' if dry_run else ''}written")
    print()

    active_now = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE status='active'"
    ).fetchone()[0]

    conn.close()

    print("=== Summary ===")
    print(f"  Sessions retired   : {retired}")
    print(f"  Bindings rebound   : {rebound}")
    print(f"  Bindings dropped   : {dropped}")
    print(f"  Map entries written: {map_count}")
    print(f"  Active sessions now: {active_now}")
    if dry_run:
        print("  (dry-run — no changes written)")


if __name__ == "__main__":
    main()
