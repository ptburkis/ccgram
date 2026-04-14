# CCGram Architecture

CCGram is a bridge between Telegram and AI agent CLIs (Claude Code, Codex, Gemini) running inside tmux windows. A Telegram message from Peter becomes a tmux send-keys. A Claude response written to a JSONL transcript gets tailed and sent back to the right Telegram topic. This document captures the message flow and the hard-won invariants.

**Branch:** `peter-fixes`. **Date of last major fixes:** 2026-04-14.

---

## 1. Overview

```
Telegram (topics/threads)
    │
    │  inbound: user sends message
    ▼
CCGram bot (python-telegram-bot)
    │
    ├─ handle_text_message
    │     └─ maybe_refresh_session_map (auto-heal)
    │           └─ send_to_window → tmux send-keys → Claude TUI
    │
    │  outbound: Claude writes response
    ▼
JSONL transcript (~/.claude/projects/<slug>/<uuid>.jsonl)
    │
    └─ session_monitor: tails file with byte offset
          └─ sends assistant turns to Telegram topic (thread_id)
```

**Outbound path**: Claude writes structured JSONL lines to its transcript. `session_monitor.py` polls each tracked session file, reads new bytes since `last_byte_offset` (persisted in `monitor_state.json`), parses assistant turns, and sends them to the Telegram topic bound to that window.

**Inbound path**: A Telegram message arrives at the bot. `handle_text_message` in `src/ccgram/handlers/text_handler.py` looks up the window ID from `state.json` thread bindings, calls `maybe_refresh_session_map` to auto-heal stale entries, then calls `send_to_window` → `tmux_manager.send_keys` → tmux send-keys into the agent's TUI.

---

## 2. The Compound Key Chain

Every Telegram message flows along this chain. Any broken link drops the message silently.

```
Topic (Telegram thread_id)
  │  state.json → thread_bindings[user_id][thread_id] = "@N"
  ▼
Window ID (@N, tmux unique window id)
  │  tmux display-message: pane_tty, pane_current_command
  ▼
Process (claude/codex PID attached to tty)
  │  session_map.json[session:@N] = {session_id, transcript_path, ...}
  ▼
Session (session_id UUID + transcript_path)
  │  hook marker in JSONL bytes: "tmux key=ccgram:@N, window_name=W, session_id=<stem>"
  ▼
JSONL file (source of truth for Claude's output)
  │  monitor_state.json[transcript_path].last_byte_offset
  ▼
Send to Telegram (threaded reply to thread_id)
```

**Files and owners:**

| File | Owner | Content |
|------|-------|---------|
| `~/.ccgram/state.json` | CCGram | `thread_bindings`: topic → window ID |
| `~/.ccgram/session_map.json` | CCGram + hook.py | window ID → session UUID + transcript path |
| `~/.ccgram/monitor_state.json` | `session_monitor.py` | per-transcript byte offset |
| `~/.claude/projects/<slug>/<uuid>.jsonl` | Claude Code | full conversation transcript |

**How each link breaks:**

- **Topic → window**: stale `state.json` after a tmux window is killed and recreated (new `@N` ID).
- **Window → process**: window exists but process died or restarted without CCGram knowing.
- **Process → session**: hook fired but `session_map.json` write lost (disk error, race); or Claude rotated sessions via `/compact` and the hook didn't fire cleanly.
- **Session → JSONL**: JSONL path in `session_map.json` points at an old transcript; new one exists but isn't tracked.
- **JSONL → Telegram**: `monitor_state.json` byte offset is wrong (reset to 0 triggers replay backlog; offset past EOF means new content is skipped).

---

## 3. THE SHARED-CWD INVARIANT

This is the most important section. Read it before touching any code that resolves jsonl → window.

**The problem:** Multiple tmux windows can share a cwd. For example:
- `@4 james-claude-hub` running Claude in `/home/peter/projects/james`
- `@19 james` running Claude in `/home/peter/projects/james`

Both produce JSONL files in the same Claude project directory: `~/.claude/projects/-home-peter-projects-james/`.

Any code that picks "the newest JSONL in a project directory" and assigns it to a window **will wire both windows to the same session**. Symptoms: output from one session appears in the wrong Telegram topic; the real session goes silent.

**The filter:** a JSONL file `<stem>.jsonl` belongs to window `@X` with `window_name` W if and only if its bytes contain:

```
tmux key=ccgram:@X, window_name=W, session_id=<stem>
```

where `<stem>` is the jsonl's own filename without `.`.

This marker is written by `src/ccgram/hook.py` during the `SessionStart` hook event. It is authoritative — Claude writes it, CCGram reads it.

**Implementation:** The check lives in `_jsonl_belongs_to_window()` in `src/ccgram/session_autoheal.py` (lines 55–74). Import and call it. Never use mtime alone.

**The three places this invariant applies (bit us in each one on Apr 14 2026):**

1. **`src/ccgram/providers/codex.py` — `discover_transcript` CWD fallback**
   Fixed in commit `9208593`. Codex has no hook, so it uses cwd-based fallback discovery. The fix: among cwd-matching candidates, only assign the one not already claimed by another window.

