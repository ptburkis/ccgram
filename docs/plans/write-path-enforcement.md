# Write-Path Enforcement Hook — Design

**Status:** Draft 2026-04-15  
**Branch:** `peter-fixes`  
**Author:** James  

---

## Problem

Agents running in project windows sometimes write files outside their own `cwd`:

1. A session in `~/projects/james` writes to `~/.openclaw/workspace/...`
2. An agent returns a local path (`/home/peter/projects/foo/bar.md`) in chat instead of the clawd hub URL (`https://clawd.tail483fa1.ts.net:8443/files/foo/bar.md`)

Both confuse the user and can corrupt other sessions' state.

---

## Approach 1 — PreToolUse Hook (BLOCK/WARN out-of-tree writes)

Claude Code supports lifecycle hooks via `.claude/settings.json`. A `PreToolUse` hook runs before every `Write`, `Edit`, or `NotebookEdit` call and can exit non-zero to block the tool or return a warning the agent sees.

### Hook script: `scripts/hooks/write-path-guard.sh`

```bash
#!/usr/bin/env bash
# PreToolUse hook — blocks writes outside the session cwd.
# Receives tool input JSON on stdin; reads CCGRAM_SESSION_CWD env var (or $PWD).

set -euo pipefail

SESSION_CWD="${CCGRAM_SESSION_CWD:-$PWD}"
TARGET_PATH=$(cat /dev/stdin | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('path','') or d.get('file_path',''))" 2>/dev/null || true)

if [[ -z "$TARGET_PATH" ]]; then exit 0; fi   # can't resolve — let it through

# Resolve to absolute
RESOLVED=$(realpath -m "$TARGET_PATH" 2>/dev/null || echo "$TARGET_PATH")

# Exceptions — always allowed
for EXCEPTION in "$HOME/.ccgram" "$HOME/.claude" "$SESSION_CWD/hub-shared"; do
    if [[ "$RESOLVED" == "$EXCEPTION"* ]]; then exit 0; fi
done

# Check against session cwd
if [[ "$RESOLVED" != "$SESSION_CWD"* ]]; then
    echo "BLOCKED: '$RESOLVED' is outside session cwd '$SESSION_CWD'." >&2
    echo "Write only to your project directory. Use the hub URL for file references." >&2
    exit 1
fi

exit 0
```

**How it integrates:**
- `exit 1` → Claude Code returns a tool error to the agent; the agent sees "write blocked" and adjusts
- `exit 0` → write proceeds normally

### Block vs Prompt recommendation

| Case | Action | Rationale |
|------|--------|-----------|
| Write to another project dir (`.openclaw/`, `~/projects/other/`) | **BLOCK** | Unambiguous mistake; no legitimate reason |
| Write to `~/.ccgram/*` or `~/.claude/*` (agent dotfiles) | **ALLOW** | Hook config, memory updates — valid |
| Write to `hub-shared/` (symlinked) | **ALLOW** | Peter explicitly shares this across sessions |
| Write to `/tmp/` | **ALLOW** | Scratch space, no state risk |
| Write to system config outside all the above | **PROMPT** (warn, don't block) | Ambiguous; let the agent decide consciously |

---

## Approach 2 — PostToolUse Hook (URL rewrite in output)

A `PostToolUse` hook intercepts the Write/Edit result and rewrites any `/home/peter/...` path references to the equivalent `https://clawd.tail483fa1.ts.net:8443/files/...` URL.

**Upside:** Users always get tappable links in Telegram.  
**Downside:** Doesn't prevent bad writes — only cosmetic for display. The agent's internal thinking still uses local paths. A follow-up `Read` call will still use the local path; no harm done but the inconsistency can confuse.

**Recommendation:** Implement PostToolUse URL rewrite as a *complement* to PreToolUse blocking, not a replacement.

---

## Configuration — Where Does the Hook Live?

### Option 1: Global `~/.claude/settings.json`
Affects every Claude Code session on the machine. Simplest to deploy but coarse — can't be tuned per-project.

### Option 2: Per-project `.claude/settings.json`
Lives in the project root. Only activates when Claude Code CWD matches. Clean isolation; can carry project-specific exceptions.

### Option 3: Bootstrapped via `create_session()` at spawn time
`session_lifecycle.create_session()` writes `.claude/settings.json` into the target project if it doesn't already contain the hook entry. Ensures the hook is present for every newly spawned session without manual setup.

### Recommendation: Option 2 + 3

- **Option 2** (per-project) is the right scope: the hook is meaningful only in the context of a specific session's cwd.
- **Option 3** (bootstrapped at spawn) ensures every session gets it automatically. New projects don't need manual config.
- Together: if `.claude/settings.json` already exists in the project and contains the hook, `create_session()` skips the write (idempotent).

Option 1 (global) is too blunt — it would fire for James sessions that legitimately manage other project files (e.g. the master james session).

---

## Hook config shape (`.claude/settings.json`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash $HOME/.ccgram/hooks/write-path-guard.sh"
          }
        ]
      }
    ]
  }
}
```

The hook script lives in `~/.ccgram/hooks/` (a central location, not per-project) so updates deploy once. The per-project `.claude/settings.json` just references it.

---

## Shared Rule — WAY-WE-WORK.md

A section should be added to `hub-shared/WAY-WE-WORK.md`:

```markdown
## Write-Path Rule
- **Agents write only to their own cwd.** Never write to another project's directory.
- **File references in chat use the clawd hub URL format:**
  `https://clawd.tail483fa1.ts.net:8443/files/<path-relative-to-projects-root>/`
  not local paths like `/home/peter/...`.
- Exceptions: `~/.ccgram/*`, `~/.claude/*`, `hub-shared/` (symlink), `/tmp/`.
- Enforcement: PreToolUse hook auto-bootstrapped by `create_session()`.
```

---

## Exceptions Summary

| Path | Allowed | Reason |
|------|---------|--------|
| `$SESSION_CWD/**` | Always | Own project tree |
| `~/.ccgram/**` | Always | CCGram agent config & memory |
| `~/.claude/**` | Always | Claude Code settings, memory |
| `hub-shared/` (symlink) | Always | Shared hub resources |
| `/tmp/**` | Always | Scratch, non-persistent |
| Another `~/projects/<X>/**` | BLOCK | Cross-project contamination |
| `~/.openclaw/**` | BLOCK | CCGram system state |
| System files outside above | PROMPT | Ambiguous — warn and ask |

---

## Implementation Checklist (not yet actioned)

- [ ] Write `~/.ccgram/hooks/write-path-guard.sh`
- [ ] Add PostToolUse URL-rewrite hook (`write-path-url-rewrite.sh`)
- [ ] Update `session_lifecycle.create_session()` to bootstrap `.claude/settings.json`
- [ ] Add write-path rule section to `hub-shared/WAY-WE-WORK.md`
- [ ] Test: spawn a session in `/tmp/test-project`, attempt write to `/tmp/other-project/` → verify block
- [ ] Test: write inside cwd → verify passthrough
