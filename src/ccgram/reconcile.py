"""Reconcile CCGram state across tmux, Telegram MTProto, Claude transcripts, and the DB.

Reads from four authorities and classifies divergences into auto-fixable and
manual-review categories.  The result is a :class:`ReconcileReport` that can
be displayed as a human-readable table or serialised to JSON.

Authority order (verbatim from design doc):

| Field | Authority |
|---|---|
| Window exists? | tmux (TmuxManager.list_windows) |
| Topic exists + current title | MTProto (MTProtoClient.list_forum_topics) |
| Session identity (which JSONL is real) | Claude Code transcripts — by CCGRAM_SESSION_ID marker if present, else cwd+mtime heuristic with loud warning |
| User-chosen bindings | DB topic_bindings |

Issue kinds:
- ``title_drift``       — DB topic_title != MTProto live title  (manual_review until Chunk F)
- ``orphan_topic``      — MTProto has topic, no DB binding       (manual_review)
- ``orphan_binding``    — DB binding with gone/retired session   (manual_review)
- ``orphan_window``     — tmux window with no DB session row     (manual_review)
- ``ambiguous_session`` — two windows claim same session_id      (manual_review)
- ``duplicate_binding`` — two topic_bindings share session_id    (manual_review)
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ccgram import store
from ccgram.mtproto_client import ForumTopic
from ccgram.utils import ccgram_dir

logger = structlog.get_logger(__name__)

_KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "title_drift",
        "orphan_topic",
        "orphan_binding",
        "orphan_window",
        "ambiguous_session",
        "duplicate_binding",
    }
)


# ---- Public data structures --------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileIssue:
    """A single discrepancy between authorities, with its severity and optional fix."""

    kind: str
    severity: str
    detail: str
    suggested_fix: dict | None = None


@dataclass
class ReconcileReport:
    """Result of a reconcile run.

    Attributes:
        issues:       All detected issues, sorted deterministically by (kind, detail).
        applied:      Issues that were successfully auto-fixed.  Empty when dry_run=True.
        group_id:     The Telegram group that was reconciled.
        dry_run:      True when no writes were made.
        generated_at: Unix epoch second when the report was produced.
    """

    issues: list[ReconcileIssue]
    applied: list[ReconcileIssue]
    group_id: int
    dry_run: bool
    generated_at: int

    @property
    def summary(self) -> dict[str, int]:
        """Issue counts by kind.  Always includes all known kinds (zero-filled)."""
        counts: dict[str, int] = {k: 0 for k in sorted(_KNOWN_KINDS)}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        return counts

    @property
    def auto_fix_count(self) -> int:
        """Number of auto-fixable issues."""
        return sum(1 for i in self.issues if i.severity == "auto_fix")

    @property
    def manual_review_count(self) -> int:
        """Number of issues requiring manual review."""
        return sum(1 for i in self.issues if i.severity == "manual_review")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this report."""

        def _issue_to_dict(i: ReconcileIssue) -> dict[str, Any]:
            return {
                "kind": i.kind,
                "severity": i.severity,
                "detail": i.detail,
                "suggested_fix": i.suggested_fix,
            }

        return {
            "group_id": self.group_id,
            "dry_run": self.dry_run,
            "generated_at": self.generated_at,
            "summary": self.summary,
            "auto_fix_count": self.auto_fix_count,
            "manual_review_count": self.manual_review_count,
            "issues": [_issue_to_dict(i) for i in self.issues],
            "applied": [_issue_to_dict(i) for i in self.applied],
        }


# ---- Default fetchers --------------------------------------------------------


async def _default_tmux_fetcher() -> list[tuple[str, str, str]]:
    from ccgram.tmux_manager import TmuxManager

    manager = TmuxManager()
    windows = await manager.list_windows()
    return [(w.window_id, w.window_name, w.cwd) for w in windows]


async def _default_topic_fetcher(group_id: int) -> list[ForumTopic]:
    from ccgram.mtproto_client import MTProtoClient

    client = MTProtoClient()
    async with client:
        return await client.list_forum_topics(group_id)