2. **`claude-hub doctor --fix` — BEHIND detection**
   Fixed in commit `ba6e4d3`. Doctor's `--fix` path was picking the newest JSONL in a project dir by mtime alone. Now it calls `_jsonl_belongs_to_window()` before assigning.

3. **`src/ccgram/session_autoheal.py` — `_find_newer_jsonl_sync`**
   Fixed in commit `5de2753`. The auto-heal path had the same flaw. Now filters by hook marker; if no JSONL matches, returns `None` rather than guessing.

**Rule:** If you add any new code that picks a JSONL for a window, this filter is non-negotiable. We were bit three times in one day. Import `_jsonl_belongs_to_window` from `session_autoheal.py` or replicate the marker check exactly.

---

## 4. The CWD Slug Encoding

Claude's projects directory names are derived from the cwd using this encoding:

```python
slug = "-" + cwd.lstrip("/").replace("/", "-").replace("_", "-")
```

Both `/` **and** `_` become `-`. The leading `/` becomes `-` as well (via `lstrip` + first replace).

Examples:
- `/home/peter/projects/james` → `-home-peter-projects-james`
- `/home/peter/projects/bulugo_lead_gen` → `-home-peter-projects-bulugo-lead-gen`

**The gotcha:** forgetting the `replace("_", "-")` step. Any code computing the slug that only replaces `/` will produce the wrong directory name for projects with underscores in their path. The directory won't be found; the window will be silently skipped.

This burned us in commit `79e9927` — doctor's BEHIND check skipped all bulugo windows because the underscore conversion was missing.

**Canonical implementation:** `_cwd_to_project_slug()` in `src/ccgram/session_autoheal.py` (line 39). The same logic exists inline in `src/ccgram/session_monitor.py` at line 1043. If you need this conversion, use one of these; don't write it from memory.

---

## 5. Auto-Heal on Inbound

**File:** `src/ccgram/session_autoheal.py`

**Entry point:** `maybe_refresh_session_map(window_id)` — async, called on every inbound message just before `send_to_window`.

