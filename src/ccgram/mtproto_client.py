"""Read-only MTProto user client for Telegram forum topic discovery.

Wraps Telethon to provide a thin, read-only view of Telegram Forum topics.
Intended for reconciliation: reading authoritative topic IDs and titles from
Telegram without going through the Bot API (which can't list all topics).

Usage::

    client = MTProtoClient()          # resolves credentials from env / .env
    async with client:                # calls connect(); disconnects on exit
        topics = await client.list_forum_topics(group_id=-1001234567890)
        for t in topics:
            print(t.topic_id, t.title)

First-time login (interactive, run once)::

    client = MTProtoClient()
    await client.login(phone="+44...")  # prompts for code if needed

Credentials: ``TELEGRAM_API_ID`` and ``TELEGRAM_API_HASH`` (or ``TG_API_ID`` /
``TG_API_HASH``) in environment or ``~/.ccgram/.env`` / ``~/.ccgram/mtproto.env``.
Get them at https://my.telegram.org/apps.

Read-only guarantee: this module exposes no ``send_*``, ``create_*``,
``edit_*``, or ``delete_*`` methods. All writes go through the Bot API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import structlog

from ccgram.utils import ccgram_dir

if TYPE_CHECKING:
    pass  # telethon type stubs would go here if available

logger = structlog.get_logger(__name__)


class TopicMessage(NamedTuple):
    """A single message read from a Telegram Forum topic via MTProto."""

    message_id: int
    date: datetime
    from_bot: bool
    text: str
    reply_to_message_id: int | None


@dataclass(frozen=True, slots=True)
class ForumTopic:
    """A single Telegram Forum topic, read from MTProto.

    Fields:
        topic_id: The stable integer ID used by both MTProto and Bot API as
            ``message_thread_id``.  Never changes for the life of the topic.
        title: Human-readable topic title as it appears in Telegram.
        top_message_id: The message_id of the first (pinned-top) message in the
            topic.  Changes when the topic is re-created.
        is_closed: True if the topic is closed (replies disabled).
        is_hidden: True if the topic is hidden from the topic list.
        created_date: UTC datetime the topic was created, or None if the
            Telegram server did not include a date in the response.
        raw: Non-stable extras from the raw Telegram object.  Excluded from
            equality comparison and repr to keep output readable.
    """

    topic_id: int
    title: str
    top_message_id: int
    is_closed: bool
    is_hidden: bool
    created_date: datetime | None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


class MTProtoCredentialsError(RuntimeError):
    """Raised when TELEGRAM_API_ID / TELEGRAM_API_HASH are missing or invalid.

    Obtain credentials at https://my.telegram.org/apps and set them in
    ``~/.ccgram/.env`` (or ``~/.ccgram/mtproto.env``) as::

        TELEGRAM_API_ID=12345678
        TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
    """


class MTProtoSessionMissingError(RuntimeError):
    """Raised when a valid session file is required but absent or expired.

    Run ``MTProtoClient().login()`` once to create the session file.
    """


def session_path() -> Path:
    """Return the path of the MTProto session file (includes .session suffix)."""
    return ccgram_dir() / "mtproto.session"


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser — no external dependency."""
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _resolve_api_credentials() -> tuple[int, str]:
    """Resolve API ID and hash from env vars or .env files.

    Resolution order:
    1. Environment variables (``TELEGRAM_API_ID`` / ``TG_API_ID``, same for hash)
    2. ``~/.ccgram/.env``
    3. ``~/.ccgram/mtproto.env``

    Raises:
        MTProtoCredentialsError: if credentials are absent or api_id is not an integer.
    """
    _hint = (
        "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the environment or in "
        "~/.ccgram/.env (or ~/.ccgram/mtproto.env). "
        "Get them at https://my.telegram.org/apps"
    )

    def _find(env_primary: str, env_alias: str, sources: list[dict[str, str]]) -> str:
        val = os.environ.get(env_primary) or os.environ.get(env_alias)
        if val:
            return val
        for src in sources:
            val = src.get(env_primary) or src.get(env_alias)
            if val:
                return val
        return ""

    config_dir = ccgram_dir()
    env_sources = [
        _load_env_file(config_dir / ".env"),
        _load_env_file(config_dir / "mtproto.env"),
    ]

    raw_id = _find("TELEGRAM_API_ID", "TG_API_ID", env_sources)
    raw_hash = _find("TELEGRAM_API_HASH", "TG_API_HASH", env_sources)

    if not raw_id or not raw_hash:
        raise MTProtoCredentialsError(f"Missing Telegram API credentials. {_hint}")

    try:
        api_id = int(raw_id)
    except ValueError as exc:
        raise MTProtoCredentialsError(
            f"TELEGRAM_API_ID must be an integer, got {raw_id!r}. {_hint}"
        ) from exc

    return api_id, raw_hash


def _secure_session_file() -> None:
    """Set session file to 0o600 permissions. Logs warning on OSError."""
    path = session_path()
    if not path.exists():
        return
    try:
        path.chmod(0o600)
    except OSError as exc:
        logger.warning("mtproto.chmod_failed", path=str(path), error=str(exc))


