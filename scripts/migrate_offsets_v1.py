"""Replay-safe offset migration script.

Reconciles transcript_offset values between monitor_state.json and state.db,
taking the maximum of the two sources for each session.

Usage:
    python3 scripts/migrate_offsets_v1.py --dry-run   # default; writes plan, no DB writes
    python3 scripts/migrate_offsets_v1.py --apply     # single-TX UPDATEs

Exit codes: 0 success, 1 error, 2 refused
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CCGRAM_DIR = Path(os.environ.get('CCGRAM_DIR', Path.home() / '.ccgram'))
MONITOR_STATE_JSON = CCGRAM_DIR / 'monitor_state.json'
STATE_DB = CCGRAM_DIR / 'state.db'

JAMES_CANARY_SID = '886811e0-7632-4276-bbc3-03311caf9f53'
JAMES_CANARY_MIN_OFFSET = 31_137_954


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_monitor_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        print(f'[info] monitor_state.json not found at {path} — treating as empty')
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'[info] monitor_state.json unreadable ({exc}) — treating as empty')
        return {}

    result: dict[str, dict] = {}

    sessions_map: dict | None = None
    if isinstance(raw, dict):
        if 'tracked_sessions' in raw:
            sessions_map = raw['tracked_sessions']
        elif 'sessions' in raw:
            sessions_map = raw['sessions']
        else:
            sessions_map = {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and k != 'events_offset'
            }

    if not sessions_map:
        return {}

    for sid, entry in sessions_map.items():
        if not isinstance(entry, dict):
            continue
        offset = entry.get('last_byte_offset') or entry.get('offset') or 0
        transcript_path = (
            entry.get('file_path')
            or entry.get('transcript_path')
            or entry.get('transcript')
        )
        result[sid] = {
            'offset': int(offset),
            'transcript_path': transcript_path,
        }

    return result


def _load_db_sessions(db_path: Path) -> list[dict]:
    if not db_path.exists():
        print(f'[warning] state.db not found at {db_path} — treating as empty')
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT session_id, status, window_id, transcript_offset, transcript_path'
                ' FROM sessions'
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            print(f'[warning] sessions table unreadable ({exc}) — treating as empty')
            return []
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[warning] Could not open state.db ({exc}) — treating as empty')
        return []


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def build_plan(
    monitor_state: dict[str, dict],
    db_sessions: list[dict],
) -> tuple[list[dict], list[dict]]:
    plan: list[dict] = []
    anomalies: list[dict] = []

    db_by_sid: dict[str, dict] = {r['session_id']: r for r in db_sessions}
    db_active_sids: set[str] = {
        r['session_id'] for r in db_sessions if r['status'] == 'active'
    }

    all_sids = set(monitor_state.keys()) | db_active_sids

    for sid in all_sids:
        ms_entry = monitor_state.get(sid)
        db_row = db_by_sid.get(sid)

        ms_offset = ms_entry['offset'] if ms_entry else 0
        db_offset = int(db_row['transcript_offset'] or 0) if db_row else 0

        transcript_path: str | None = None
        if db_row and db_row.get('transcript_path'):
            transcript_path = db_row['transcript_path']
        elif ms_entry and ms_entry.get('transcript_path'):
            transcript_path = ms_entry['transcript_path']

        # Orphan: in JSON but not in DB at all
        if ms_entry and sid not in db_by_sid:
            plan.append({
                'sid': sid,
                'before_db': 0,
                'ms_offset': ms_offset,
                'after': ms_offset,
                'reason': 'orphan_no_db_row',
                'transcript_path': transcript_path,
                'skip': True,
            })
            continue

        # Active DB session not in monitor_state, with offset 0
        if sid not in monitor_state and sid in db_active_sids and db_offset == 0:
            if transcript_path:
                tp = Path(transcript_path)
                if tp.exists() and os.access(str(tp), os.R_OK):
                    effective = os.path.getsize(str(tp))
                    plan.append({
                        'sid': sid,
                        'before_db': db_offset,
                        'ms_offset': ms_offset,
                        'after': effective,
                        'reason': 'synthesize_file_end',
                        'transcript_path': transcript_path,
                        'skip': False,
                    })
                    continue

            plan.append({
                'sid': sid,
                'before_db': db_offset,
                'ms_offset': ms_offset,
                'after': 0,
                'reason': 'max_of_ms_db',
                'transcript_path': transcript_path,
                'skip': False,
            })
            continue

        # Standard case: take max of ms_offset and db_offset
        effective = max(ms_offset, db_offset)

        # Anomaly check: transcript file smaller than effective offset
        if transcript_path:
            tp = Path(transcript_path)
            if tp.exists():
                file_size = os.path.getsize(str(tp))
                if file_size < effective:
                    anomalies.append({
                        'sid': sid,
                        'before_db': db_offset,
                        'ms_offset': ms_offset,
                        'effective': effective,
                        'file_size': file_size,
                        'transcript_path': transcript_path,
                    })
                    continue  # Do NOT add to plan

        plan.append({
            'sid': sid,
            'before_db': db_offset,
            'ms_offset': ms_offset,
            'after': effective,
            'reason': 'max_of_ms_db',
            'transcript_path': transcript_path,
            'skip': False,
        })

    return plan, anomalies


def check_james_canary(plan: list[dict]) -> str | None:
    for entry in plan:
        if entry['sid'] == JAMES_CANARY_SID and not entry.get('skip'):
            if entry['after'] < JAMES_CANARY_MIN_OFFSET:
                return (
                    f'James canary check FAILED: sid={JAMES_CANARY_SID} '
                    f'after={entry["after"]} < minimum={JAMES_CANARY_MIN_OFFSET}'
                )
    return None


def apply_updates(db_path: Path, plan: list[dict]) -> int:
    updates = [
        (entry['after'], entry['sid'])
        for entry in plan
        if not entry.get('skip')
    ]
    if not updates:
        print('[apply] Nothing to update.')
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute('BEGIN')
        for after, sid in updates:
            conn.execute(
                'UPDATE sessions SET transcript_offset=? WHERE session_id=?',
                (after, sid),
            )
        conn.commit()
        print(f'[apply] Committed {len(updates)} UPDATE(s).')
        return len(updates)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')


def _write_plan_file(plan: list[dict], anomalies: list[dict]) -> Path:
    ts = _ts()
    plan_path = CCGRAM_DIR / f'migration-plan-{ts}.json'
    CCGRAM_DIR.mkdir(parents=True, exist_ok=True)
    with open(plan_path, 'w', encoding='utf-8') as fh:
        json.dump(
            [
                {
                    'sid': e['sid'],
                    'before_db': e['before_db'],
                    'ms_offset': e['ms_offset'],
                    'after': e['after'],
                    'reason': e['reason'],
                    'transcript_path': e.get('transcript_path'),
                }
                for e in plan
            ],
            fh,
            indent=2,
        )
    print(f'[plan] Written: {plan_path}')

    if anomalies:
        anomaly_path = CCGRAM_DIR / f'migration-anomalies-{ts}.tsv'
        with open(anomaly_path, 'w', encoding='utf-8') as fh:
            fh.write('sid\tbefore_db\tms_offset\teffective\tfile_size\ttranscript_path\n')
            for a in anomalies:
                fh.write(
                    f'{a["sid"]}\t{a["before_db"]}\t{a["ms_offset"]}\t'
                    f'{a["effective"]}\t{a["file_size"]}\t{a.get("transcript_path", "")}\n'
                )
        print(f'[anomalies] Written: {anomaly_path}')

    return plan_path


def _print_summary_table(plan: list[dict], anomalies: list[dict]) -> None:
    col_sid = 36
    col_num = 12
    header = (
        f'{"SID":<{col_sid}}  {"BEFORE":>{col_num}}  {"MS":>{col_num}}  '
        f'{"AFTER":>{col_num}}  REASON'
    )
    sep = '-' * len(header)
    print()
    print(header)
    print(sep)
    for entry in plan:
        skip_marker = ' [SKIP]' if entry.get('skip') else ''
        print(
            f'{entry["sid"]:<{col_sid}}  '
            f'{entry["before_db"]:>{col_num}}  '
            f'{entry["ms_offset"]:>{col_num}}  '
            f'{entry["after"]:>{col_num}}  '
            f'{entry["reason"]}{skip_marker}'
        )
    if anomalies:
        print()
        print('ANOMALIES (excluded from plan):')
        for a in anomalies:
            print(
                f'  {a["sid"]}  effective={a["effective"]}  file_size={a["file_size"]}  '
                f'path={a.get("transcript_path", "N/A")}'
            )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Replay-safe offset migration: reconciles monitor_state.json with state.db'
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Write plan file only, no DB writes (default behaviour)',
    )
    mode_group.add_argument(
        '--apply',
        action='store_true',
        default=False,
        help='Apply UPDATEs to state.db in a single transaction',
    )
    args = parser.parse_args(argv)

    do_apply = args.apply

    print(f'[info] CCGRAM_DIR={CCGRAM_DIR}')
    print(f'[info] MONITOR_STATE_JSON={MONITOR_STATE_JSON}')
    print(f'[info] STATE_DB={STATE_DB}')
    print(f'[info] Mode: {"apply" if do_apply else "dry-run"}')
    print()

    monitor_state = _load_monitor_state(MONITOR_STATE_JSON)
    db_sessions = _load_db_sessions(STATE_DB)

    print(f'[info] monitor_state entries: {len(monitor_state)}')
    print(f'[info] DB sessions: {len(db_sessions)}')

    plan, anomalies = build_plan(monitor_state, db_sessions)

    _print_summary_table(plan, anomalies)

    _write_plan_file(plan, anomalies)

    if anomalies:
        print(
            f'[refused] {len(anomalies)} anomaly/anomalies detected. '
            'Fix before applying. Exiting with code 2.'
        )
        return 2

    canary_err = check_james_canary(plan)
    if canary_err:
        print(f'[refused] {canary_err}. Exiting with code 2.')
        return 2

    if do_apply:
        try:
            count = apply_updates(STATE_DB, plan)
            print(f'[done] Applied {count} update(s).')
        except Exception as exc:
            print(f'[error] Apply failed: {exc}')
            return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
