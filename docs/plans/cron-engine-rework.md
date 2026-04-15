# Phase 5 — Cron Engine + Dispatch Rework

**Status:** Draft 2026-04-15 20:30 UTC after state-unification Phase 4.
**Target branch:** `peter-fixes`
**Author:** James

---

## Why

State-unification (Phases 1–4) moved session state to SQLite; topic bindings now have immutable `session_id`. But the cron engine and a handful of related handlers still key on `window_name`, which is mutable and ambiguous. During today's live migration this produced a cascade:

1. Cron fires → `claude-hub send <window_name> ...`
2. Window output goes to whichever topic is in `thread_bindings` for that window
3. If binding missing or stale, `topic_orchestration` auto-creates a new topic
4. Effort-suffix code renames target topic to match the window name
5. Telegram topic now points at wrong project; reconcile shows title drift

We've mitigated symptoms (kill-switch on auto-create, cron disable during migration) but the root cause — routing by mutable names instead of DB IDs — remains.

## Design

### Cron schema

Extend `crons` table (from Chunk A) with new columns; keep legacy fields for one release cycle:

```sql
ALTER TABLE crons ADD COLUMN target_session_id TEXT;   -- canonical
ALTER TABLE crons ADD COLUMN target_topic_id INTEGER;  -- canonical
ALTER TABLE crons ADD COLUMN target_group_id INTEGER;  -- canonical
-- existing target_window column stays; marked DEPRECATED in comments.
```

At fire time:
1. Prefer `target_session_id` → `store.get_session(sid)` → `window_id` → dispatch.
2. Fallback: `(target_group_id, target_topic_id)` → `store.get_topic_binding()` → `session_id` → resolve.
3. Last resort: legacy `target_window` — but log a WARNING.

If the resolved `window_id` isn't live in tmux, the cron should attempt a clean restart via `create_session(existing_topic_id=...)` rather than silently failing or (worse) triggering `topic_orchestration.auto-create`.

### Retire topic_orchestration._bind_topic_to_user + auto-create

Today's kill-switch (`CCGRAM_ALLOW_AUTO_TOPIC` env guard) is a temporary gate. Phase 5 removes the auto-create path entirely. The only way to create a new topic becomes:

- `claude-hub spawn` (CLI)
- `create_session` called from the user-driven directory-picker UX (handlers/directory_callbacks.py)
- `forum_topic_created` handler when user makes a new topic in Telegram

Windows with no binding + incoming output get a **one-time operator alert** on topic 7 (or a configurable alert thread), not a silent auto-create. The alert links to "open `claude-hub reconcile` or `claude-hub spawn`".

### Effort-suffix code respects user-chosen titles

`handlers/polling_coordinator.py`'s `_apply_effort_suffix` currently rewrites the Telegram topic title to `<window_name> <suffix>` on every effort change. This overwrites any user-chosen title (e.g., Peter renamed topic 69 to "James"; the suffix code changed it back to "james-2 [M]").

Fix: before renaming, compare the current title's base (stripped of `⚡`, `[M]`, `[L]`, etc.) against the window name. If they diverge, assume the user chose a custom title; only update the suffix portion, not the base.

### Cron edit UX

Add `claude-hub cron-edit <id> [--topic ID] [--session ID] [--group ID]` so operators can migrate existing crons from window-based to session/topic-based targeting without hand-editing `crons.json`. Also `claude-hub cron-list --stale` to find crons whose window target no longer resolves.

---

## Chunks

### I — Cron schema + dispatch rework (foundation + logic in one chunk)

`src/ccgram/store.py`: add new columns, update `list_crons`/`upsert_cron` signatures. Migration in `scripts/migrate_to_sqlite.py` maps existing `target_window` to best-guess `target_session_id` by looking up window_store.
`src/ccgram/cron_runner.py` (or wherever the dispatch loop lives): resolve targets via DB with fallback ladder. Tests for resolve-happy-path, resolve-miss-window-id, resolve-only-legacy-target_window.

### J — Retire auto-create + alert path

`src/ccgram/handlers/topic_orchestration.py`: remove `_bind_topic_to_user` and the `bot.create_forum_topic` call. Replace with "post an alert to operator thread, suppress" — or, if an `existing_topic_id` is in scope, route through `create_session`. Remove the temporary `CCGRAM_ALLOW_AUTO_TOPIC` kill-switch (the new path doesn't need it). Update tests.

### K — Effort-suffix respects user titles

`src/ccgram/handlers/polling_coordinator.py`: `_apply_effort_suffix` reads current topic title via MTProto, strips the suffix, compares base to window name. If divergent, preserve base. Unit tests with monkeypatched MTProto.

### L — Cron CLI UX

`/home/peter/ccgram-dashboard/claude-hub`: add `cron-edit`, `cron-list --stale`. Update `cron-add` to accept `--topic` / `--session` (resolved to IDs) and write the new columns.

### Dependency graph

```
I ────┬──► J
      └──► L
K (independent)
```

### Rollout

1. Ship I + K in parallel (both small; no write conflicts).
2. Ship J (depends on I).
3. Ship L (depends on I).
4. Soak 48h with crons re-enabled, watch heartbeats + reconcile.
5. Delete the `target_window` column once crons are all migrated (Phase 6).
