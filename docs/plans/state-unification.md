# State Unification — Design + Work Plan

**Status:** Draft — authored 2026-04-15, awaiting Peter's go-ahead to start chunks.
**Author:** James (Claude Code, james-2 session)
**Target branch:** `peter-fixes`

---

## Problem

Session/topic/window state lives in ~8 places and drifts. Today's incident on the `james` group exposed it:

- Two tmux windows (`@5 james`, `@19 james-claude-hub`) share the **same `session_id`** in `session_map.json` because they share a cwd. Transcript de-dup logic treats them as one session → messages fan out to both bindings.
- Thread bindings got corrupted across watchdog-triggered restarts: topic 86 (Telegram title "social-posts") ended up bound to the james window; topic 69 (Telegram title "james") ended up with no binding; topic 90 (Telegram title "seo-workspace") is wired to cwd for `foundbyacrowd`. User had to manually re-init sessions, producing phantom duplicates like `james-2`.
- `ccgram-watchdog.sh` is killing the daemon every few hours because it can't parse `Sweep completed` from the main pane and falls back to `age=99999s` → kill. Each kill re-runs the broken heal logic.
- Health-check cron fails every 5 minutes (`claude-hub: No such file or directory` — missing PATH in cron env), so auto-drift-detection is silently dead.

Root cause: state is keyed on derived heuristics (`cwd`, `window_name`) instead of immutable IDs; there is no single constrained store; there is no authoritative read-back for Telegram topic titles.

## Current state inventory

**ccgram-owned files (`~/.ccgram/`):**

| File | Contents | Written by |
|---|---|---|
| `session_map.json` | `window_id → session_id, cwd, transcript_path, provider` | `session_map.py` |
| `state.json` | `thread_bindings`, `window_states`, `window_display_names`, `group_chat_ids`, `user_window_offsets`, `user_dir_favorites` | `session.py` |
| `target-state.json` | Frozen "golden" snapshot for restore | `restore_command.py` |
| `monitor_state.json` | Session monitor resume state | `monitor_state.py` |
| `crons.json` | Cron definitions | cron CLI |
| `health-state.json` | Health check state | `scripts/health-check.sh` |
| `bg_work_shown.json`, `effort_shown.json` | Per-feature display state | `handlers/polling_coordinator.py` |
| `events.jsonl`, `debug/timeline-*.jsonl`, `debug/terminal-*.log` | Logs (not state, but consulted during heal) | various |

**External authorities (cannot collapse):**

| System | Authoritative for | Read via |
|---|---|---|
| tmux | Window existence, window IDs, pane PIDs, live cwd | `tmux list-panes -F ...` |
| Telegram | Topic IDs, topic titles (ground truth post-crash) | MTProto `channels.getForumTopicsByID` |
| Claude Code / Codex | Session identity, transcripts | `~/.claude/projects/<slug>/<sid>.jsonl` |
| User crontab | Scheduled shell jobs (watchdog, health-check) | `crontab -l` |

## Target architecture

### Single source: SQLite at `~/.ccgram/state.db`

One file, atomic multi-row writes, foreign keys, uniqueness constraints that catch corruption loudly. Schema (draft):

```sql
CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,        -- UUID, minted at spawn time
    cwd           TEXT NOT NULL,
    agent         TEXT NOT NULL,           -- 'claude' | 'codex' | 'gemini'
    mode          TEXT,                    -- e.g. 'yolo', 'summary', etc.
    status        TEXT NOT NULL,           -- 'pending' | 'active' | 'errored' | 'retired'
    window_id     TEXT,                    -- tmux @id; null while pending
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE topic_bindings (
    group_id      INTEGER NOT NULL,
    topic_id      INTEGER NOT NULL,
    session_id    TEXT NOT NULL UNIQUE,    -- ← hard guard: one session, one topic
    topic_title   TEXT NOT NULL,           -- last-known title from MTProto
    bound_at      INTEGER NOT NULL,
    PRIMARY KEY (group_id, topic_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE orphaned_topics (      -- topics we know about but aren't bound
    group_id      INTEGER NOT NULL,
    topic_id      INTEGER NOT NULL,
    topic_title   TEXT NOT NULL,
    first_seen    INTEGER NOT NULL,
    PRIMARY KEY (group_id, topic_id)
);

CREATE TABLE heartbeats (
    component     TEXT PRIMARY KEY,        -- 'ccgram-main', 'watchdog', etc.
    last_beat     INTEGER NOT NULL,
    details       TEXT                     -- JSON blob, optional
);

CREATE TABLE crons (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    schedule      TEXT NOT NULL,
    target_window TEXT NOT NULL,
    message       TEXT NOT NULL,
    enabled       INTEGER NOT NULL,
    last_run      INTEGER,
    last_result   TEXT,
    created_at    INTEGER NOT NULL
);

-- User prefs, window offsets, favorites etc. → kv table or dedicated tables, TBD
```