**What it does:**
1. Looks up the window's current `session_map` entry and cwd.
2. Computes the Claude project directory from the cwd using `_cwd_to_project_slug`.
3. Scans the project directory for JSONL files that carry the hook marker for this window (`_jsonl_belongs_to_window` filter).
4. If a newer JSONL exists (mtime > current transcript's mtime): updates `session_map.json` and `monitor_state.json` atomically. The new session is tracked starting at the current file end — this skips any existing content and avoids a replay backlog.
5. Refreshes the in-memory `session_manager` state.
6. Silent on error — never raises, never blocks message delivery.

**Why this matters:** Claude rotates sessions on `/compact` or restart. Without auto-heal, the session_map keeps pointing at the old (finished) JSONL until someone runs `claude-hub doctor --fix` and restarts CCGram. With auto-heal, the next inbound message heals it automatically.

**Constraint:** Only acts on Claude windows. Hookless providers (Codex, Gemini) discover their own sessions.

**Does not conflict with** `session_monitor.py`'s reconciler: that reconciler handles windows with a _missing_ `session_id`; auto-heal handles windows with a _stale_ (no-longer-valid) `session_id`.

---

## 6. Debug Timeline (Observability)

**File:** `src/ccgram/debug_timeline.py`

Always-on structured event recording. Every significant event is appended to:

```
~/.ccgram/debug/timeline-YYYY-MM-DD.jsonl
```

Per-window terminal output is captured via tmux `pipe-pane` to:

```
~/.ccgram/debug/terminal-@N.log
```

**Event types:**

| Type | Meaning |
|------|---------|
| `send.attempt` | About to send a message to a window |
| `send.success` | Message reached tmux |
| `send.error` | tmux send-keys failed |
| `transcript` | New assistant turn read from JSONL |
| `hook` | Hook event received (SessionStart, etc.) |
| `injection` | Inbound message injected into window |
| `system.startup` | CCGram started |

**Retention:** 2-day rolling (older files deleted on startup). Approximately 40 MB/day under normal load.

**Disable:** Set `CCGRAM_DEBUG_ENABLED=0` in environment.

**Note:** `send.*` events currently have an empty `window_id` field — context isn't threaded through from `msg_telegram.py`. Use `--include-global` in `claude-hub debug` to see them.

---

## 7. Tool Reference

### claude-hub doctor

```
claude-hub doctor [--verbose] [--fix] [--json]
```

Audits the full compound key chain for every live window. Reports status per window:

- **OK** — chain intact, transcript exists, byte offset sane.
- **STALE** — session_map entry points at a non-existent transcript.
- **BEHIND** — a newer JSONL exists in the project dir than what's tracked (session rotated, hook missed).
- **MISSING** — live window has no session_map entry.
- **UNTRACKED** — project dir exists with JSONL but no live window.

`--fix` auto-heals STALE and BEHIND entries. MISSING/UNTRACKED are informational only.

`--verbose` shows the full chain for each window including PID, transcript path, and byte offset.

### claude-hub debug

```
claude-hub debug <window> [--tail N] [--since 1h] [--watch] [--type send] [--include-global] [--json]
```

Displays the timeline event log for a window. `--include-global` shows events without a window_id (covers `send.*` events). `--watch` streams live.

### claude-hub reconcile

```
claude-hub reconcile <window> [--last 10m]
```

Compares the transcript (what Claude wrote) against send events (what was actually sent to Telegram). Surfaces gaps. Note: text is reformatted between transcript and Telegram (markdown, 🎩 prefix), so the matcher uses prefix matching and can produce false positives. Verify gaps with `claude-hub debug --type send` before using replay.

### claude-hub replay

```
claude-hub replay <window> [--last N] [--confirm] [--prefix PFX]
```

Re-sends the last N assistant turns from the transcript to Telegram. Dry-run by default; `--confirm` to actually send. Prefixes messages with `[replay]`. Claude-only — Codex windows error out.

---

## 8. Never-Again Gotchas (Apr 14 2026)

These cost half a day. Future readers: don't repeat them.

### 1. Missing import crashes every inbound message silently (commit `86c9748`)

When Phase 2 (observability) wired `get_timeline().log(...)` calls into `tmux_manager.py`, the `from .debug_timeline import get_timeline` import was missing. Every Telegram → tmux injection crashed with `NameError: name 'get_timeline' is not defined`. The error surfaced only as `[error] Unhandled bot error` in CCGram stdout — no Telegram alert, no visible breakage on the user side. This ran silently for ~3 hours.

**Rule:** When adding a call to a module-level function, grep for all call sites and verify imports exist. Sonnet agents routinely write the call and forget the import. The import discipline section of `DEV-STANDARDS.md` covers this, but it bears repeating here.

### 2. Unhandled bot errors are silent

CCGram's `python-telegram-bot` error handler catches exceptions at the framework level and logs them at `ERROR` level to stdout only. There is no Telegram notification. If inbound messages stop reaching tmux, the first diagnostic step is:

```bash
grep "Unhandled bot error" /path/to/ccgram-stdout.log
```

That log line will contain the exception type and stack trace, which is the real root cause.

### 3. CWD slug encoding misses underscores

See Section 4. The fix is `replace("_", "-")` after `replace("/", "-")`. Without it, any project with an underscore in its path is silently ignored.

### 4. Shared-cwd bleed

See Section 3. The fix is `_jsonl_belongs_to_window()`. Without it, windows sharing a cwd steal each other's sessions.

---

## 9. Debug Workflow

Use this when a topic goes quiet or messages stop flowing.

**Step 1 — Identify the broken link:**
```bash
claude-hub doctor --verbose
```
Red `✗` entries show exactly which step in the compound key chain failed.

**Step 2 — Event timeline drill-in:**
```bash
claude-hub debug <window> --tail 30 --include-global
```
Look for `send.error` entries or gaps between `injection` and `send.success`.

**Step 3 — If session_map is wrong:**
```bash
claude-hub doctor --fix
claude-hub doctor --verbose   # verify
```
Then restart CCGram if the fix didn't take effect automatically.

**Step 4 — If messages are genuinely missing (sent by Claude, never reached Telegram):**
```bash
claude-hub reconcile <window> --last 10m
claude-hub replay <window> --last 3 --confirm
```

**Step 5 — If "Unhandled bot error" appears in CCGram log:**
Check the stack trace. Almost certainly a `NameError` (missing import) or similar crash in the inbound handler. Fix the code, reinstall CCGram, restart.

**Step 6 — If outbound is silent but inbound works:**
Likely the session_map points at a dead JSONL. Auto-heal should catch this on the next inbound message. Check logs for:
```
auto-healed session_map for @N
```
If auto-heal isn't firing, run `doctor --fix` manually.

---

## File Reference

| Path | Purpose |
|------|---------|
| `src/ccgram/handlers/text_handler.py` | Inbound: `handle_text_message`, calls auto-heal |
| `src/ccgram/session_autoheal.py` | Auto-heal: `maybe_refresh_session_map`, `_jsonl_belongs_to_window`, `_cwd_to_project_slug` |
| `src/ccgram/session_monitor.py` | Outbound: tails JSONL, sends to Telegram; cwd slug encoding at line 1043 |
| `src/ccgram/hook.py` | Writes `session_map.json` on `SessionStart` hook; emits the hook marker to structured log |
| `src/ccgram/debug_timeline.py` | Timeline logger singleton (`get_timeline()`) |
| `src/ccgram/doctor_cmd.py` | `claude-hub doctor` implementation |
| `src/ccgram/providers/codex.py` | Codex transcript discovery with cwd fallback + shared-cwd de-dup |
| `~/.ccgram/state.json` | Runtime: topic → window bindings |
| `~/.ccgram/session_map.json` | Runtime: window → session UUID + transcript path |
| `~/.ccgram/monitor_state.json` | Runtime: per-transcript byte offsets |
| `~/.ccgram/debug/timeline-YYYY-MM-DD.jsonl` | Debug: event timeline |
| `~/.ccgram/debug/terminal-@N.log` | Debug: raw tmux pane output |