def _to_forum_topic(raw: object) -> ForumTopic | None:
    """Convert a raw Telethon topic object to ForumTopic, or None for deleted topics."""
    if not hasattr(raw, "title"):
        return None

    date_raw = getattr(raw, "date", None)
    created_date: datetime | None = None
    if isinstance(date_raw, int):
        created_date = datetime.fromtimestamp(date_raw, tz=timezone.utc)
    elif isinstance(date_raw, datetime):
        created_date = date_raw

    raw_dict: dict = {}
    if hasattr(raw, "to_dict"):
        try:
            raw_dict = raw.to_dict()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            raw_dict = {}
    elif hasattr(raw, "__dict__"):
        raw_dict = dict(raw.__dict__)

    return ForumTopic(
        topic_id=int(getattr(raw, "id", 0)),
        title=str(getattr(raw, "title", "")),
        top_message_id=int(getattr(raw, "top_message", 0)),
        is_closed=bool(getattr(raw, "closed", False)),
        is_hidden=bool(getattr(raw, "hidden", False)),
        created_date=created_date,
        raw=raw_dict,
    )


def _to_topic_message(raw: object) -> "TopicMessage | None":
    """Convert a raw Telethon message object to TopicMessage, or None if unsuitable."""
    msg_id = getattr(raw, "id", None)
    date_raw = getattr(raw, "date", None)
    if msg_id is None or date_raw is None:
        return None

    if isinstance(date_raw, int):
        date = datetime.fromtimestamp(date_raw, tz=timezone.utc)
    elif isinstance(date_raw, datetime):
        date = date_raw if date_raw.tzinfo else date_raw.replace(tzinfo=timezone.utc)
    else:
        return None

    sender = getattr(raw, "sender", None)
    # Primary: check bot flag on the resolved sender entity.
    # GetRepliesRequest doesn't always attach sender objects unless the entity
    # cache is primed. Fall back to via_bot_id or post_author as secondary signals.
    if sender is not None:
        from_bot = bool(getattr(sender, "bot", False))
    else:
        via_bot_id = getattr(raw, "via_bot_id", None)
        post_author = getattr(raw, "post_author", None)
        from_bot = bool(via_bot_id or post_author)
    text: str = getattr(raw, "message", "") or ""

    reply_to = getattr(raw, "reply_to", None)
    reply_to_msg_id: int | None = None
    if reply_to is not None:
        reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)

    return TopicMessage(
        message_id=int(msg_id),
        date=date,
        from_bot=from_bot,
        text=text,
        reply_to_message_id=reply_to_msg_id,
    )

