# CCGram — Architecture

Last updated: 2026-04-15 (post state-unification Phase 4 / Chunk H).

CCGram bridges Telegram forum topics to tmux-hosted agent sessions (Claude
Code, Codex, Gemini). This document focuses on the **state model** that was
unified in Phases 1–4.

---

## State

The authoritative store is a single SQLite database at
`~/.ccgram/state.db`, managed by [`src/ccgram/store.py`](../src/ccgram/store.py).

### What the DB owns

| Table              | Purpose                                          |
|--------------------|--------------------------------------------------|
| `sessions`         | Agent sessions (`session_id` PK, cwd, agent…)    |
| `topic_bindings`   | Telegram `(group_id, topic_id) ↔ session_id`, 1:1|
| `orphaned_topics`  | Topics seen via MTProto, not yet bound           |
| `heartbeats`       | Liveness beats from long-running components     |
| `crons`            | Scheduled-message definitions                   |
| `user_prefs`       | KV store for per-scope prefs (monitor offsets, window favourites, display names…) |

`topic_bindings.session_id` is `UNIQUE` — the DB itself prevents the
"two topics share one session" corruption that motivated the refactor.

### External authorities (cannot collapse)

| System             | Authoritative for                          | Read via                           |
|--------------------|--------------------------------------------|------------------------------------|
| tmux               | Window existence, window IDs, pane PIDs    | `tmux list-panes -F ...`           |
| MTProto (Telegram) | Topic IDs + current titles (ground truth)  | `MTProtoClient.list_forum_topics`  |
| Claude Code JSONLs | Session identity, transcripts              | `~/.claude/projects/<slug>/<sid>.jsonl` |

The DB stores *bindings and user intent*, not derived facts.

---

## Session lifecycle

```
 create_session(cwd, topic, agent) ───► reconcile() ───► delete_session(sid)
           │                                 ▲                    │
           ▼                                 │                    ▼
   1. DB INSERT (pending)              DB SELECT + diff       DB DELETE
   2. createForumTopic (Bot API)       tmux list_windows      (CASCADE drops
   3. tmux new-window (+ SID env)      MTProto titles          binding row)
   4. DB UPDATE (active, window_id)    JSONL identity         tmux kill-window
                                                              Bot closeForumTopic
```

All four steps of `create_session()` run in a single transaction with
side-effect rollback on failure.

---

## `CCGRAM_SESSION_ID` marker

Every session launched via `create_session()` gets a UUID that is:

1. Set as env var `CCGRAM_SESSION_ID=<uuid>` in the tmux pane.
2. Written to `~/.ccgram/debug/terminal-<window_id>.sid` as fallback.

The transcript watcher keys on this marker, so two windows in the same cwd
never cross-link. If both mechanisms are absent, the watcher emits a loud
warning and falls back to `cwd + mtime` heuristics (legacy behaviour,
kept only for pre-Phase-3 sessions).

---

## Watchdog heartbeat

```
Mailbox.sweep ──► ~/.ccgram/heartbeats/ccgram-main.txt ──► ccgram-watchdog.sh
                 (epoch seconds, updated every sweep)     (age > threshold → restart)
```

The file-based heartbeat is tolerant of tmux server restarts and does not
rely on parsing the main pane's stdout (which was the root cause of the
false-positive watchdog kills seen in March 2026).

---

## Data flow

```
   ┌─────────┐   ┌──────────────┐   ┌──────────┐
   │  tmux   │   │  MTProto     │   │  Claude  │
   │  (wins) │   │  (titles)    │   │  JSONLs  │
   └────┬────┘   └──────┬───────┘   └─────┬────┘
        │               │                 │
        └───────┬───────┴────────┬────────┘
                ▼                ▼
            ┌───────────────────────┐
            │    reconcile()        │
            │    ReconcileReport    │
            │    AUTO_FIX | MANUAL  │
            └──────────┬────────────┘
                       ▼
            ┌───────────────────────┐
            │  store.py   (SQLite)  │
            │  ~/.ccgram/state.db   │
            └───────────────────────┘
                       ▲
                       │
      create_session() │ delete_session()
                       │
          Telegram ◄───┼───► CLI  ◄──► Web dashboard
```

---

## Legacy JSON files

Four JSON files (`session_map.json`, `state.json`, `target-state.json`,
`monitor_state.json`) are still written as shadow copies for rollback
safety. Production read paths prefer the DB and fall back to JSON only
on first boot (with a WARNING log line).

Retirement criteria are documented in
[`docs/plans/state-unification-runbook.md`](plans/state-unification-runbook.md#legacy-file-retirement-timeline).