async def _default_session_identity_fetcher() -> dict[str, str]:
    """Read session_map.json and return window_id -> session_id mapping.

    Keys in session_map.json look like ``"ccgram:@5"``; the prefix is stripped
    so the returned dict uses bare window IDs (``"@5"``).

    TODO: Once Chunk F lands (transcript watcher keyed on CCGRAM_SESSION_ID),
    replace this implementation with a scan of running tmux panes' environments
    for the CCGRAM_SESSION_ID marker — more authoritative than the JSON file.
    """
    path = ccgram_dir() / "session_map.json"
    if not path.exists():
        return {}
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("reconcile.session_map_read_error", error=str(exc))
        return {}
    result: dict[str, str] = {}
    for key, val in raw.items():
        # Key format is "session_name:@N" — take only the part after the last ":"
        window_id = key.rsplit(":", 1)[-1] if ":" in key else key
        if isinstance(val, dict) and "session_id" in val:
            result[window_id] = val["session_id"]
    return result


# ---- Issue checkers ----------------------------------------------------------


def _check_title_drift(
    bindings: list[store.TopicBinding],
    live_topics_by_id: dict[int, ForumTopic],
    group_id: int,
) -> list[ReconcileIssue]:
    issues: list[ReconcileIssue] = []
    for binding in bindings:
        if binding.group_id != group_id:
            continue
        topic = live_topics_by_id.get(binding.topic_id)
        if topic is None:
            continue
        if binding.topic_title != topic.title:
            issues.append(
                ReconcileIssue(
                    kind="title_drift",
                    severity="manual_review",
                    detail=(
                        f"topic {binding.topic_id}: DB title {binding.topic_title!r}"
                        f" != live title {topic.title!r}"
                        " (could be a rename OR a wrong-session binding — "
                        "cannot disambiguate until session_id markers land in Chunk F)"
                    ),
                    suggested_fix={
                        "action": "update_topic_title",
                        "group_id": group_id,
                        "topic_id": binding.topic_id,
                        "new_title": topic.title,
                    },
                )
            )
    return issues


def _check_orphan_topics(
    bindings: list[store.TopicBinding],
    live_topics: list[ForumTopic],
    group_id: int,
) -> list[ReconcileIssue]:
    bound_ids = {b.topic_id for b in bindings if b.group_id == group_id}
    issues: list[ReconcileIssue] = []
    for topic in live_topics:
        if topic.topic_id not in bound_ids:
            issues.append(
                ReconcileIssue(
                    kind="orphan_topic",
                    severity="manual_review",
                    detail=(
                        f"topic {topic.topic_id} ({topic.title!r}) has no DB binding"
                    ),
                    suggested_fix=None,
                )
            )
    return issues


def _check_orphan_bindings(
    bindings: list[store.TopicBinding],
    live_topics_by_id: dict[int, ForumTopic],
    sessions_by_id: dict[str, store.Session],
    group_id: int,
) -> list[ReconcileIssue]:
    issues: list[ReconcileIssue] = []
    for binding in bindings:
        if binding.group_id != group_id:
            continue
        if binding.topic_id not in live_topics_by_id:
            issues.append(
                ReconcileIssue(
                    kind="orphan_binding",
                    severity="manual_review",
                    detail=(
                        f"binding for topic {binding.topic_id}"
                        f" ({binding.topic_title!r}): topic not found in MTProto"
                    ),
                    suggested_fix={
                        "action": "delete_binding",
                        "group_id": group_id,
                        "topic_id": binding.topic_id,
                    },
                )
            )
        elif binding.session_id not in sessions_by_id:
            issues.append(
                ReconcileIssue(
                    kind="orphan_binding",
                    severity="manual_review",
                    detail=(
                        f"binding for topic {binding.topic_id}"
                        f" ({binding.topic_title!r}): session"
                        f" {binding.session_id!r} not found in DB"
                    ),
                    suggested_fix={
                        "action": "delete_binding",
                        "group_id": group_id,
                        "topic_id": binding.topic_id,
                    },
                )
            )
        elif sessions_by_id[binding.session_id].status == "retired":
            issues.append(
                ReconcileIssue(
                    kind="orphan_binding",
                    severity="manual_review",
                    detail=(
                        f"binding for topic {binding.topic_id}"
                        f" ({binding.topic_title!r}): session"
                        f" {binding.session_id!r} is retired"
                    ),
                    suggested_fix={
                        "action": "delete_binding",
                        "group_id": group_id,
                        "topic_id": binding.topic_id,
                    },
                )
            )
    return issues