**Not stored in DB:** tmux window list (use tmux at read-time), transcript contents (read from JSONL), Telegram topic metadata beyond last-known-title (use MTProto at read-time). The DB stores *bindings* and *user intent*, not derived facts.

### MTProto user client (read-only)

Alongside the existing bot. Login once (phone code), session file at `~/.ccgram/mtproto.session`. Used only for reconcile reads:

- `list_forum_topics(group_id)` → `[(topic_id, title, top_msg_id, is_closed, is_hidden), ...]`
- `get_forum_topics_by_id(group_id, [ids])` → same, but filtered

Never writes via MTProto — all writes still go through Bot API for audit trail.

### Canonical `create_session()` flow

```
create_session(cwd, topic_name, agent, mode, group_id, existing_topic_id=None)
    -> session_id

1. Mint new session_id (UUID)
2. INSERT sessions (status='pending', window_id=NULL)
3. If existing_topic_id:
      MTProto verify: topic exists, title == topic_name
   Else:
      Bot API createForumTopic(group_id, topic_name)
      Record returned topic_id
4. tmux new-window -n <topic_name> -c <cwd>, capture window_id
5. Launch agent in the pane with CCGRAM_SESSION_ID=<sid> env var
6. INSERT topic_bindings (group_id, topic_id, session_id, topic_title)
   -- UNIQUE(session_id) catches cross-link attempts
7. UPDATE sessions SET window_id=..., status='active'
8. (All in one transaction; on any failure rollback + cleanup side effects)
```

### `reconcile()` — the new heal

Runs on startup and on-demand via `claude-hub reconcile`. Pulls all four authorities and produces a diff with per-field authority:

| Field | Authority |
|---|---|
| Window exists? | tmux |
| Topic exists + current title | MTProto |
| Session identity (which JSONL is real) | Claude Code transcripts (by `CCGRAM_SESSION_ID` marker) |
| User-chosen bindings | DB `topic_bindings` |
| User preferences (cwd favorites, display offsets) | DB |

Any mismatch: surface loudly, require confirmation for destructive fixes. Never silently rebind.

### Single spawn path, three front doors

1. **Telegram** — user creates topic OR messages an unbound topic → existing `topic_lifecycle` / `topic_orchestration` UX picker (cwd, agent, mode) → calls `create_session()` under the hood.
2. **Web console** — form in `ccgram-dashboard/dashboard.py` → POST to `/api/spawn` → calls `create_session()`.
3. **CLI** — `claude-hub spawn --cwd=X --topic=Y --agent=claude` → calls `create_session()`.

Mirror operation `delete_session(session_id)`: kill tmux window, hide/close Telegram topic, mark retired in DB. Same three front doors.

---

## Work plan — chunks

Dependency graph:

```
Phase 1 (parallel)      Phase 2 (needs 1)      Phase 3 (needs 2)        Phase 4
  A. SQLite schema   ─┐                                                    
                     ├──► C. create/delete   ─┐                            
  B. MTProto client  ─┘                       ├─► E. Entry-point wiring ─┐
                      ──► D. reconcile()    ──┘                           ├──► H. Retire legacy
                                                F. Transcript→sid keying ─┤       JSON + docs + tests
                                                G. Watchdog + cron fixes ─┘
```

### Chunk A — SQLite schema + migration tool

**Deliverable:** `src/ccgram/store.py` (new) with DDL, connection helper, and CRUD functions for `sessions`, `topic_bindings`, `orphaned_topics`, `heartbeats`, `crons`. Migration script `scripts/migrate_to_sqlite.py` that reads all current `~/.ccgram/*.json` files and populates the DB. Read-only at first — legacy JSON files remain unmodified.

**Acceptance:**
- `pytest tests/test_store.py` covers: schema creation, uniqueness violation on duplicate session_id in `topic_bindings`, FK cascade on session delete, migration from real `~/.ccgram` snapshot.
- Migration is idempotent (running twice is a no-op).
- `claude-hub db-dump` CLI subcommand for inspection.

### Chunk B — MTProto read client

**Deliverable:** `src/ccgram/mtproto_client.py` with login flow, session storage, and `list_forum_topics` + `get_forum_topics_by_id`. Uses `telethon`. CLI: `claude-hub topics list --group <id>` for manual verification.

**Acceptance:**
- Login flow stores session at `~/.ccgram/mtproto.session` with 600 perms.
- `list_forum_topics` on the james group returns the real topic IDs + titles (verified against the corruption we spotted: topic 86 should be "social-posts", 90 "seo-workspace", 69 "james").
- No write methods implemented — code review confirms read-only.

### Chunk C — `create_session()` + `delete_session()` transactional functions

**Deliverable:** `src/ccgram/session_lifecycle.py` (new) implementing both functions. Full rollback on any failure. Depends on A + B.

