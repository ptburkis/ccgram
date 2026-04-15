# State Unification — Live Migration Runbook

**Status:** Draft 2026-04-15 after Phase 3 committed.
**Target:** Peter's live james group (chat_id `-1003568873755`) on clawd.
**Prerequisites:** Phase 1, 2, 3 all committed on `peter-fixes` (HEAD ≥ `787fc49`).

---

## Goal

Move every existing Telegram topic ↔ tmux window pairing from the legacy heuristic bindings to the new DB-backed `create_session()` path, so that:

1. Every session has a `CCGRAM_SESSION_ID` marker (file + pane env).
2. Every `topic_bindings` row points at the correct topic via MTProto-verified title.
3. `reconcile --apply` runs cleanly with no drift.
4. The old `heal` path is never triggered again.

---

## Steps

### 0. Snapshot first

```bash
cp -r ~/.ccgram ~/.ccgram.pre-migration-$(date +%Y%m%d-%H%M%S)
cd ~/.openclaw/workspace/repos/ccgram && git log --oneline -10 > /tmp/ccgram-git-before.log
```

Tmux state cannot be snapshotted, but write down the current `tmux list-windows -t ccgram` output for reference.

### 1. Reinstall ccgram

```bash
uv tool install --force --reinstall --from ~/.openclaw/workspace/repos/ccgram ccgram
```

This updates the binary that the main tmux window runs. It does NOT restart the running daemon.

### 2. Stop every active session cleanly

For each bound Telegram topic, ask the agent to finish up and save state. Then:

```bash
# In the ccgram main window (or via tmux send-keys)
tmux kill-window -t ccgram:<window_name>   # for each active project window
```

Skip `__main__` (the ccgram daemon itself) — that restarts next.

### 3. Restart the ccgram daemon

```bash
tmux send-keys -t ccgram:__main__ C-c
sleep 3
tmux send-keys -t ccgram:__main__ "TMUX_SESSION_NAME=ccgram ccgram" Enter
```

Wait 10s. Then verify:
```bash
cat ~/.ccgram/heartbeats/ccgram-main.txt    # should show current epoch, updating every sweep
ls -la ~/.ccgram/state.db                   # should exist, from migration
```

### 4. Run reconcile dry-run

```bash
claude-hub reconcile --group -1003568873755
```

Expected output: `orphan_topic` for every topic whose session you just stopped (no active window = no binding). `title_drift` on any whose DB title doesn't match Telegram. No `duplicate_binding`. One `orphan_window` per stale window ID left in DB — ignore those; they'll clear when you spawn new sessions.

### 5. Re-spawn each project session via the new flow

For each project (maintain list below — tick off as you go):

```bash
claude-hub spawn --cwd /home/peter/projects/<project> \
                 --topic <telegram_topic_title> \
                 --agent claude \
                 --mode yolo \
                 --group -1003568873755 \
                 --existing-topic-id <id>     # optional: bind to existing topic instead of creating new
```