def _check_orphan_windows(
    windows: list[tuple[str, str, str]],
    sessions: list[store.Session],
) -> list[ReconcileIssue]:
    session_window_ids = {s.window_id for s in sessions if s.window_id}
    issues: list[ReconcileIssue] = []
    for window_id, window_name, cwd in windows:
        if window_id not in session_window_ids:
            issues.append(
                ReconcileIssue(
                    kind="orphan_window",
                    severity="manual_review",
                    detail=(
                        f"tmux window {window_id} ({window_name!r}, cwd={cwd!r})"
                        " has no session row in DB"
                    ),
                    suggested_fix=None,
                )
            )
    return issues


def _check_ambiguous_sessions(
    identity_map: dict[str, str],
) -> list[ReconcileIssue]:
    """Detect multiple windows claiming the same session_id."""
    reverse: dict[str, list[str]] = defaultdict(list)
    for window_id, session_id in identity_map.items():
        reverse[session_id].append(window_id)
    issues: list[ReconcileIssue] = []
    for session_id, window_ids in reverse.items():
        if len(window_ids) > 1:
            windows_str = ", ".join(sorted(window_ids))
            issues.append(
                ReconcileIssue(
                    kind="ambiguous_session",
                    severity="manual_review",
                    detail=(
                        f"session {session_id!r} claimed by multiple windows:"
                        f" {windows_str}"
                    ),
                    suggested_fix=None,
                )
            )
    return issues


def _check_duplicate_bindings(
    bindings: list[store.TopicBinding],
) -> list[ReconcileIssue]:
    """Defensive check for duplicate session_id in topic_bindings.

    The ``UNIQUE(session_id)`` constraint in the schema prevents this under
    normal operation.  This check emits loudly if the invariant is ever
    violated (e.g. via direct SQL, migration artefacts, or schema recreation).
    """
    by_session: dict[str, list[store.TopicBinding]] = defaultdict(list)
    for binding in bindings:
        by_session[binding.session_id].append(binding)
    issues: list[ReconcileIssue] = []
    for session_id, dupes in by_session.items():
        if len(dupes) > 1:
            topic_ids = ", ".join(str(b.topic_id) for b in dupes)
            issues.append(
                ReconcileIssue(
                    kind="duplicate_binding",
                    severity="manual_review",
                    detail=(
                        f"session {session_id!r} appears in multiple bindings"
                        f" (topic_ids: {topic_ids}) — schema UNIQUE violation"
                    ),
                    suggested_fix=None,
                )
            )
    return issues


# ---- Apply logic -------------------------------------------------------------


def _apply_issue(issue: ReconcileIssue, db_path: Path | None) -> bool:
    """Apply a single auto_fix issue to the DB.  Returns True on success."""
    if issue.kind == "title_drift":
        fix = issue.suggested_fix
        if not fix:
            return False
        try:
            with store.connect(db_path) as conn:
                existing = store.get_topic_binding(conn, fix["group_id"], fix["topic_id"])
                if existing is None:
                    logger.warning(
                        "reconcile.apply.binding_gone",
                        topic_id=fix["topic_id"],
                    )
                    return False
                store.upsert_topic_binding(
                    conn,
                    group_id=existing.group_id,
                    topic_id=existing.topic_id,
                    session_id=existing.session_id,
                    topic_title=fix["new_title"],
                    bound_at=existing.bound_at,
                )
        except sqlite3.Error as exc:
            logger.error(
                "reconcile.apply.db_error",
                kind=issue.kind,
                error=str(exc),
            )
            return False
        logger.info(
            "reconcile.apply.title_drift",
            topic_id=fix["topic_id"],
            new_title=fix["new_title"],
        )
        return True
    return False