**Acceptance:**
- Integration test that spawns a real tmux window, creates a real Telegram topic in a test group, binds them, then deletes — asserting DB + tmux + Telegram all clean at the end.
- Deliberate-failure tests: MTProto verify fails → nothing persists; tmux new-window fails → topic deleted; agent launch fails → window killed + topic deleted.
- `UNIQUE(session_id)` violation is the expected error if caller tries to double-bind.

### Chunk D — `reconcile()` with authority order

**Deliverable:** `src/ccgram/reconcile.py` replacing current heal logic. Pulls from tmux, MTProto, transcripts, DB. Produces a `ReconcileReport` with discrepancies classified `AUTO_FIX` (safe) vs `MANUAL_REVIEW` (destructive). Depends on A + B.

**Acceptance:**
- Unit tests for each mismatch class (orphan topic, orphan window, title drift, missing session_id marker, two windows claiming same session_id).
- `claude-hub reconcile --dry-run` prints the report.
- `claude-hub reconcile --apply` applies AUTO_FIX changes only.

### Chunk E — Wire entry points into create_session()

**Deliverable:** Refactor `handlers/topic_lifecycle.py`, `handlers/topic_orchestration.py`, `handlers/directory_callbacks.py` to call `create_session()` instead of writing scattered state. Add `forum_topic_created` proactive prompt. Add `claude-hub spawn` CLI subcommand. Add `/api/spawn` and `/api/sessions` routes to `ccgram-dashboard/dashboard.py`. Depends on C.

**Acceptance:**
- User sends first message to an unbound topic → existing picker UX appears → selection calls `create_session()` → DB row written, tmux window spawned.
- User creates a new topic in Telegram → bot posts prompt with cwd/agent inline keyboard in that topic.
- `claude-hub spawn --cwd=X --topic=Y --agent=claude` works end-to-end.
- Web dashboard has a "New session" form that works.
- All three paths produce identical DB state for identical inputs.

### Chunk F — Transcript watcher keys on session_id

**Deliverable:** Refactor `src/ccgram/session_watcher.py` and related code to key on `session_id` (from `CCGRAM_SESSION_ID` env marker) rather than cwd/window heuristics. Write a per-window marker file `~/.ccgram/debug/terminal-<window_id>.sid` at spawn time as fallback. Depends on C.

**Acceptance:**
- Two tmux windows in the same cwd produce two distinct session_ids and two distinct transcript streams; messages do not cross-fan-out.
- Regression test: reproduce the `@5`/`@19` double-binding scenario from today; assert no cross-linking occurs.
- Loud error (not silent dedup) if two windows ever claim the same session_id.

### Chunk G — Watchdog + cron fixes

**Deliverable:** 
1. Rewrite `ccgram-dashboard/ccgram-watchdog.sh` to use the `heartbeats` table (ccgram writes a beat every N seconds, watchdog reads it) instead of parsing pane output.
2. Fix `~/ccgram-dashboard/scripts/health-check.sh` PATH so `claude-hub` resolves.
3. Deduplicate the two crontab health-check entries (keep one).
4. Re-enable `Infrastructure Health Check` ccgram cron.

**Acceptance:**
- 24-hour soak: no false-positive watchdog restarts logged.
- `~/.ccgram/health-check.log` shows clean `HEALTH_OK` ticks with no `claude-hub: No such file or directory` errors.
- Only one health-check run per tick in the log (no duplicates).

### Chunk H — Retire legacy JSON + final tests + docs

**Deliverable:** Remove all reads from `session_map.json`, `state.json`, `target-state.json`, `monitor_state.json`. Retain them as debug *dumps* written alongside DB writes for one release cycle, then delete. Update `ARCHITECTURE.md`. Write migration runbook for users upgrading. Depends on E + F + G.

**Acceptance:**
- `grep -r "session_map.json\|state.json" src/` returns no reads, only optional debug writes.
- `ARCHITECTURE.md` describes the new data flow with a diagram.
- End-to-end test: fresh install → spawn 3 sessions via different entry points → restart ccgram → all sessions reconciled correctly → teardown all → DB + tmux + Telegram clean.

---

## Decisions still to make

1. **KV for prefs vs dedicated tables?** `user_window_offsets`, `user_dir_favorites`, `window_display_names` — probably a single `user_prefs(key, value_json)` table unless we want query on them.
2. **Display name rule** — should `topic_title` in DB ever be authoritative, or always re-fetched via MTProto? Proposed: DB stores last-known for UX/display speed; authoritative reads go through MTProto on reconcile.
3. **Migration order for live system** — can we run Chunk A + migration tool live (read-only, safe), or does it need a maintenance window? Proposed: live is fine because JSON is the authority until Chunk H.
4. **Pippa CCGram on mrsclawd** — same fork deployment; coordinate rollout so both instances migrate together or each gets its own DB.
5. **Backup/rollback** — nightly `sqlite3 .backup` + keep last 7. Add to `nightly-backup.sh`.