Project-to-topic mapping (from today's MTProto run):

| Topic ID | Title | Window name | cwd | Agent |
|---|---|---|---|---|
| 39 | bulugo-dev | bulugo-dev | `~/projects/bulugo_lead_gen` | claude |
| 69 | james-2 | james | `~/projects/james` | claude |
| 86 | social-posts | social-posts | `~/projects/social-posts` | claude |
| 87 | new-life | new-life | `~/projects/new-life` | claude |
| 90 | seo-workspace | seo-workspace | `~/projects/seo-workspace` | claude |
| 92 | family-wellbeing | family-wellbeing | `~/projects/family-wellbeing` | claude |
| 138 | bulugo-monitor | bulugo-monitor | `~/projects/bulugo_lead_gen` | claude |
| 249 | tweengifts | tweengifts | `~/projects/tweengifts` | claude |
| 251 | fittheroom | fittheroom | `~/projects/fittheroom` | claude |
| 252 | foundbyacrowd | foundbyacrowd | `~/projects/foundbyacrowd` | claude |
| 529 | james-claude-hub | james-claude-hub | `~/projects/james` | claude |
| 1258 | memory-dreams | memory-dreams | `~/projects/memory-dreams` | claude |
| 1553 | bulugo_lead_gen | bulugo_lead_gen | `~/projects/bulugo_lead_gen` | claude |
| 3185 | bulugo_lead_gen_codex | bulugo_lead_gen_codex | `~/projects/bulugo_lead_gen` | codex |
| 3508 | bulugo_dev_claude | bulugo_dev_claude | `~/projects/bulugo_lead_gen` | claude |
| 4792 | hub-orchestrator | hub-orchestrator | `~/projects/hub-orchestrator` | claude |
| 7046 | paint-my-room | paint-my-room | `~/projects/paint-my-room` | claude |
| 7083 | bulugo-bug-fix | bulugo-bug-fix | `~/projects/bulugo_lead_gen` | claude |
| 7084 | bulugo-project-management | bulugo-project-management | `~/projects/bulugo_lead_gen` | claude |
| 7236 | usage-scraper-codex | usage-scraper-codex | `~/projects/usage-scraper` | codex |
| 7242 | usage-scraper | usage-scraper | `~/projects/usage-scraper` | claude |
| 7309 | james-claude-hub [M] | james-claude-hub | `~/projects/james` | claude |
| 7310 | memory-dreams | memory-dreams | `~/projects/memory-dreams` | claude |
| 7318 | tweengifts | tweengifts | `~/projects/tweengifts` | claude |

**Review this table carefully before spawning — some topics may be duplicates (529 + 7309 both "james-claude-hub"; 249 + 7318 both "tweengifts"; 1258 + 7310 both "memory-dreams"). Decide which to keep active and which to close.**

### 6. Re-run reconcile dry-run

```bash
claude-hub reconcile --group -1003568873755
```

Should show: zero `orphan_topic` (every topic bound), zero `duplicate_binding`, zero `ambiguous_session`. `title_drift` might still appear if MTProto-live titles differ from what you passed as `--topic`; those are safe to `--apply`.

### 7. Apply auto-fix issues

```bash
claude-hub reconcile --group -1003568873755 --apply
```

Now `title_drift` items (safe with session_id markers now in place) are absorbed silently. `manual_review` items remain in the report for you to triage.

### 8. Verify end-to-end

- Send a test message to any one topic → confirm it goes to the expected tmux window and the agent responds in the right topic.
- `claude-hub diagnose` → heartbeats current, no watchdog restarts in last hour.
- `tail -f ~/.ccgram/health-check.log` → `HEALTH_OK` ticks, no PATH errors.

---

## Rollback

If anything goes catastrophically wrong:

```bash
# Stop ccgram
tmux send-keys -t ccgram:__main__ C-c

# Restore legacy state
mv ~/.ccgram/state.db ~/.ccgram/state.db.aborted-$(date +%s)
cp -rv ~/.ccgram.pre-migration-*/state.json ~/.ccgram/state.json
cp -rv ~/.ccgram.pre-migration-*/session_map.json ~/.ccgram/session_map.json
# etc.

# Revert ccgram install
cd ~/.openclaw/workspace/repos/ccgram
git log --oneline -5   # find commit before Phase 1 (e088d68^)
# Downgrade isn't the cleanest — easier: just re-install from the pre-Phase-1 state
# by checking out that commit and uv tool install --reinstall.
```

The legacy JSON paths in Chunks E handlers are preserved as shadow writes, so the old code path still works if you revert the binary.

---

## Known issues to fix in Phase 4 / Chunk H

1. **Migration script bug** — `scripts/migrate_to_sqlite.py` uses `thread_bindings`' outer user_id as `group_id` instead of looking up the real chat_id via `group_chat_ids`. Patched in DB for Peter's live setup via a one-line SQL UPDATE. Fresh installs on new machines will hit this. Fix: cross-reference `group_chat_ids[f"{user_id}:{topic_id}"]` during migration.
2. **Topic 1 (General) should be filtered** from `orphan_topic` classification — it's the forum root, always present, never bound.
3. **systemd `ExecStartPre=claude-hub heal`** is harmless now (heal is a no-op stub) but the line is misleading. Low priority: clean up on next systemd edit.
4. **Plaintext sudo password** `orchard32` in `~/ccgram-dashboard/health-check.sh` — rotate and move to env var.

---

## Legacy file retirement timeline

Shadow writes to `session_map.json`, `state.json`, `target-state.json`,
`monitor_state.json` remain in place for one release cycle after Phase 4
ships, for rollback safety.

**Status after Chunk H:**

- Chunk H shipped docs (`ARCHITECTURE.md`), migration-script `group_id`
  fix, reconcile topic-1 filter, integration test, and TODO markers on
  the three startup loaders.
- The read-flip itself (startup loaders preferring DB, falling back to
  JSON with a WARNING) was **deferred** — the file-by-file refactor
  exceeded the Opus small-edit threshold and a delegated Sonnet run hit
  a budget guardrail. Follow-up task below.

**Removal criteria:**

- Phase 4 (Chunk H) follow-up: `_load_state` / `load_session_map` /
  `MonitorState.load` refactored to DB-first, JSON-fallback with
  WARNING. (See TODO comments at `session.py`, `session_map.py`,
  `monitor_state.py`.)
- Daemon runs 7 consecutive days with:
  - no watchdog restarts,
  - no reconcile `manual_review` issues,
  - no `"falling back to legacy ... JSON"` WARNINGs in logs.

Once both criteria hold, open a follow-up PR to delete the JSON write
paths in:

- `src/ccgram/session.py` (`_save_state`, `StatePersistence` wiring)
- `src/ccgram/session_map.py` (`save_session_map`)
- `src/ccgram/monitor_state.py` (`save_monitor_state`)
- `src/ccgram/hook.py` (`session_map.json` update on SessionStart)

Downstream CLI/debug utilities that currently read these files
(`doctor_cmd.py`, `msg_cmd.py`, `msg_discovery.py`, `status_cmd.py`)
should migrate to `store.*` lookups in the same PR.