# ---- Public API --------------------------------------------------------------


async def reconcile(
    *,
    group_id: int,
    dry_run: bool = True,
    db_path: Path | None = None,
    tmux_fetcher: Callable[[], Awaitable[list[tuple[str, str, str]]]] | None = None,
    topic_fetcher: Callable[[int], Awaitable[list[ForumTopic]]] | None = None,
    session_identity_fetcher: Callable[[], Awaitable[dict[str, str]]] | None = None,
) -> ReconcileReport:
    """Reconcile CCGram state and return a classified report.

    Pulls from four authorities (tmux, MTProto, transcript session markers, DB)
    and classifies each discrepancy as ``auto_fix`` or ``manual_review``.

    Args:
        group_id:                  Telegram group ID whose topic bindings to check.
        dry_run:                   When True (default), computes report but writes
                                   nothing to the DB.  ``report.applied`` is empty.
        db_path:                   Path to the SQLite DB.  Defaults to
                                   ``store.db_path()``.
        tmux_fetcher:              Async callable → ``[(window_id, name, cwd), ...]``.
                                   Defaults to ``TmuxManager().list_windows()``.
        topic_fetcher:             Async callable taking ``group_id`` → list of
                                   ``ForumTopic``.  Defaults to
                                   ``MTProtoClient().list_forum_topics()``.
        session_identity_fetcher:  Async callable → ``{window_id: session_id}``.
                                   Defaults to reading ``~/.ccgram/session_map.json``
                                   (strips ``"ccgram:"`` prefix from keys).

                                   TODO: Once Chunk F lands, replace with a
                                   CCGRAM_SESSION_ID env-marker scan across running
                                   tmux panes for a more authoritative source.

    Returns:
        :class:`ReconcileReport` with all detected issues and applied fixes.
        Issues are sorted deterministically by ``(kind, detail)``.
        Only ``auto_fix`` issues are ever applied; ``manual_review`` items are
        returned in ``report.issues`` unchanged and are never auto-applied.
    """
    _tmux_fetcher = tmux_fetcher or _default_tmux_fetcher
    _topic_fetcher = topic_fetcher or _default_topic_fetcher
    _identity_fetcher = session_identity_fetcher or _default_session_identity_fetcher

    windows = await _tmux_fetcher()
    live_topics = await _topic_fetcher(group_id)
    identity_map = await _identity_fetcher()

    live_topics_by_id: dict[int, ForumTopic] = {t.topic_id: t for t in live_topics}

    with store.connect(db_path) as conn:
        bindings = store.list_topic_bindings(conn, group_id=group_id)
        sessions = store.list_sessions(conn)

    sessions_by_id: dict[str, store.Session] = {s.session_id: s for s in sessions}

    all_issues: list[ReconcileIssue] = []
    all_issues.extend(_check_title_drift(bindings, live_topics_by_id, group_id))
    all_issues.extend(_check_orphan_topics(bindings, live_topics, group_id))
    all_issues.extend(
        _check_orphan_bindings(bindings, live_topics_by_id, sessions_by_id, group_id)
    )
    all_issues.extend(_check_orphan_windows(windows, sessions))
    all_issues.extend(_check_ambiguous_sessions(identity_map))
    all_issues.extend(_check_duplicate_bindings(bindings))

    all_issues.sort(key=lambda i: (i.kind, i.detail))

    applied: list[ReconcileIssue] = []
    if not dry_run:
        for issue in all_issues:
            if issue.severity == "auto_fix" and _apply_issue(issue, db_path):
                applied.append(issue)

    return ReconcileReport(
        issues=all_issues,
        applied=applied,
        group_id=group_id,
        dry_run=dry_run,
        generated_at=int(time.time()),
    )