class MTProtoClient:
    """Read-only MTProto client for Telegram Forum topic discovery.

    Credentials are resolved at construction time; no network activity until
    ``connect()`` or ``login()`` is called.

    This class intentionally exposes no write methods — all Telegram mutations
    go through the Bot API for auditability.
    """

    def __init__(
        self,
        *,
        api_id: int | None = None,
        api_hash: str | None = None,
        session_stem: str | None = None,
    ) -> None:
        """Construct the client, resolving credentials if not provided.

        Args:
            api_id: Telegram API ID.  Resolved from env / .env if not given.
            api_hash: Telegram API hash.  Resolved from env / .env if not given.
            session_stem: Path WITHOUT ``.session`` suffix.  Telethon appends it.
                Defaults to ``str(ccgram_dir() / "mtproto")``.

        Raises:
            MTProtoCredentialsError: if credentials cannot be resolved.
        """
        if api_id is None or api_hash is None:
            resolved_id, resolved_hash = _resolve_api_credentials()
            api_id = api_id if api_id is not None else resolved_id
            api_hash = api_hash if api_hash is not None else resolved_hash

        self._api_id: int = api_id
        self._api_hash: str = api_hash
        self._session_stem: str = (
            session_stem if session_stem is not None else str(ccgram_dir() / "mtproto")
        )
        self._client: object | None = None

    def _session_file(self) -> Path:
        return Path(self._session_stem + ".session")

    def _ensure_telethon_client(self) -> None:
        if self._client is None:
            from telethon import TelegramClient  # lazy import

            self._client = TelegramClient(
                self._session_stem, self._api_id, self._api_hash
            )

    async def login(self, *, phone: str | None = None) -> None:
        """Interactive login — prompts for phone/code if needed. Idempotent."""
        ccgram_dir().mkdir(parents=True, exist_ok=True)
        self._ensure_telethon_client()
        await self._client.start(phone=phone)  # type: ignore[union-attr]
        _secure_session_file()
        logger.info("mtproto.login_ok", session=self._session_stem)

    async def connect(self) -> None:
        """Non-interactive connect — requires a pre-existing valid session.

        Raises:
            MTProtoSessionMissingError: if session file absent or not authorised.
        """
        sf = self._session_file()
        if not sf.exists():
            raise MTProtoSessionMissingError(
                f"No MTProto session file at {sf}. "
                "Run MTProtoClient().login() once to create it."
            )
        self._ensure_telethon_client()
        await self._client.connect()  # type: ignore[union-attr]
        if not await self._client.is_user_authorized():  # type: ignore[union-attr]
            await self._client.disconnect()  # type: ignore[union-attr]
            raise MTProtoSessionMissingError(
                f"Session at {sf} is not authorised. "
                "Run MTProtoClient().login() to re-authenticate."
            )

    async def disconnect(self) -> None:
        """Disconnect from Telegram. Safe to call if not connected."""
        if self._client is not None:
            await self._client.disconnect()  # type: ignore[union-attr]

    async def __aenter__(self) -> "MTProtoClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    async def list_forum_topics(
        self,
        group_id: int,
        *,
        limit: int = 200,
    ) -> list[ForumTopic]:
        """List all forum topics in a supergroup/channel.

        Paginates automatically. Deleted topics are silently skipped.

        Args:
            group_id: Numeric Telegram group ID (negative for supergroups).
            limit: Maximum number of topics to return.
        """
        from telethon.tl.functions.messages import GetForumTopicsRequest  # lazy

        client = self._client
        entity = await client.get_input_entity(group_id)  # type: ignore[union-attr]

        collected: list[ForumTopic] = []
        seen_ids: set[int] = set()
        page_size = min(100, limit)
        offset_date = None
        offset_id = 0
        offset_topic = 0

        while len(collected) < limit:
            response = await client(  # type: ignore[union-attr]
                GetForumTopicsRequest(
                    peer=entity,
                    q=None,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=page_size,
                )
            )
            raw_topics = response.topics
            if not raw_topics:
                break

            for raw in raw_topics:
                ft = _to_forum_topic(raw)
                if ft is not None and ft.topic_id not in seen_ids:
                    seen_ids.add(ft.topic_id)
                    collected.append(ft)

            if len(raw_topics) < page_size:
                break

            last = raw_topics[-1]
            offset_topic = int(getattr(last, "id", 0))
            offset_id = int(getattr(last, "top_message", 0))
            raw_date = getattr(last, "date", None)
            offset_date = raw_date if raw_date else None

        return collected[:limit]

    async def get_forum_topics_by_id(
        self,
        group_id: int,
        topic_ids: list[int],
    ) -> list[ForumTopic]:
        """Fetch specific forum topics by their IDs.

        Empty input returns empty list without a network call.
        """
        if not topic_ids:
            return []

        from telethon.tl.functions.messages import GetForumTopicsByIDRequest  # lazy

        client = self._client
        entity = await client.get_input_entity(group_id)  # type: ignore[union-attr]
        response = await client(  # type: ignore[union-attr]
            GetForumTopicsByIDRequest(peer=entity, topics=topic_ids)
        )

        result: list[ForumTopic] = []
        for raw in response.topics:
            ft = _to_forum_topic(raw)
            if ft is not None:
                result.append(ft)
        return result

    async def get_topic_history(
        self,
        group_id: int,
        topic_id: int,
        *,
        limit: int = 200,
        min_date: "datetime | None" = None,
    ) -> "list[TopicMessage]":
        """Fetch message history for a forum topic.

        Returns messages most-recent first. Paginates automatically up to
        ``limit``. ``min_date`` (UTC) stops pagination once messages older
        than the cutoff are encountered.

        Args:
            group_id: Numeric Telegram group ID (negative for supergroups).
            topic_id: The forum topic ID (== message_thread_id in Bot API).
            limit: Maximum total messages to return.
            min_date: Optional UTC floor — pagination stops when a message's
                date is before this value.
        """
        from telethon.tl.functions.messages import GetRepliesRequest  # lazy

        client = self._client
        entity = await client.get_input_entity(group_id)  # type: ignore[union-attr]

        collected: list[TopicMessage] = []
        offset_id = 0
        offset_date = 0
        add_offset = 0
        page_size = min(100, limit)

        while len(collected) < limit:
            response = await client(  # type: ignore[union-attr]
                GetRepliesRequest(
                    peer=entity,
                    msg_id=topic_id,
                    offset_id=offset_id,
                    offset_date=offset_date,
                    add_offset=add_offset,
                    limit=page_size,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
            messages = getattr(response, "messages", [])
            if not messages:
                break

            stop = False
            for raw in messages:
                tm = _to_topic_message(raw)
                if tm is None:
                    continue
                if min_date is not None:
                    msg_date = tm.date
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                    if msg_date < min_date:
                        stop = True
                        break
                collected.append(tm)
                if len(collected) >= limit:
                    stop = True
                    break

            if stop or len(messages) < page_size:
                break

            last = messages[-1]
            offset_id = int(getattr(last, "id", 0))
            raw_date = getattr(last, "date", None)
            if isinstance(raw_date, int):
                offset_date = raw_date
            elif isinstance(raw_date, datetime):
                offset_date = int(raw_date.timestamp())

        return collected[:limit]

