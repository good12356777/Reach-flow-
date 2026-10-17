"""
Multi-User Telegram Promotion Manager - ONE FILE (Bot API only, Render Free friendly).

No personal Telegram login and no API ID/hash: only a bot token and your numeric Telegram ID.
Users link their own groups/channels (they must be admins; the bot must be able to post),
build a campaign, preview, confirm, run, stop, read reports. Private messages go only to people
who tapped Subscribe on the sender's personal link.

RENDER (Free web service) - set these once in the dashboard:
  Build Command : pip install "aiogram>=3.13,<4"
  Start Command : python app.py
  Health check  : /health
  Environment   : BOT_TOKEN, ADMIN_TELEGRAM_ID, PYTHON_VERSION=3.12.3
                  STORAGE_CHAT_ID  (recommended: id of a PRIVATE channel the bot administers,
                                    so opt-ins and linked chats survive restarts/sleep)
                  BLOCKED_USER_IDS (optional, comma-separated)
LOCAL: pip install "aiogram>=3.13,<4", set the variables (or a .env file), run: python app.py
Never put the bot token inside this file or on GitHub.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from typing import Callable, Optional, Tuple
import asyncio
import uuid
from collections import Counter, deque
from datetime import datetime
from enum import Enum
from typing import Optional
import copy
import json
from typing import Any, Optional, Protocol
from datetime import datetime, timezone
from html import escape
from typing import Awaitable, Callable, Optional, Protocol
from typing import Awaitable, Callable, Optional
from typing import Tuple
from aiogram.exceptions import TelegramAPIError
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
import io
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaDocument
import math
from collections import Counter
from html import escape as esc
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
import signal
import sys


# ============================================================================
# Configuration (environment variables)
# ============================================================================
class ConfigError(RuntimeError):
    """Missing or invalid configuration. Messages never contain secret values."""


_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
REQUIRED_VARS = ("BOT_TOKEN", "ADMIN_TELEGRAM_ID")


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    admin_id: int
    data_dir: Path
    storage_chat_id: Optional[int] = None      # private channel that keeps small state across Render Free sleeps
    base_url: Optional[str] = None             # public https URL; present => webhook mode (Render sets it)
    port: int = 10000                          # Render injects PORT for web services
    blocked_ids: tuple = ()                    # permanent block list from BLOCKED_USER_IDS
    max_concurrent_campaigns: int = 3
    max_image_bytes: int = 5 * 1024 * 1024
    max_groups_per_campaign: int = 10
    log_level: str = "INFO"

    @property
    def webhook_mode(self) -> bool:
        return self.base_url is not None


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader for local development. Never overrides real env vars."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(env: Mapping[str, str], name: str, default: Optional[int] = None, allow_negative: bool = False) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{name} is required.")
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number.") from None
    if value == 0 or (value < 0 and not allow_negative):
        raise ConfigError(f"{name} must be {'non-zero' if allow_negative else 'greater than zero'}.")
    return value


def load_config(env: Optional[Mapping[str, str]] = None, check_disk: bool = True) -> Config:
    env = os.environ if env is None else env

    missing = [n for n in REQUIRED_VARS if not env.get(n, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
            + ". Copy .env.example to .env for local use, or set them in the Render dashboard."
        )

    problems: list[str] = []
    token = env["BOT_TOKEN"].strip()
    if not _TOKEN_RE.match(token):
        problems.append("BOT_TOKEN does not look like a BotFather token")

    vals: dict[str, Optional[int]] = {}
    for name, default, neg in (
        ("ADMIN_TELEGRAM_ID", None, False),
        ("MAX_CONCURRENT_CAMPAIGNS", 3, False),
        ("MAX_GROUPS_PER_CAMPAIGN", 10, False),
        ("PORT", 10000, False),
    ):
        try:
            vals[name] = _int(env, name, default, neg)
        except ConfigError as exc:
            problems.append(str(exc))
    storage = None
    if env.get("STORAGE_CHAT_ID", "").strip():
        try:
            storage = _int(env, "STORAGE_CHAT_ID", allow_negative=True)
        except ConfigError as exc:
            problems.append(str(exc))

    blocked: list[int] = []
    for part in env.get("BLOCKED_USER_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and int(part) > 0:
            blocked.append(int(part))
        else:
            problems.append("BLOCKED_USER_IDS must be comma-separated numeric Telegram ids")
            break

    base_url = (env.get("WEBHOOK_BASE_URL", "").strip() or env.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")
    if base_url and not base_url.startswith("https://"):
        problems.append("WEBHOOK_BASE_URL must start with https:// (Telegram only calls secure URLs)")

    if problems:
        raise ConfigError("Invalid configuration: " + "; ".join(problems) + ".")

    data_dir = Path(env.get("DATA_DIR", "./data").strip() or "./data").resolve()
    if check_disk:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            probe = data_dir / ".write_test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise ConfigError(f"DATA_DIR ({data_dir}) is not writable: {exc.strerror}.") from None

    return Config(
        bot_token=token,
        admin_id=vals["ADMIN_TELEGRAM_ID"],
        data_dir=data_dir,
        storage_chat_id=storage,
        base_url=base_url or None,
        port=vals["PORT"],
        blocked_ids=tuple(blocked),
        max_concurrent_campaigns=vals["MAX_CONCURRENT_CAMPAIGNS"],
        max_groups_per_campaign=vals["MAX_GROUPS_PER_CAMPAIGN"],
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


# ============================================================================
# Security: log redaction, validation, rate limiting
# ============================================================================
_SECRET_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),  # bot tokens
    re.compile(r"\b[0-9a-fA-F]{32}\b"),  # long hex secrets (defence in depth)
]
_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Remember an exact secret so it is scrubbed from every log line."""
    if value and len(value) >= 6:
        _registered.add(value)


def redact(text: str) -> str:
    for secret in _registered:
        text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redacts the final formatted line, including tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def is_admin(admin_id: int, user_id: int) -> bool:
    """The only way to be admin is to match the ADMIN_TELEGRAM_ID environment variable."""
    return isinstance(user_id, int) and user_id == admin_id


class RateLimiter:
    """Sliding-window limiter against button/message spamming, with a short block."""

    def __init__(
        self,
        max_events: int = 25,
        window: float = 20.0,
        block_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_events, self.window, self.block_seconds, self._clock = (
            max_events, window, block_seconds, clock,
        )
        self._events: dict[int, deque] = defaultdict(deque)
        self._blocked_until: dict[int, float] = {}

    def allow(self, key: int) -> bool:
        now = self._clock()
        if now < self._blocked_until.get(key, 0.0):
            return False
        q = self._events[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            self._blocked_until[key] = now + self.block_seconds
            q.clear()
            return False
        q.append(now)
        return True


# ---------------------------------------------------------------- validation
MAX_TITLE = 60
MAX_TEXT = 3500  # leaves room for the opt-in footer inside Telegram's 4096 limit
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    """The message is safe to show to the user."""


def clean_title(raw: str) -> str:
    value = re.sub(r"\s+", " ", _CONTROL.sub("", raw or "")).strip()
    if not value:
        raise ValidationError("The title can't be empty.")
    if len(value) > MAX_TITLE:
        raise ValidationError(f"The title is too long ({len(value)}/{MAX_TITLE} characters).")
    return value


def clean_message(raw: str) -> str:
    value = _CONTROL.sub("", raw or "").strip()
    if not value:
        raise ValidationError("The message can't be empty.")
    if len(value) > MAX_TEXT:
        raise ValidationError(f"The message is too long ({len(value)}/{MAX_TEXT} characters).")
    return value


def clean_brand(raw: str) -> str:
    value = re.sub(r"\s+", " ", _CONTROL.sub("", raw or "")).strip()
    if not 2 <= len(value) <= 40:
        raise ValidationError("The sender name must be 2 to 40 characters.")
    return value


def clean_phone(raw: str) -> str:
    value = re.sub(r"[\s\-().]", "", raw or "")
    if not value.startswith("+"):
        value = "+" + value
    if not re.fullmatch(r"\+\d{7,15}", value):
        raise ValidationError("Please send your number with country code, e.g. +2348012345678.")
    return value


def clean_code(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not 5 <= len(digits) <= 6:
        raise ValidationError("The login code has 5 or 6 digits.")
    return digits


def parse_user_id(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{1,15}", raw) and int(raw) > 0:
        return int(raw)
    return None


def sniff_image(data: bytes) -> Optional[Tuple[str, str]]:
    """Identify JPEG/PNG/WEBP by file signature (never trust file names). -> (ext, mime)"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def webhook_secret(bot_token: str) -> str:
    """Derived secret so Telegram's webhook calls can be told apart from anyone else's.
    Sent by Telegram in a header and also used for the URL path. Needs no extra env var."""
    return hmac.new(bot_token.encode(), b"promo-bot-webhook", hashlib.sha256).hexdigest()[:48]


_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def parse_chat_ref(raw: str):
    """'@name', 't.me/name', 'https://t.me/name' -> '@name'; '-1001234567890' -> int; else None."""
    value = (raw or "").strip()
    value = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", value)
    if re.fullmatch(r"-?\d{5,20}", value):
        return int(value)
    value = value.lstrip("@")
    return f"@{value}" if _USERNAME.fullmatch(value) else None


# The other sections refer to these helpers as `security.<name>`.
from types import SimpleNamespace as _NS  # noqa: E402
security = _NS(register_secret=register_secret, redact=redact, RedactingFormatter=RedactingFormatter, setup_logging=setup_logging, is_admin=is_admin, RateLimiter=RateLimiter, ValidationError=ValidationError, clean_title=clean_title, clean_message=clean_message, clean_brand=clean_brand, clean_phone=clean_phone, clean_code=clean_code, parse_user_id=parse_user_id, sniff_image=sniff_image, webhook_secret=webhook_secret, parse_chat_ref=parse_chat_ref)


# ============================================================================
# In-memory workspaces (drafts, progress, reports)
# ============================================================================
class Status(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    FAILED = "Failed"


@dataclass
class GroupInfo:
    chat_id: int
    title: str
    kind: str  # "group" | "supergroup" | "channel"
    username: Optional[str] = None
    members: Optional[int] = None
    can_post: Optional[bool] = None  # None = not checked yet
    reason: str = ""


@dataclass
class Settings:
    batch_size: int = 30       # opt-in subscribers per batch (a processing preference only)
    message_delay: float = 1.0  # seconds between private messages
    batch_pause: float = 30.0   # seconds between batches


@dataclass
class Campaign:
    id: str
    owner_id: int
    title: str = ""
    text: str = ""
    image: Optional[bytes] = field(default=None, repr=False)
    image_ext: str = "jpg"
    targets: list[GroupInfo] = field(default_factory=list)
    include_subscribers: bool = False
    revision: int = 0
    confirmed_rev: Optional[int] = None
    # progress (real counters only)
    status: Status = Status.PENDING
    eligible: int = 0
    attempts: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    stage: str = ""
    note: str = ""
    failure_reasons: Counter = field(default_factory=Counter)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    recipient_ids: list[int] = field(default_factory=list, repr=False)

    def touch(self) -> None:
        """Any edit invalidates an earlier confirmation."""
        self.revision += 1
        self.confirmed_rev = None

    def destination_label(self) -> str:
        parts = [t.title for t in self.targets]
        if self.include_subscribers:
            parts.append("My opt-in subscribers (private messages)")
        return ", ".join(parts) if parts else "None selected"

    def scrub(self) -> None:
        """Release recipient-level data and heavy content once the run is over."""
        self.recipient_ids.clear()
        self.image = None
        self.targets = [GroupInfo(t.chat_id, t.title, t.kind) for t in self.targets]
        self.task = None


@dataclass(frozen=True)
class Report:
    campaign_id: str
    title: str
    destination: str
    status: str
    eligible: int
    attempts: int
    success: int
    failed: int
    skipped: int
    completed_at: datetime
    reasons: dict
    note: str = ""


@dataclass
class Workspace:
    user_id: int
    settings: Settings = field(default_factory=Settings)
    draft: Optional[Campaign] = None
    running: Optional[Campaign] = None
    reports: deque = field(default_factory=lambda: deque(maxlen=20))  # newest first
    groups: list[GroupInfo] = field(default_factory=list)
    group_query: str = ""
    links_restored: bool = False  # saved links reloaded once per process run
    created_at: float = field(default_factory=time.time)

    def new_draft(self) -> Campaign:
        self.draft = Campaign(id=uuid.uuid4().hex[:8], owner_id=self.user_id)
        return self.draft

    def get_draft(self) -> Campaign:
        return self.draft or self.new_draft()


class TemporaryStore:
    def __init__(self) -> None:
        self._ws: dict[int, Workspace] = {}

    def get(self, user_id: int) -> Workspace:
        if user_id not in self._ws:
            self._ws[user_id] = Workspace(user_id=user_id)
        return self._ws[user_id]

    def peek(self, user_id: int) -> Optional[Workspace]:
        return self._ws.get(user_id)

    def drop(self, user_id: int) -> None:
        self._ws.pop(user_id, None)

    def stats(self) -> dict:
        """Aggregate numbers only (used by the administrator). No user content."""
        return {
            "workspaces": len(self._ws),
            "running": sum(1 for w in self._ws.values() if w.running),
            "reports": sum(len(w.reports) for w in self._ws.values()),
        }


# ============================================================================
# Small saved state (opt-ins, links, suspensions)
# ============================================================================
log = logging.getLogger(__name__)


def _fresh() -> dict:
    return {"v": 1, "owners": {}, "suspended": [], "destinations": {}}


class Persister(Protocol):
    label: str

    async def load(self) -> Optional[dict]: ...
    async def save(self, state: dict) -> None: ...


class FilePersister:
    label = "file"

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "state.json"

    def load_sync(self) -> Optional[dict]:
        if not self._path.is_file():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.error("state.json was unreadable; it was set aside and a fresh one started.")
            try:
                self._path.rename(self._path.with_suffix(".corrupt"))
            except OSError:
                pass
            return None

    async def load(self) -> Optional[dict]:
        return self.load_sync()

    async def save(self, state: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self._path)


class PersistentStore:
    def __init__(self, persister: Optional[Persister] = None, state: Optional[dict] = None,
                 blocked_ids: tuple = ()):
        self._p = persister
        self._blocked = frozenset(blocked_ids)
        self.changed = asyncio.Event()  # set on every change so a background task can save (never the handlers)
        self._s = _fresh()
        if state:
            for key in self._s:
                if key in state:
                    self._s[key] = state[key]
        self._dirty = False
        self._lock = asyncio.Lock()
        self.last_saved: Optional[float] = None

    # ---------------------------------------------------------- construction
    @classmethod
    async def load(cls, persister: Persister, blocked_ids: tuple = ()) -> "PersistentStore":
        """Raises if the backend cannot be read, so the caller never overwrites good data."""
        return cls(persister, await persister.load(), blocked_ids)

    @classmethod
    def from_dir(cls, directory: Path, blocked_ids: tuple = ()) -> "PersistentStore":
        p = FilePersister(directory)
        return cls(p, p.load_sync(), blocked_ids)

    @property
    def pending(self) -> bool:
        return self._dirty

    @property
    def backend(self) -> str:
        return self._p.label if self._p else "memory"

    def _touch(self) -> None:
        self._dirty = True
        self.changed.set()

    async def flush(self) -> bool:
        """Write pending changes. Never raises: a failed save keeps data in memory and retries later."""
        if self._p is None or not self._dirty:
            return False
        async with self._lock:
            if not self._dirty:
                return False
            self._dirty = False
            snapshot: Any = copy.deepcopy(self._s)
            try:
                await self._p.save(snapshot)
            except Exception as exc:
                self._dirty = True
                log.warning("Saving state failed (%s); will retry.", type(exc).__name__)
                return False
            self.last_saved = time.time()
            return True

    # -------------------------------------------------------------- suspension
    def is_suspended(self, user_id: int) -> bool:
        return user_id in self._blocked or user_id in self._s["suspended"]

    def suspend(self, user_id: int) -> None:
        if user_id not in self._s["suspended"]:
            self._s["suspended"].append(user_id)
            self._touch()

    def unsuspend(self, user_id: int) -> bool:
        if user_id in self._s["suspended"]:
            self._s["suspended"].remove(user_id)
            self._touch()
            return True
        return False

    def list_suspended(self) -> list[int]:
        return sorted(set(self._s["suspended"]) | self._blocked)

    # ------------------------------------------------------------ destinations
    def add_destination(self, chat_id: int, title: str, kind: str, owner_id: int) -> None:
        d = self._s["destinations"].setdefault(str(chat_id), {"title": title, "kind": kind, "owners": []})
        d["title"], d["kind"] = (title or "Untitled")[:80], kind
        if owner_id not in d["owners"]:
            d["owners"].append(owner_id)
        self._touch()

    def remove_destination(self, chat_id: int) -> bool:
        if self._s["destinations"].pop(str(chat_id), None) is not None:
            self._touch()
            return True
        return False

    def destinations_for(self, owner_id: int) -> list[dict]:
        out = [
            {"id": int(cid), "title": d["title"], "kind": d["kind"]}
            for cid, d in self._s["destinations"].items() if owner_id in d["owners"]
        ]
        return sorted(out, key=lambda d: d["title"].lower())

    def unlink_destination(self, chat_id: int, owner_id: int) -> bool:
        """Remove ONE user's claim on ONE chat (other admins' claims stay)."""
        d = self._s["destinations"].get(str(chat_id))
        if not d or owner_id not in d["owners"]:
            return False
        d["owners"].remove(owner_id)
        if not d["owners"]:
            del self._s["destinations"][str(chat_id)]
        self._touch()
        return True

    def unlink_owner(self, owner_id: int) -> int:
        """Remove one user's claim on every destination. Returns how many were unlinked."""
        n = 0
        for cid in list(self._s["destinations"]):
            d = self._s["destinations"][cid]
            if owner_id in d["owners"]:
                d["owners"].remove(owner_id)
                n += 1
                if not d["owners"]:
                    del self._s["destinations"][cid]
        if n:
            self._touch()
        return n

    def destination_count(self) -> int:
        return len(self._s["destinations"])

    # ---------------------------------------------------------------- opt-ins
    def _owner(self, owner_id: int) -> Optional[dict]:
        return self._s["owners"].get(str(owner_id))

    def owner_exists(self, owner_id: int) -> bool:
        return self._owner(owner_id) is not None

    def set_brand(self, owner_id: int, brand: str) -> None:
        entry = self._s["owners"].setdefault(str(owner_id), {"brand": brand, "subs": {}})
        entry["brand"] = brand
        self._touch()

    def get_brand(self, owner_id: int) -> Optional[str]:
        entry = self._owner(owner_id)
        return entry["brand"] if entry else None

    def subscribe(self, owner_id: int, subscriber_id: int) -> bool:
        entry = self._owner(owner_id)
        if entry is None:
            return False
        entry["subs"][str(subscriber_id)] = int(time.time())
        self._touch()
        return True

    def unsubscribe(self, owner_id: int, subscriber_id: int) -> bool:
        entry = self._owner(owner_id)
        if entry and entry["subs"].pop(str(subscriber_id), None) is not None:
            self._touch()
            return True
        return False

    def unsubscribe_all(self, subscriber_id: int) -> int:
        removed = 0
        for entry in self._s["owners"].values():
            if entry["subs"].pop(str(subscriber_id), None) is not None:
                removed += 1
        if removed:
            self._touch()
        return removed

    def is_subscribed(self, owner_id: int, subscriber_id: int) -> bool:
        entry = self._owner(owner_id)
        return bool(entry and str(subscriber_id) in entry["subs"])

    def subscribers(self, owner_id: int) -> list[int]:
        entry = self._owner(owner_id)
        return [int(s) for s in entry["subs"]] if entry else []

    def count(self, owner_id: int) -> int:
        entry = self._owner(owner_id)
        return len(entry["subs"]) if entry else 0

    def owners_count(self) -> int:
        return len(self._s["owners"])


# ============================================================================
# Reports and previews
# ============================================================================
REASON_LABELS = {
    "no_permission": "The bot has no permission to post there",
    "inaccessible": "Chat not accessible (removed, or upgraded to a supergroup)",
    "message_too_long": "Message too long for Telegram",
    "flood_wait": "Telegram asked us to slow down",
    "rate_limited": "Telegram rate limit (not retried again)",
    "flood_wait_too_long": "Telegram wait time too long; campaign stopped",
    "recipient_blocked_bot": "Recipient blocked the bot (removed from list)",
    "recipient_unreachable": "Recipient unreachable",
    "network_error": "Network problem",
        "telegram_error": "Other Telegram error",
    "bad_request": "Telegram rejected the message",
    "unexpected_error": "Unexpected error",
}

NOT_READ_NOTE = (
    "ℹ️ “Successful” means Telegram accepted the message. "
    "It does not mean anyone has read it."
)


def make_report(c: Campaign) -> Report:
    return Report(
        campaign_id=c.id,
        title=c.title,
        destination=c.destination_label(),
        status=c.status.value,
        eligible=c.eligible,
        attempts=c.attempts,
        success=c.success,
        failed=c.failed,
        skipped=c.skipped,
        completed_at=datetime.fromtimestamp(c.finished_at, timezone.utc)
        if c.finished_at else datetime.now(timezone.utc),
        reasons=dict(c.failure_reasons),
        note=c.note,
    )


def format_report(r: Report) -> str:
    lines = [
        "📊 <b>CAMPAIGN REPORT</b>",
        "",
        f"<b>CAMPAIGN NAME:</b>\n{escape(r.title)}",
        f"<b>DESTINATION:</b>\n{escape(r.destination)}",
        f"<b>CAMPAIGN STATUS:</b>\n{escape(r.status)}",
        f"<b>ELIGIBLE RECIPIENTS:</b>\n{r.eligible}",
        f"<b>ATTEMPTS:</b>\n{r.attempts}",
        f"<b>SUCCESSFUL SUBMISSIONS:</b>\n{r.success}",
        f"<b>FAILED ATTEMPTS:</b>\n{r.failed}",
        f"<b>SKIPPED ITEMS:</b>\n{r.skipped}",
        f"<b>COMPLETION TIME:</b>\n{r.completed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ]
    if r.reasons:
        lines += ["", "<b>Why items failed:</b>"]
        lines += [f"• {escape(REASON_LABELS.get(k, k))}: {v}" for k, v in sorted(r.reasons.items())]
    if r.note:
        lines += ["", f"📝 {escape(r.note)}"]
    lines += ["", NOT_READ_NOTE]
    return "\n".join(lines)


def format_progress(c: Campaign) -> str:
    done = c.attempts + c.skipped
    return (
        f"▶️ <b>{escape(c.title)}</b> — {escape(c.status.value)}\n"
        f"Stage: {escape(c.stage or 'starting')}\n"
        f"Processed: {done} of {c.eligible}\n"
        f"Accepted by Telegram: {c.success} · Failed: {c.failed} · Skipped: {c.skipped}"
    )


def format_preview_summary(c: Campaign, subscriber_count: int = 0) -> str:
    img = f"attached ({c.image_ext.upper()}, {len(c.image) // 1024} KB)" if c.image else "none"
    lines = [
        "👁 <b>Campaign preview</b>",
        f"<b>Title:</b> {escape(c.title or '—')}",
        f"<b>Image:</b> {img}",
        f"<b>Destinations:</b> {escape(c.destination_label())}",
    ]
    if c.include_subscribers:
        lines.append(f"<b>Opt-in subscribers who will be messaged:</b> {subscriber_count}")
    lines.append("\nThe exact message is shown below this summary.")
    return "\n".join(lines)


# ============================================================================
# Delivery engine (pacing, batches, flood-wait, honest counters)
# ============================================================================
log = logging.getLogger(__name__)


class SendError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        retry_after: Optional[float] = None,
        unsubscribe: bool = False,
        abort_all: bool = False,
    ):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.unsubscribe = unsubscribe
        self.abort_all = abort_all


class GroupSender(Protocol):
    async def send(self, target: GroupInfo, campaign: Campaign) -> None: ...


class DMSender(Protocol):
    async def send(self, recipient_id: int, campaign: Campaign, brand: str) -> None: ...


class _Abort(Exception):
    pass


class _Skip(Exception):
    pass


class DeliveryEngine:
    def __init__(
        self,
        persist: PersistentStore,
        dm_sender: DMSender,
        *,
        group_delay: float = 5.0,
        max_flood_wait: float = 120.0,
    ):
        self._persist = persist
        self._dm = dm_sender
        self._group_delay = group_delay
        self._max_flood_wait = max_flood_wait

    # ---------------------------------------------------------------- helpers
    @staticmethod
    async def _pause(c: Campaign, seconds: float) -> bool:
        """Sleep, but wake immediately on cancel. True = slept fully, False = cancelled."""
        if c.cancel_event.is_set():
            return False
        try:
            await asyncio.wait_for(c.cancel_event.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def _with_flood_wait(self, c: Campaign, send: Callable[[], Awaitable[None]]) -> None:
        try:
            return await send()
        except SendError as exc:
            if exc.retry_after is None:
                raise
            if exc.retry_after > self._max_flood_wait:
                raise SendError("flood_wait_too_long", abort_all=True) from None
            if not await self._pause(c, exc.retry_after + 1):
                raise _Skip() from None
        try:  # exactly one retry, only after the wait Telegram asked for
            return await send()
        except SendError as exc:
            if exc.retry_after is not None:
                raise SendError("rate_limited") from None
            raise

    async def _attempt(self, c: Campaign, send: Callable[[], Awaitable[None]]) -> Optional[SendError]:
        c.attempts += 1
        try:
            await self._with_flood_wait(c, send)
            c.success += 1
            return None
        except _Skip:
            c.attempts -= 1
            c.skipped += 1
            return None
        except SendError as exc:
            c.failed += 1
            c.failure_reasons[exc.reason] += 1
            if exc.abort_all:
                raise _Abort(exc.reason) from None
            return exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never log message content or recipient ids
            log.error("Unexpected delivery error: %s", type(exc).__name__)
            c.failed += 1
            c.failure_reasons["unexpected_error"] += 1
            return None

    # -------------------------------------------------------------------- run
    async def run(self, c: Campaign, settings: Settings, group_sender: Optional[GroupSender]) -> None:
        c.status = Status.RUNNING
        c.started_at = time.time()
        try:
            recipients: list[int] = []
            if c.include_subscribers:
                recipients = list(dict.fromkeys(self._persist.subscribers(c.owner_id)))  # dedupe
            c.recipient_ids = recipients
            c.eligible = len(c.targets) + len(recipients)

            # --- groups / channels the user's own account may post to
            if c.targets:
                if group_sender is None:
                    raise SendError("no_permission", abort_all=True)
                c.stage = "posting to groups/channels"
                for i, target in enumerate(c.targets):
                    if c.cancel_event.is_set():
                        break
                    await self._attempt(c, lambda t=target: group_sender.send(t, c))
                    more = i < len(c.targets) - 1 or recipients
                    if more and not await self._pause(c, self._group_delay):
                        break

            # --- opt-in subscribers, in batches, via the bot
            if recipients and not c.cancel_event.is_set():
                brand = self._persist.get_brand(c.owner_id) or "the sender"
                size = max(1, settings.batch_size)
                batches = [recipients[i:i + size] for i in range(0, len(recipients), size)]
                for b_index, batch in enumerate(batches):
                    c.stage = f"private messages, batch {b_index + 1} of {len(batches)}"
                    for rid in batch:
                        if c.cancel_event.is_set():
                            break
                        if not self._persist.is_subscribed(c.owner_id, rid):
                            c.skipped += 1  # opted out while the campaign was running
                            continue
                        err = await self._attempt(c, lambda r=rid: self._dm.send(r, c, brand))
                        if err and err.unsubscribe:
                            self._persist.unsubscribe(c.owner_id, rid)
                        if not await self._pause(c, settings.message_delay):
                            break
                    if c.cancel_event.is_set():
                        break
                    if b_index < len(batches) - 1 and not await self._pause(c, settings.batch_pause):
                        break

            c.status = Status.CANCELLED if c.cancel_event.is_set() else Status.COMPLETED
            if c.status is Status.CANCELLED:
                c.note = "Stopped before all items were processed."
        except _Abort as exc:
            c.status = Status.FAILED
            c.note = f"Campaign stopped: {exc}."
        except SendError as exc:
            c.status = Status.FAILED
            c.failure_reasons[exc.reason] += 1
            c.note = f"Campaign stopped: {exc.reason}."
        except asyncio.CancelledError:
            c.status = Status.CANCELLED
            c.note = "Interrupted because the application was shutting down."
            raise
        except Exception as exc:
            log.error("Campaign crashed: %s", type(exc).__name__)
            c.status = Status.FAILED
            c.note = "An unexpected error stopped this campaign."
        finally:
            remaining = c.eligible - c.attempts - c.skipped
            if remaining > 0:
                c.skipped += remaining  # never attempted: report them as skipped, not as sent
            c.stage = "finished"
            c.finished_at = time.time()


# ============================================================================
# Campaign lifecycle (confirmation, limits, stop)
# ============================================================================
log = logging.getLogger(__name__)
FinishCallback = Callable[[int, Report], Awaitable[None]]


class CampaignError(Exception):
    """Safe to show to the user."""


class CampaignManager:
    def __init__(self, max_concurrent: int, persist: PersistentStore, engine: DeliveryEngine):
        self._max = max_concurrent
        self._persist = persist
        self._engine = engine
        self._active = 0
        self._tasks: set[asyncio.Task] = set()
        self._shutting_down = False

    @property
    def active(self) -> int:
        return self._active

    def problems(self, ws: Workspace, *, can_post_groups: bool = True) -> list[str]:
        c = ws.draft
        if c is None:
            return ["Create a promotion first."]
        out: list[str] = []
        if not c.title:
            out.append("Add a campaign title.")
        if not c.text:
            out.append("Add a promotional message.")
        if not c.targets and not c.include_subscribers:
            out.append("Choose at least one destination.")
        if c.targets and not can_post_groups:
            out.append("Posting to groups is not available right now.")
        for t in c.targets:
            if t.can_post is not True:
                out.append(f"Posting in “{t.title}” is not confirmed. Open it in MY GROUPS to re-check.")
        if c.include_subscribers:
            if self._persist.count(c.owner_id) == 0:
                out.append("You have no opted-in subscribers yet.")
            if not self._persist.get_brand(c.owner_id):
                out.append("Set a sender name in MY SETTINGS first.")
        return out

    async def start(
        self,
        ws: Workspace,
        group_sender: Optional[GroupSender],
        on_finish: Optional[FinishCallback] = None,
    ) -> Campaign:
        c = ws.draft
        if c is None:
            raise CampaignError("There is no campaign draft to start.")
        if c.owner_id != ws.user_id:  # defence in depth
            raise CampaignError("This campaign does not belong to you.")
        if ws.running is not None:
            raise CampaignError("You already have a campaign running. Stop it or wait for it to finish.")
        if c.confirmed_rev != c.revision:
            raise CampaignError("Please preview and confirm the campaign before starting it.")
        if self._active >= self._max:
            raise CampaignError("The platform is busy with other campaigns. Please try again in a few minutes.")
        problems = self.problems(ws, can_post_groups=group_sender is not None)
        if problems:
            raise CampaignError(" ".join(problems))

        ws.draft = None
        ws.running = c
        self._active += 1
        task = asyncio.create_task(self._run(ws, c, group_sender, on_finish))
        c.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return c

    async def _run(self, ws: Workspace, c: Campaign, group_sender, on_finish) -> None:
        try:
            await self._engine.run(c, ws.settings, group_sender)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("Campaign task failed: %s", type(exc).__name__)
            c.status = Status.FAILED
        finally:
            self._active -= 1
            report = make_report(c)
            ws.reports.appendleft(report)
            ws.running = None
            c.scrub()
        await self._persist.flush()  # e.g. subscribers who blocked the bot were removed
        if on_finish and not self._shutting_down:
            try:
                await on_finish(ws.user_id, report)
            except Exception as exc:
                log.warning("Could not deliver the finished-campaign notice: %s", type(exc).__name__)

    def stop(self, ws: Workspace) -> bool:
        """Cancel THIS user's campaign only."""
        if ws.running is None:
            return False
        ws.running.cancel_event.set()
        return True

    async def shutdown(self) -> None:
        self._shutting_down = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


# ============================================================================
# Linking chats and verifying admin/bot rights
# ============================================================================
log = logging.getLogger(__name__)
KINDS = {"group": "group", "supergroup": "supergroup", "channel": "channel"}


class ChatError(Exception):
    """Message is safe to show to the user."""


def assess_bot(chat_type: str, bot_member) -> Tuple[bool, str]:
    """Can the BOT post in this chat, judging from its own membership record?"""
    status = getattr(bot_member, "status", "")
    if chat_type == "channel":
        if status == "administrator" and getattr(bot_member, "can_post_messages", False):
            return True, "The bot is an admin here and can post."
        if status == "administrator":
            return False, "The bot is an admin but is not allowed to post messages. Enable “Post messages” for it."
        return False, "Make the bot an admin of the channel with permission to post messages."
    if status in ("creator", "administrator", "member"):
        return True, "The bot is in this group and can send messages (confirmed for real when sending)."
    if status == "restricted":
        if getattr(bot_member, "can_send_messages", True):
            return True, "The bot is restricted in this group but can still send messages."
        return False, "The bot is muted in this group. Allow it to send messages."
    return False, "The bot is not in this group any more. Add it again."


class ChatManager:
    def __init__(self, bot):
        self._bot = bot

    async def verify(self, user_id: int, ref) -> GroupInfo:
        """Return a GroupInfo for a chat the user administers and the bot can reach."""
        try:
            chat = await self._bot.get_chat(ref)
        except TelegramAPIError:
            raise ChatError(
                "I can't see that chat. Add the bot to it first (as admin for channels), "
                "then try again."
            ) from None
        if chat.type not in KINDS:
            raise ChatError("That is not a group or channel.")
        try:
            bot_member = await self._bot.get_chat_member(chat.id, self._bot.id)
        except TelegramAPIError:
            raise ChatError("The bot is not in that chat yet. Add it, then try again.") from None
        try:
            member = await self._bot.get_chat_member(chat.id, user_id)
        except TelegramAPIError:
            raise ChatError("I couldn't confirm your role in that chat.") from None
        if member.status not in ("creator", "administrator"):
            raise ChatError("Only the owner or an administrator of a chat can use it for promotions.")
        if chat.type == "channel" and member.status == "administrator" \
                and getattr(member, "can_post_messages", False) is not True:
            raise ChatError("Your admin role in that channel does not include posting messages.")
        ok, reason = assess_bot(chat.type, bot_member)
        members = None
        try:
            members = await self._bot.get_chat_member_count(chat.id)
        except TelegramAPIError:
            pass
        return GroupInfo(
            chat_id=chat.id,
            title=(chat.title or "Untitled")[:80],
            kind=KINDS[chat.type],
            username=getattr(chat, "username", None),
            members=members,
            can_post=ok,
            reason=reason,
        )

    async def recheck(self, user_id: int, info: GroupInfo) -> Tuple[bool, str]:
        """Re-verify ownership and the bot's rights right now. Updates `info` in place."""
        try:
            fresh = await self.verify(user_id, info.chat_id)
        except ChatError as exc:
            info.can_post, info.reason = False, str(exc)
            return False, info.reason
        info.title, info.kind, info.username = fresh.title, fresh.kind, fresh.username
        info.members, info.can_post, info.reason = fresh.members, fresh.can_post, fresh.reason
        return bool(info.can_post), info.reason


def filter_groups(groups: list[GroupInfo], query: str) -> list[tuple[int, GroupInfo]]:
    """(original index, group) pairs whose title matches the query."""
    q = query.strip().lower()
    return [(i, g) for i, g in enumerate(groups) if not q or q in g.title.lower()]


# ============================================================================
# Real senders (groups/channels and opted-in private messages)
# ============================================================================
CAPTION_LIMIT = 1024


async def deliver_via_bot(bot: Bot, chat_id: int, text: str, image, ext: str, markup=None) -> None:
    """Send text or image+text through the bot. Raises aiogram exceptions unchanged."""
    if image:
        photo = BufferedInputFile(image, filename=f"promo.{ext}")
        if len(text) <= CAPTION_LIMIT:
            await bot.send_photo(chat_id, photo, caption=text, reply_markup=markup)
        else:
            await bot.send_photo(chat_id, photo)
            await bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)


def map_bot_error(exc: Exception, *, private: bool) -> SendError:
    """Translate an aiogram exception into the engine's plain reasons."""
    if isinstance(exc, TelegramRetryAfter):
        return SendError("flood_wait", retry_after=float(exc.retry_after))
    if isinstance(exc, TelegramUnauthorizedError):
        return SendError("telegram_error", abort_all=True)
    if isinstance(exc, TelegramForbiddenError):
        return SendError("recipient_blocked_bot", unsubscribe=True) if private else SendError("no_permission")
    if isinstance(exc, TelegramBadRequest):
        text = str(exc).lower()
        if "message is too long" in text:
            return SendError("message_too_long")
        if "chat not found" in text:
            return SendError("recipient_unreachable", unsubscribe=True) if private else SendError("inaccessible")
        if "upgraded" in text or "migrat" in text:
            return SendError("inaccessible")
        if "rights" in text or "administrator" in text or "not allowed" in text:
            return SendError("no_permission")
        return SendError("bad_request")
    if isinstance(exc, TelegramNetworkError):
        return SendError("network_error")
    if isinstance(exc, TelegramAPIError):
        return SendError("telegram_error")
    raise exc


class BotGroupSender:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send(self, target: GroupInfo, campaign: Campaign) -> None:
        try:
            await deliver_via_bot(self._bot, target.chat_id, campaign.text, campaign.image, campaign.image_ext)
        except TelegramAPIError as exc:
            raise map_bot_error(exc, private=False) from None


class BotDMSender:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send(self, recipient_id: int, campaign: Campaign, brand: str) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔕 Unsubscribe", callback_data=f"unsub:{campaign.owner_id}")
        ]])
        footer = f"\n\n<i>Sent by {escape(brand)}. You subscribed to these messages.</i>"
        try:
            await deliver_via_bot(self._bot, recipient_id, campaign.text + footer,
                                  campaign.image, campaign.image_ext, markup)
        except TelegramAPIError as exc:
            raise map_bot_error(exc, private=True) from None


# ============================================================================
# Saved state kept in a private Telegram channel
# ============================================================================
log = logging.getLogger(__name__)
STATE_FILENAME = "promo-bot-state.json"


class TelegramPersister:
    label = "telegram"

    def __init__(self, bot: Bot, chat_id: int):
        self._bot, self._chat, self._msg_id = bot, chat_id, None

    async def load(self) -> Optional[dict]:
        """None = no snapshot yet (first run). Raises if Telegram can't be read (caller must not overwrite)."""
        chat = await self._bot.get_chat(self._chat)
        pinned = getattr(chat, "pinned_message", None)
        doc = getattr(pinned, "document", None)
        if not doc or getattr(doc, "file_name", None) != STATE_FILENAME:
            return None
        buf = io.BytesIO()
        await self._bot.download(doc, destination=buf)
        self._msg_id = pinned.message_id
        return json.loads(buf.getvalue().decode("utf-8"))

    async def save(self, state: dict) -> None:
        data = json.dumps(state, separators=(",", ":")).encode("utf-8")
        if self._msg_id is not None:
            try:
                await self._bot.edit_message_media(
                    media=InputMediaDocument(media=BufferedInputFile(data, filename=STATE_FILENAME)),
                    chat_id=self._chat, message_id=self._msg_id,
                )
                return
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                self._msg_id = None  # message was deleted: fall through and post a fresh snapshot
        msg = await self._bot.send_document(
            self._chat, BufferedInputFile(data, filename=STATE_FILENAME), disable_notification=True
        )
        self._msg_id = msg.message_id
        try:
            await self._bot.pin_chat_message(self._chat, msg.message_id, disable_notification=True)
        except TelegramAPIError as exc:
            log.warning("Could not pin the state snapshot (%s). Give the bot 'Pin messages' rights.", type(exc).__name__)


# ============================================================================
# The bot interface (menus, callbacks, user isolation, admin)
# ============================================================================
log = logging.getLogger(__name__)
PAGE_SIZE = 8


@dataclass
class AppContext:
    cfg: Config
    bot: Bot
    store: TemporaryStore
    persist: PersistentStore
    chats: ChatManager
    campaigns: CampaignManager
    limiter: security.RateLimiter
    started_at: float = field(default_factory=time.time)
    error_counts: Counter = field(default_factory=Counter)
    _username: Optional[str] = None

    async def bot_username(self) -> str:
        if self._username is None:
            self._username = (await self.bot.get_me()).username or ""
        return self._username


class Link(StatesGroup):
    ref = State()


class Draft(StatesGroup):
    title = State()
    message = State()
    image = State()


class Find(StatesGroup):
    query = State()


class Brand(StatesGroup):
    name = State()


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


HOME = ("🏠 Main menu", "menu:home")

MAIN_MENU = kb(
    [("🔗 LINK A GROUP / CHANNEL", "menu:link")],
    [("👤 MY ACCOUNT STATUS", "menu:status")],
    [("👥 MY GROUPS", "menu:groups")],
    [("📝 CREATE PROMOTION", "menu:create")],
    [("🖼 UPLOAD PROMOTIONAL IMAGE", "menu:image")],
    [("✍️ ENTER PROMOTIONAL MESSAGE", "menu:message")],
    [("👁 PREVIEW CAMPAIGN", "menu:preview")],
    [("🚀 START CAMPAIGN", "menu:start")],
    [("▶️ MY ACTIVE CAMPAIGNS", "menu:active")],
    [("📊 MY CAMPAIGN REPORTS", "menu:reports")],
    [("🛑 STOP CAMPAIGN", "menu:stop")],
    [("🧹 UNLINK ALL & CLEAR MY DATA", "menu:clear")],
    [("⚙️ MY SETTINGS", "menu:settings")],
    [("❓ HELP", "menu:help")],
)

HELP_TEXT = (
    "❓ <b>How this works</b>\n\n"
    "1️⃣ <b>Link</b> a group or channel: add this bot to it (as admin for channels), then pick it. "
    "I check that <i>you</i> are an owner/admin there. No login or phone number is needed.\n"
    "2️⃣ <b>My groups</b>: see what is linked and choose destinations.\n"
    "3️⃣ <b>Create promotion</b>: title, message (use Telegram's own bold/italic), optional image.\n"
    "4️⃣ <b>Preview</b>, then <b>Start</b> and confirm. Nothing is sent before you confirm.\n"
    "5️⃣ Watch <b>Active campaigns</b>, <b>Stop</b> any time, read your <b>Reports</b>.\n\n"
    "📣 Posts appear as sent by <b>this bot</b>.\n"
    "👥 <b>Private messages</b> go only to people who tapped <i>Subscribe</i> on your personal link "
    "(in MY SETTINGS). Every message has an Unsubscribe button.\n\n"
    "🔒 Your data is visible only to you. Campaign data lives in memory and is lost when the bot "
    "restarts or goes to sleep.\n\n"
    "Commands: /menu · /cancel · /unsubscribe"
)


class Guard(BaseMiddleware):
    """Runs before every handler: private chats only, suspension, spam limit, own workspace."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return None
        chat = event.chat if isinstance(event, Message) else getattr(event.message, "chat", None)
        if chat is not None and chat.type != "private":
            return None  # the bot works in private chat only
        uid = user.id
        if self.ctx.persist.is_suspended(uid):
            text = "⛔ Your access to this bot has been suspended."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return None
        if not self.ctx.limiter.allow(uid):
            if isinstance(event, CallbackQuery):
                await event.answer("Slow down a little 🙂", show_alert=False)
            return None
        data["ws"] = self.ctx.store.get(uid)
        return await handler(event, data)


def _ints(data: str, count: int) -> Optional[list[int]]:
    parts = data.split(":")[1:]
    if len(parts) < count:
        return None
    try:
        return [int(p) for p in parts[:count]]
    except ValueError:
        return None


def build_router(ctx: AppContext) -> Router:
    router = Router()
    guard = Guard(ctx)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)
    cfg = ctx.cfg

    # ------------------------------------------------------------- utilities
    async def show(event, text: str, markup=None) -> None:
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except TelegramAPIError:
                pass
            msg = event.message
            if not isinstance(msg, Message):
                await ctx.bot.send_message(event.from_user.id, text, reply_markup=markup)
                return
            try:
                await msg.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
            await msg.answer(text, reply_markup=markup)
        else:
            await event.answer(text, reply_markup=markup)

    async def alert(event, text: str) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)

    async def reset(state: FSMContext, uid: int) -> None:
        await state.clear()

    async def safe_delete(message: Message) -> None:
        try:
            await message.delete()
        except TelegramAPIError:
            pass

    async def notify_finished(uid: int, report) -> None:
        try:
            await ctx.bot.send_message(uid, format_report(report), reply_markup=kb([HOME]))
        except TelegramForbiddenError:
            pass

    # ---------------------------------------------------------- main menu
    async def m_home(event, ws: Workspace, state: FSMContext) -> None:
        uid = event.from_user.id
        lines = ["👋 <b>Promotion Manager</b>", ""]
        lines.append(f"Linked groups/channels: {len(ws.groups)}")
        if ws.running:
            lines.append(f"Campaign running: <b>{esc(ws.running.title)}</b>")
        elif ws.draft and ws.draft.title:
            lines.append(f"Draft in progress: <b>{esc(ws.draft.title)}</b>")
        lines.append("\nChoose an option:")
        await show(event, "\n".join(lines), MAIN_MENU)

    async def m_help(event, ws, state):
        await show(event, HELP_TEXT, kb([HOME]))

    # ------------------------------------------------- link groups / channels
    LINK_INTRO = (
        "🔗 <b>Link a group or channel</b>\n\n"
        "This bot posts from <b>its own account</b> (there is no personal login). So first add the bot:\n"
        "• <b>Group:</b> add the bot as a member that is allowed to send messages.\n"
        "• <b>Channel:</b> add the bot as an <b>admin</b> with permission to post messages.\n\n"
        "Then pick the chat below. I will check that <b>you</b> are an owner/admin there and that the bot "
        "can post. You can also type the chat's @username or numeric ID."
    )
    MAX_LINKED = 30

    async def restore_links(ws: Workspace, uid: int) -> None:
        """After a restart or Render sleep, rebuild THIS user's linked chats from the saved list.
        Saved links are only a convenience: ownership and the bot's rights are re-verified live
        with Telegram (getChatMember) before anything is shown as usable."""
        if ws.links_restored:
            return
        ws.links_restored = True
        have = {g.chat_id for g in ws.groups}
        saved = [d for d in ctx.persist.destinations_for(uid) if d["id"] not in have][:MAX_LINKED]
        infos = [GroupInfo(chat_id=d["id"], title=d["title"], kind=d["kind"]) for d in saved]
        gate = asyncio.Semaphore(5)

        async def one(info: GroupInfo) -> None:
            async with gate:
                await ctx.chats.recheck(uid, info)

        await asyncio.gather(*(one(i) for i in infos))
        ws.groups.extend(infos)

    async def m_link(event, ws, state):
        uname = await ctx.bot_username()
        inline = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add the bot to a group", url=f"https://t.me/{uname}?startgroup=link")],
            [InlineKeyboardButton(text="➕ Add the bot to a channel",
                                  url=f"https://t.me/{uname}?startchannel=link&admin=post_messages")],
            [InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home")],
        ])
        await show(event, LINK_INTRO, inline)
        await state.set_state(Link.ref)
        pick = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text="👥 Pick a group",
                               request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=False)),
                KeyboardButton(text="📣 Pick a channel",
                               request_chat=KeyboardButtonRequestChat(request_id=2, chat_is_channel=True)),
            ]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await ctx.bot.send_message(event.from_user.id, "Now pick the chat 👇\n(or type its @username / ID, or /cancel)",
                                   reply_markup=pick)

    async def link_chat(message: Message, ws: Workspace, ref) -> None:
        uid = message.from_user.id
        await restore_links(ws, uid)
        try:
            await ctx.bot.send_chat_action(uid, "typing")
        except TelegramAPIError:
            pass
        try:
            info = await ctx.chats.verify(uid, ref)  # proves YOU administer it, using YOUR Telegram id
        except ChatError as exc:
            await message.answer(f"⚠️ {esc(str(exc))}", reply_markup=ReplyKeyboardRemove())
            await message.answer("What next?", reply_markup=kb([("🔗 Try again", "menu:link")], [HOME]))
            return
        if cfg.storage_chat_id is not None and info.chat_id == cfg.storage_chat_id:
            await message.answer("⚠️ That chat stores the bot's own data and can't be used as a destination.",
                                 reply_markup=ReplyKeyboardRemove())
            return
        existing = next((g for g in ws.groups if g.chat_id == info.chat_id), None)
        if existing is not None:  # refresh in place so a draft keeps pointing at the same object
            existing.title, existing.kind, existing.username = info.title, info.kind, info.username
            existing.members, existing.can_post, existing.reason = info.members, info.can_post, info.reason
            info = existing
        elif len(ws.groups) >= MAX_LINKED:
            await message.answer(f"⚠️ You can link at most {MAX_LINKED} chats. Unlink one first.",
                                 reply_markup=ReplyKeyboardRemove())
            return
        else:
            ws.groups.append(info)
        idx = ws.groups.index(info)
        ctx.persist.add_destination(info.chat_id, info.title, info.kind, uid)  # survives restarts/sleep
        icon = "🟢" if info.can_post else "🔴"
        await message.answer(f"✅ Linked <b>{esc(info.title)}</b> ({info.kind})\n{icon} {esc(info.reason)}",
                             reply_markup=ReplyKeyboardRemove())
        rows = ([("➕ Use as destination", f"gu:{idx}:0")] if info.can_post else [])
        await message.answer("What next?", reply_markup=kb(rows, [("👥 My groups", "menu:groups")],
                                                          [("🔗 Link another", "menu:link")], [HOME]))

    @router.message(F.chat_shared)
    async def chat_shared(message: Message, state: FSMContext, ws: Workspace):
        await state.clear()
        await link_chat(message, ws, message.chat_shared.chat_id)

    @router.message(StateFilter(Link.ref), F.text)
    async def link_by_text(message: Message, state: FSMContext, ws: Workspace):
        ref = security.parse_chat_ref(message.text)
        if ref is None:
            await message.answer("⚠️ Send the chat's @username, its t.me link, or its numeric ID. (/cancel to stop)")
            return
        await state.clear()
        await link_chat(message, ws, ref)

    async def m_status(event, ws, state):
        u = event.from_user
        name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x) or "—"
        uname = f"@{esc(u.username)}" if getattr(u, "username", None) else "no username"
        lines = [
            "👤 <b>My account</b>",
            "<i>Telegram identifies you automatically. No login is needed.</i>",
            f"Name: {esc(name)}",
            f"Username: {uname}",
            f"Telegram ID: <code>{u.id}</code>",
            "",
            f"Linked groups/channels: {max(len(ws.groups), len(ctx.persist.destinations_for(u.id)))}",
            f"Running campaign: {'yes' if ws.running else 'no'}",
            f"Draft: {esc(ws.draft.title) if ws.draft and ws.draft.title else 'none'}",
            f"Finished campaigns this session: {len(ws.reports)}",
            f"Opt-in subscribers: {ctx.persist.count(u.id)}",
        ]
        await show(event, "\n".join(lines), kb([("🔗 Link a group/channel", "menu:link")], [HOME]))

    async def m_clear(event, ws, state):
        await show(
            event,
            "🧹 <b>Clear my data?</b>\n\nThis stops your running campaign and forgets your linked groups, "
            "draft and reports.\n\nYour sender name and subscriber list are kept "
            "(manage them in MY SETTINGS). To stop the bot posting in a chat, also remove the bot from that "
            "chat in Telegram.",
            kb([("✅ Yes, clear", "clr:yes")], [("↩️ Cancel", "menu:home")]),
        )

    @router.callback_query(F.data == "clr:yes")
    async def clear_yes(cb: CallbackQuery, ws: Workspace):
        ctx.campaigns.stop(ws)
        ws.groups.clear()
        ctx.persist.unlink_owner(cb.from_user.id)
        ws.draft = None
        ws.reports.clear()
        ws.group_query = ""
        await show(cb, "✅ Your linked groups, draft and reports were cleared.", kb([HOME]))

    # ---------------------------------------------------------------- groups
    async def render_groups(event, ws: Workspace, page: int) -> None:
        items = filter_groups(ws.groups, ws.group_query)
        pages = max(1, math.ceil(len(items) / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        chosen = {t.chat_id for t in ws.draft.targets} if ws.draft else set()
        rows = []
        for i, g in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            mark = "✅" if g.chat_id in chosen else ("🟢" if g.can_post else "⚠️")
            rows.append([(f"{mark} {g.title[:40]}", f"g:{i}:{page}")])
        if pages > 1:
            rows.append([("◀", f"gp:{page - 1}"), (f"{page + 1}/{pages}", f"gp:{page}"), ("▶", f"gp:{page + 1}")])
        rows.append([("🔎 Search", "gs"), ("🔄 Re-check all", "gr")] + ([("✖ Clear search", "gc")] if ws.group_query else []))
        rows.append([("🔗 Link another", "menu:link"), ("📍 Destinations", "d:dest")])
        rows.append([HOME])
        head = f"👥 <b>Your linked groups & channels</b> ({len(items)})"
        if ws.group_query:
            head += f"\nSearch: “{esc(ws.group_query)}”"
        legend = "\n✅ selected · 🟢 the bot can post · ⚠️ not confirmed (tap to see why)"
        await show(event, head + legend, kb(*rows))

    async def m_groups(event, ws, state):
        await restore_links(ws, event.from_user.id)
        if not ws.groups:
            await show(event, "You haven't linked any group or channel yet.",
                       kb([("🔗 Link one", "menu:link")], [HOME]))
            return
        ws.group_query = ""
        await render_groups(event, ws, 0)

    @router.callback_query(F.data.startswith("gp:"))
    async def groups_page(cb: CallbackQuery, ws: Workspace):
        vals = _ints(cb.data, 1)
        await render_groups(cb, ws, vals[0] if vals else 0)

    @router.callback_query(F.data == "gr")
    async def groups_refresh(cb: CallbackQuery, ws: Workspace):
        for g in list(ws.groups):
            await ctx.chats.recheck(cb.from_user.id, g)
        await render_groups(cb, ws, 0)

    @router.callback_query(F.data == "gc")
    async def groups_clear(cb: CallbackQuery, ws: Workspace):
        ws.group_query = ""
        await render_groups(cb, ws, 0)

    @router.callback_query(F.data == "gs")
    async def groups_search(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Find.query)
        await show(cb, "🔎 Type part of the group name to search for. (/cancel to stop)")

    @router.message(StateFilter(Find.query), F.text)
    async def groups_search_text(message: Message, state: FSMContext, ws: Workspace):
        ws.group_query = " ".join(message.text.split())[:50]
        await state.clear()
        await render_groups(message, ws, 0)

    def own_group(ws: Workspace, idx: Optional[int]):
        """Backend ownership check: an index only ever resolves inside THIS user's own list."""
        if idx is None or not 0 <= idx < len(ws.groups):
            return None
        return ws.groups[idx]

    async def render_group_detail(event, ws: Workspace, idx: int, page: int) -> None:
        info = ws.groups[idx]
        selected = bool(ws.draft and any(t.chat_id == info.chat_id for t in ws.draft.targets))
        vis = f"public (@{esc(info.username)})" if info.username else "private / no public link"
        members = str(info.members) if info.members else "not provided by Telegram"
        icon = "🟢" if info.can_post else "🔴"
        text = (
            f"<b>{esc(info.title)}</b>\nType: {info.kind}\nChat ID: <code>{info.chat_id}</code>\n"
            f"Visibility: {vis}\nMembers: {members}\n"
            f"Posting: {icon} {esc(info.reason)}\n\n"
            "ℹ️ Posts appear as sent by the bot. Telegram does not give a complete member list, so this "
            "tool never reads or contacts individual members."
        )
        toggle = ("➖ Remove from destinations", f"gu:{idx}:{page}") if selected else \
                 ("➕ Use as destination", f"gu:{idx}:{page}")
        await show(event, text, kb([toggle], [("🔄 Re-check", f"g:{idx}:{page}"), ("🗑 Unlink", f"gx:{idx}:{page}")],
                                   [("◀ Back to list", f"gp:{page}")]))

    @router.callback_query(F.data.startswith("g:"))
    async def group_open(cb: CallbackQuery, ws: Workspace):
        vals = _ints(cb.data, 2)
        info = own_group(ws, vals[0] if vals else None)
        if info is None:
            await alert(cb, "That list is out of date. Open MY GROUPS again.")
            return
        await ctx.chats.recheck(cb.from_user.id, info)
        await render_group_detail(cb, ws, vals[0], vals[1])

    @router.callback_query(F.data.startswith("gu:"))
    async def group_toggle(cb: CallbackQuery, ws: Workspace):
        vals = _ints(cb.data, 2)
        info = own_group(ws, vals[0] if vals else None)
        if info is None:
            await alert(cb, "That list is out of date. Open MY GROUPS again.")
            return
        draft = ws.get_draft()
        existing = [t for t in draft.targets if t.chat_id == info.chat_id]
        if existing:
            draft.targets.remove(existing[0])
        else:
            ok, _ = await ctx.chats.recheck(cb.from_user.id, info)
            if not ok:
                await render_group_detail(cb, ws, vals[0], vals[1])
                await alert(cb, "The bot can't post there (or you're no longer an admin), so it can't be a destination.")
                return
            if len(draft.targets) >= cfg.max_groups_per_campaign:
                await alert(cb, f"You can pick at most {cfg.max_groups_per_campaign} groups per campaign.")
                return
            draft.targets.append(info)
        draft.touch()
        await render_group_detail(cb, ws, vals[0], vals[1])

    @router.callback_query(F.data.startswith("gx:"))
    async def group_unlink(cb: CallbackQuery, ws: Workspace):
        vals = _ints(cb.data, 2)
        info = own_group(ws, vals[0] if vals else None)
        if info is None:
            await alert(cb, "That list is out of date. Open MY GROUPS again.")
            return
        ws.groups.remove(info)
        ctx.persist.unlink_destination(info.chat_id, cb.from_user.id)
        if ws.draft:
            ws.draft.targets = [t for t in ws.draft.targets if t.chat_id != info.chat_id]
            ws.draft.touch()
        if ws.groups:
            await render_groups(cb, ws, 0)
        else:
            await show(cb, "Unlinked. You have no linked groups now.", kb([("🔗 Link one", "menu:link")], [HOME]))

    # -------------------------------------------------------------- drafting
    def draft_panel(ws: Workspace):
        c = ws.get_draft()
        text = (
            "📝 <b>Campaign draft</b>\n"
            f"Title: {esc(c.title) if c.title else '—'}\n"
            f"Message: {str(len(c.text)) + ' characters' if c.text else '—'}\n"
            f"Image: {c.image_ext.upper() + ' attached' if c.image else 'none'}\n"
            f"Destinations: {esc(c.destination_label())}\n\n"
            "Nothing is sent until you press START and confirm."
        )
        markup = kb(
            [("✏️ Title", "d:title"), ("✍️ Message", "menu:message")],
            [("🖼 Image", "menu:image"), ("🗑 Remove image", "d:noimg")],
            [("📍 Destinations", "d:dest")],
            [("👁 Preview", "menu:preview"), ("🚀 Start", "menu:start")],
            [("🗑 Discard draft", "d:discard"), HOME],
        )
        return text, markup

    async def show_panel(event, ws: Workspace) -> None:
        text, markup = draft_panel(ws)
        await show(event, text, markup)

    async def m_create(event, ws, state):
        if ws.running:
            await show(event, "▶️ A campaign is running. Wait for it to finish or stop it before creating another.",
                       kb([("🛑 Stop it", "menu:stop")], [HOME]))
            return
        if ws.draft and (ws.draft.title or ws.draft.text):
            text, markup = draft_panel(ws)
            await show(event, "You already have a draft:\n\n" + text, markup)
            return
        ws.new_draft()
        await state.set_state(Draft.title)
        await state.update_data(wizard=True)
        await show(event, "📝 <b>New promotion</b>\n\nStep 1 of 3: send a short <b>title</b> for this campaign "
                          "(only you see it).\n\n/cancel to stop.")

    @router.callback_query(F.data == "d:panel")
    async def d_panel(cb: CallbackQuery, ws: Workspace, state: FSMContext):
        await reset(state, cb.from_user.id)
        await show_panel(cb, ws)

    @router.callback_query(F.data == "d:title")
    async def d_title(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Draft.title)
        await state.update_data(wizard=False)
        await show(cb, "✏️ Send the new title. (/cancel to stop)")

    @router.callback_query(F.data == "d:noimg")
    async def d_noimg(cb: CallbackQuery, ws: Workspace):
        c = ws.get_draft()
        c.image = None
        c.touch()
        await show_panel(cb, ws)

    @router.callback_query(F.data == "d:discard")
    async def d_discard(cb: CallbackQuery, ws: Workspace, state: FSMContext):
        await reset(state, cb.from_user.id)
        ws.draft = None
        await show(cb, "🗑 Draft discarded.", kb([HOME]))

    @router.message(StateFilter(Draft.title), F.text)
    async def got_title(message: Message, state: FSMContext, ws: Workspace):
        try:
            title = security.clean_title(message.text)
        except security.ValidationError as exc:
            await message.answer(f"⚠️ {esc(str(exc))}")
            return
        c = ws.get_draft()
        c.title = title
        c.touch()
        wizard = (await state.get_data()).get("wizard")
        if wizard:
            await state.set_state(Draft.message)
            await message.answer(MESSAGE_PROMPT.format(step="Step 2 of 3: "))
        else:
            await state.clear()
            await show_panel(message, ws)

    MESSAGE_PROMPT = (
        "✍️ {step}send the <b>promotional message</b>.\n\n"
        "Use Telegram's own formatting (select text → Bold / Italic / Link). "
        "Maximum 3,500 characters. Only promote what you are allowed to promote.\n\n/cancel to stop."
    )

    async def m_message(event, ws, state):
        if ws.running:
            await alert(event, "A campaign is running. Wait until it finishes.")
            return
        ws.get_draft()
        await state.set_state(Draft.message)
        await state.update_data(wizard=False)
        await show(event, MESSAGE_PROMPT.format(step=""))

    @router.message(StateFilter(Draft.message), F.text)
    async def got_message(message: Message, state: FSMContext, ws: Workspace):
        try:
            text = security.clean_message(message.html_text)
        except security.ValidationError as exc:
            await message.answer(f"⚠️ {esc(str(exc))}")
            return
        try:  # make sure Telegram can display it exactly as written
            await deliver_via_bot(ctx.bot, message.from_user.id, text, None, "jpg")
        except TelegramBadRequest:
            await message.answer("⚠️ Telegram could not display that formatting. Please send it again, "
                                 "using plain text or Telegram's own formatting buttons.")
            return
        c = ws.get_draft()
        c.text = text
        c.touch()
        wizard = (await state.get_data()).get("wizard")
        if wizard:
            await state.set_state(Draft.image)
            await message.answer("☝️ That's how your message will look.\n\n" + IMAGE_PROMPT.format(step="Step 3 of 3: "),
                                 reply_markup=kb([("⏭ Skip image", "d:skipimg")]))
        else:
            await state.clear()
            await message.answer("☝️ That's how your message will look.")
            await show_panel(message, ws)

    IMAGE_PROMPT = (
        "🖼 {step}send your <b>flyer or banner</b> as a photo (or as a file).\n"
        "Allowed: JPG, PNG, WEBP up to 5 MB.\n\n/cancel to stop."
    )

    async def m_image(event, ws, state):
        if ws.running:
            await alert(event, "A campaign is running. Wait until it finishes.")
            return
        ws.get_draft()
        await state.set_state(Draft.image)
        await state.update_data(wizard=False)
        await show(event, IMAGE_PROMPT.format(step=""), kb([("↩️ Back to draft", "d:panel")]))

    @router.callback_query(F.data == "d:skipimg")
    async def d_skipimg(cb: CallbackQuery, state: FSMContext, ws: Workspace):
        await state.clear()
        await show(cb, "Image skipped.")
        await cb.message.answer("Now choose where it should go 👇", reply_markup=kb(
            [("📍 Choose destinations", "d:dest")], [("📝 Draft overview", "d:panel")]))

    @router.message(StateFilter(Draft.image), F.photo | F.document)
    async def got_image(message: Message, state: FSMContext, ws: Workspace):
        if message.photo:
            obj = message.photo[-1]
        else:
            obj = message.document
            if not (obj.mime_type or "").startswith("image/"):
                await message.answer("⚠️ That file is not an image. Please send a JPG, PNG or WEBP.")
                return
        if obj.file_size and obj.file_size > cfg.max_image_bytes:
            await message.answer(f"⚠️ That image is larger than {cfg.max_image_bytes // (1024 * 1024)} MB.")
            return
        buf = io.BytesIO()
        try:
            await ctx.bot.download(obj, destination=buf)
        except TelegramAPIError:
            await message.answer("⚠️ I couldn't download that file. Please try again.")
            return
        data = buf.getvalue()
        found = security.sniff_image(data) if len(data) <= cfg.max_image_bytes else None
        if found is None:
            await message.answer("⚠️ Unsupported or damaged image. Please send a JPG, PNG or WEBP.")
            return
        c = ws.get_draft()
        c.image, c.image_ext = data, found[0]
        c.touch()
        wizard = (await state.get_data()).get("wizard")
        await state.clear()
        if wizard:
            await message.answer("✅ Image saved. Now choose where it should go 👇",
                                 reply_markup=kb([("📍 Choose destinations", "d:dest")], [("📝 Draft overview", "d:panel")]))
        else:
            await show_panel(message, ws)

    # ----------------------------------------------------------- destinations
    @router.callback_query(F.data == "d:dest")
    async def d_dest(cb: CallbackQuery, ws: Workspace, state: FSMContext):
        await reset(state, cb.from_user.id)
        uid = cb.from_user.id
        c = ws.get_draft()
        subs = ctx.persist.count(uid)
        lines = ["📍 <b>Destinations</b>", ""]
        lines.append("Groups/channels: " + (esc(", ".join(t.title for t in c.targets)) if c.targets else "none"))
        lines.append(f"Opt-in subscribers (private messages): {'ON' if c.include_subscribers else 'OFF'} — {subs} subscribed")
        await show(cb, "\n".join(lines), kb(
            [("📋 Choose groups / channels", "menu:groups")],
            [(("✅" if c.include_subscribers else "⬜") + " Opt-in subscribers", "d:subs")],
            [("📝 Done", "d:panel")],
        ))

    @router.callback_query(F.data == "d:subs")
    async def d_subs(cb: CallbackQuery, ws: Workspace, state: FSMContext):
        uid = cb.from_user.id
        c = ws.get_draft()
        if not c.include_subscribers and not ctx.persist.get_brand(uid):
            await alert(cb, "First set a sender name in MY SETTINGS. That also gives you your subscriber link.")
            return
        c.include_subscribers = not c.include_subscribers
        c.touch()
        await d_dest(cb, ws, state)

    # ---------------------------------------------------- preview / start
    async def send_preview(uid: int, ws: Workspace) -> bool:
        c = ws.draft
        await ctx.bot.send_message(uid, format_preview_summary(c, ctx.persist.count(uid)))
        try:
            await deliver_via_bot(ctx.bot, uid, c.text, c.image, c.image_ext)
        except TelegramBadRequest:
            await ctx.bot.send_message(uid, "⚠️ Telegram could not display this message. Please edit it.")
            return False
        if c.include_subscribers:
            await ctx.bot.send_message(
                uid, "ℹ️ Private messages also carry a short footer naming your sender name, "
                     "plus an Unsubscribe button.")
        return True

    async def m_preview(event, ws, state):
        uid = event.from_user.id
        c = ws.draft
        if c is None or not (c.title or c.text):
            await show(event, "You have no draft yet. Start with CREATE PROMOTION.",
                       kb([("📝 Create promotion", "menu:create")], [HOME]))
            return
        if not c.text:
            await show(event, "Add a promotional message first.", kb([("✍️ Message", "menu:message")], [HOME]))
            return
        try:
            await event.answer()
        except TelegramAPIError:
            pass
        if await send_preview(uid, ws):
            await ctx.bot.send_message(uid, "Looks right?", reply_markup=kb(
                [("🚀 Start campaign", "menu:start")], [("✏️ Edit draft", "d:panel")]))

    async def m_start(event, ws, state):
        uid = event.from_user.id
        if ws.running:
            await show(event, "▶️ You already have a campaign running.", kb([("▶️ Active campaigns", "menu:active")], [HOME]))
            return
        problems = ctx.campaigns.problems(ws)
        if problems:
            await show(event, "⚠️ <b>Not ready to start:</b>\n" + "\n".join(f"• {esc(p)}" for p in problems),
                       kb([("📝 Draft overview", "d:panel")], [HOME]))
            return
        try:
            await event.answer()
        except TelegramAPIError:
            pass
        if not await send_preview(uid, ws):
            return
        c = ws.draft
        await ctx.bot.send_message(
            uid,
            "🚨 <b>Confirm to start</b>\n"
            f"Campaign: {esc(c.title)}\nDestinations: {esc(c.destination_label())}\n\n"
            "Messages are paced to respect Telegram's limits, so large campaigns take time. "
            "You can stop at any moment.",
            reply_markup=kb([("✅ Confirm & start", f"cs:{c.id}:{c.revision}")], [("❌ Cancel", "cx")]),
        )

    @router.callback_query(F.data == "cx")
    async def confirm_cancel(cb: CallbackQuery):
        await show(cb, "Cancelled. Nothing was sent.", kb([("📝 Draft overview", "d:panel")], [HOME]))

    @router.callback_query(F.data.startswith("cs:"))
    async def confirm_start(cb: CallbackQuery, ws: Workspace):
        parts = cb.data.split(":")
        c = ws.draft
        try:
            rev = int(parts[2])
        except (IndexError, ValueError):
            rev = -1
        if c is None or len(parts) != 3 or parts[1] != c.id or rev != c.revision:
            await alert(cb, "This confirmation is out of date. Please preview and start again.")
            return
        bad = []
        for t in list(c.targets):  # rights can change after linking: verify again right now
            ok, _ = await ctx.chats.recheck(cb.from_user.id, t)
            if not ok:
                bad.append(t.title)
        if bad:
            await show(cb, "⚠️ I can no longer post in: " + esc(", ".join(bad))
                       + ".\nCheck the bot's rights and your admin role, then try again.",
                       kb([("👥 My groups", "menu:groups")], [HOME]))
            return
        sender = BotGroupSender(ctx.bot) if c.targets else None
        c.confirmed_rev = c.revision  # the user's explicit confirmation
        try:
            await ctx.campaigns.start(ws, sender, notify_finished)
        except CampaignError as exc:
            await show(cb, f"⚠️ {esc(str(exc))}", kb([HOME]))
            return
        await show(cb, "🚀 <b>Campaign started.</b>\nUse MY ACTIVE CAMPAIGNS to follow progress. "
                       "I'll send the report when it finishes.",
                   kb([("▶️ Active campaigns", "menu:active")], [HOME]))

    # -------------------------------------------------- active / stop / reports
    async def m_active(event, ws, state):
        c = ws.running
        if c is None:
            await show(event, "You have no running campaign.", kb([("🚀 Start one", "menu:start")], [HOME]))
            return
        await show(event, format_progress(c),
                   kb([("🔄 Refresh", "menu:active"), ("🛑 Stop", "menu:stop")], [HOME]))

    async def m_stop(event, ws, state):
        c = ws.running
        if c is None:
            await show(event, "You have no running campaign to stop.", kb([HOME]))
            return
        await show(event, f"🛑 Stop <b>{esc(c.title)}</b>?\nItems not yet processed will be skipped.",
                   kb([("✅ Yes, stop it", f"stop:{c.id}")], [("↩️ Keep running", "menu:active")]))

    @router.callback_query(F.data.startswith("stop:"))
    async def stop_yes(cb: CallbackQuery, ws: Workspace):
        c = ws.running
        if c is None or cb.data.split(":", 1)[1] != c.id:
            await alert(cb, "That campaign is not running any more.")
            return
        ctx.campaigns.stop(ws)
        await show(cb, "🛑 Stopping… you'll receive the final report in a moment.", kb([HOME]))

    async def m_reports(event, ws, state):
        if not ws.reports:
            await show(event, "No finished campaigns yet in this session.\n"
                              "(Reports are kept in memory and are cleared when the bot restarts.)", kb([HOME]))
            return
        rows = [[(f"{r.title[:28]} — {r.status}", f"rep:{i}")] for i, r in enumerate(ws.reports)]
        rows.append([HOME])
        await show(event, "📊 <b>Your campaign reports</b>", kb(*rows))

    @router.callback_query(F.data.startswith("rep:"))
    async def report_open(cb: CallbackQuery, ws: Workspace):
        vals = _ints(cb.data, 1)
        if vals is None or not 0 <= vals[0] < len(ws.reports):  # only this user's own reports
            await alert(cb, "That report is no longer available.")
            return
        await show(cb, format_report(ws.reports[vals[0]]), kb([("◀ Reports", "menu:reports")], [HOME]))

    # --------------------------------------------------------------- settings
    async def m_settings(event, ws, state):
        uid = event.from_user.id
        s = ws.settings
        brand = ctx.persist.get_brand(uid)
        lines = [
            "⚙️ <b>My settings</b>",
            f"Batch size: <b>{s.batch_size}</b> subscribers per batch",
            f"Delay between private messages: <b>{s.message_delay:g}s</b>",
            f"Pause between batches: <b>{s.batch_pause:g}s</b>",
            "<i>These are pacing preferences; Telegram's own limits always apply.</i>",
            "",
            f"Sender name: <b>{esc(brand) if brand else 'not set'}</b>",
            f"Subscribers: <b>{ctx.persist.count(uid)}</b>",
        ]
        if brand:
            lines.append(f"Your subscriber link:\nhttps://t.me/{await ctx.bot_username()}?start=s{uid}")
        else:
            lines.append("Set a sender name to get your subscriber link.")
        await show(event, "\n".join(lines), kb(
            [(f"Batch {n}", f"set:b:{n}") for n in (10, 30, 50, 100)],
            [(f"Delay {n}s", f"set:d:{n}") for n in (1, 2, 5)],
            [(f"Pause {n}s", f"set:p:{n}") for n in (15, 30, 60)],
            [("✏️ Sender name", "set:brand")],
            [HOME],
        ))

    @router.callback_query(F.data.startswith("set:"))
    async def settings_change(cb: CallbackQuery, ws: Workspace, state: FSMContext):
        parts = cb.data.split(":")
        if parts[1] == "brand":
            await state.set_state(Brand.name)
            await show(cb, "✏️ Send the name your subscribers will see (2–40 characters), e.g. your business name.")
            return
        allowed = {"b": (10, 30, 50, 100), "d": (1, 2, 5), "p": (15, 30, 60)}
        try:
            value = int(parts[2])
            kind = parts[1]
        except (IndexError, ValueError):
            await alert(cb, "Invalid setting.")
            return
        if kind not in allowed or value not in allowed[kind]:
            await alert(cb, "Invalid setting.")
            return
        if kind == "b":
            ws.settings.batch_size = value
        elif kind == "d":
            ws.settings.message_delay = float(value)
        else:
            ws.settings.batch_pause = float(value)
        await m_settings(cb, ws, state)

    @router.message(StateFilter(Brand.name), F.text)
    async def got_brand(message: Message, state: FSMContext, ws: Workspace):
        try:
            brand = security.clean_brand(message.text)
        except security.ValidationError as exc:
            await message.answer(f"⚠️ {esc(str(exc))}")
            return
        ctx.persist.set_brand(message.from_user.id, brand)
        await state.clear()
        await m_settings(message, ws, state)

    # ------------------------------------------------------- menu dispatcher
    MENU = {
        "home": m_home, "link": m_link, "status": m_status, "groups": m_groups,
        "create": m_create, "image": m_image, "message": m_message, "preview": m_preview,
        "start": m_start, "active": m_active, "reports": m_reports, "stop": m_stop,
        "clear": m_clear, "settings": m_settings, "help": m_help,
    }

    @router.callback_query(F.data.startswith("menu:"))
    async def menu_callback(cb: CallbackQuery, state: FSMContext, ws: Workspace):
        action = MENU.get(cb.data.split(":", 1)[1])
        if action is None:
            await alert(cb, "This button is no longer valid.")
            return
        await reset(state, cb.from_user.id)
        await action(cb, ws, state)

    # ------------------------------------------------------ opt-in subscribers
    @router.message(CommandStart(deep_link=True))
    async def start_deeplink(message: Message, command: CommandObject, state: FSMContext, ws: Workspace):
        await reset(state, message.from_user.id)
        arg = command.args or ""
        owner = security.parse_user_id(arg[1:]) if arg.startswith("s") else None
        brand = ctx.persist.get_brand(owner) if owner else None
        if not brand:
            await message.answer("That link is not valid any more.", reply_markup=MAIN_MENU)
            return
        await message.answer(
            f"📨 <b>{esc(brand)}</b> would like to send you promotional messages through this bot.\n\n"
            "You will only receive them if you tap Subscribe. You can unsubscribe at any time with the "
            "button under each message, or with /unsubscribe.",
            reply_markup=kb([("✅ Subscribe", f"sub:{owner}")], [("No thanks", "menu:home")]),
        )

    @router.callback_query(F.data.startswith("sub:"))
    async def subscribe(cb: CallbackQuery):
        vals = _ints(cb.data, 1)
        brand = ctx.persist.get_brand(vals[0]) if vals else None
        if not brand or not ctx.persist.subscribe(vals[0], cb.from_user.id):
            await alert(cb, "That link is not valid any more.")
            return
        await show(cb, f"✅ You're subscribed to messages from <b>{esc(brand)}</b>.\nSend /unsubscribe any time to stop.")

    @router.callback_query(F.data.startswith("unsub:"))
    async def unsubscribe_button(cb: CallbackQuery):
        vals = _ints(cb.data, 1)
        if vals:
            ctx.persist.unsubscribe(vals[0], cb.from_user.id)
        await cb.answer("Unsubscribed", show_alert=False)
        if isinstance(cb.message, Message):
            await cb.message.answer("🔕 You have been unsubscribed. You won't receive more messages from this sender.")

    @router.message(Command("unsubscribe"))
    async def unsubscribe_all(message: Message):
        n = ctx.persist.unsubscribe_all(message.from_user.id)
        await message.answer(f"🔕 Unsubscribed from {n} sender(s)." if n else "You had no active subscriptions.")

    # ----------------------------------------------------------------- commands
    @router.message(CommandStart())
    async def start_plain(message: Message, state: FSMContext, ws: Workspace):
        await reset(state, message.from_user.id)
        await message.answer("👋 Welcome! This bot helps you run promotion campaigns from your own Telegram account.\n"
                             "Everything is button-driven. Start with <b>CONNECT MY TELEGRAM ACCOUNT</b>, "
                             "or tap HELP.", reply_markup=MAIN_MENU)

    @router.message(Command("menu"))
    async def cmd_menu(message: Message, state: FSMContext, ws: Workspace):
        await reset(state, message.from_user.id)
        await m_home(message, ws, state)

    @router.message(Command("help"))
    async def cmd_help(message: Message, state: FSMContext):
        await reset(state, message.from_user.id)
        await message.answer(HELP_TEXT, reply_markup=kb([HOME]))

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        await reset(state, message.from_user.id)
        await message.answer("Cancelled.", reply_markup=kb([HOME]))

    # -------------------------------------------------------------------- admin
    def is_admin(event) -> bool:
        return security.is_admin(cfg.admin_id, event.from_user.id)

    def admin_text() -> str:
        stats = ctx.store.stats()
        up = int(time.time() - ctx.started_at)
        errors = ", ".join(f"{k}×{v}" for k, v in ctx.error_counts.most_common(5)) or "none"
        return (
            "🛠 <b>Platform status</b>\n"
            f"Uptime: {up // 3600}h {up % 3600 // 60}m\n"
            f"Users active this run: {stats['workspaces']}\n"
            f"Campaigns running: {ctx.campaigns.active} (limit {cfg.max_concurrent_campaigns})\n"
            f"Senders with opt-in lists: {ctx.persist.owners_count()}\n"
            f"Linked chats saved: {ctx.persist.destination_count()}\n"
            f"Storage: {ctx.persist.backend}"
            + (f" (saved {int(time.time() - ctx.persist.last_saved)}s ago)" if ctx.persist.last_saved else "")
            + ("  ⚠️ unsaved changes" if ctx.persist.pending else "") + "\n"
            f"Suspended users: {len(ctx.persist.list_suspended())}\n"
            f"Recent error types: {esc(errors)}\n\n"
            "Commands: /suspend &lt;id&gt; · /unsuspend &lt;id&gt;\n"
            "<i>The admin panel shows counts only, never anyone's campaigns, reports or messages.</i>"
        )

    ADMIN_KB = kb([("🔄 Refresh", "adm:status")], [("🚫 Suspended users", "adm:susp")], [HOME])

    @router.message(Command("admin"))
    async def cmd_admin(message: Message):
        if not is_admin(message):
            await message.answer("⛔ Not authorised.")
            return
        await message.answer(admin_text(), reply_markup=ADMIN_KB)

    @router.callback_query(F.data.startswith("adm:"))
    async def admin_cb(cb: CallbackQuery):
        if not is_admin(cb):
            await alert(cb, "⛔ Not authorised.")
            return
        if cb.data == "adm:susp":
            ids = ctx.persist.list_suspended()
            await show(cb, "🚫 <b>Suspended users</b>\n" + ("\n".join(f"<code>{i}</code>" for i in ids) or "none"),
                       ADMIN_KB)
        else:
            await show(cb, admin_text(), ADMIN_KB)

    @router.message(Command("suspend"))
    async def cmd_suspend(message: Message, command: CommandObject):
        if not is_admin(message):
            await message.answer("⛔ Not authorised.")
            return
        target = security.parse_user_id(command.args or "")
        if target is None:
            await message.answer("Usage: /suspend &lt;numeric user id&gt;")
            return
        if target == cfg.admin_id:
            await message.answer("The administrator can't be suspended.")
            return
        ctx.persist.suspend(target)
        ws = ctx.store.peek(target)
        if ws:
            ctx.campaigns.stop(ws)
        await message.answer(f"🚫 User <code>{target}</code> suspended and any running campaign stopped.")

    @router.message(Command("unsuspend"))
    async def cmd_unsuspend(message: Message, command: CommandObject):
        if not is_admin(message):
            await message.answer("⛔ Not authorised.")
            return
        target = security.parse_user_id(command.args or "")
        if target is None:
            await message.answer("Usage: /unsuspend &lt;numeric user id&gt;")
            return
        done = ctx.persist.unsuspend(target)
        await message.answer("✅ Access restored." if done else "That user was not suspended.")

    # ---------------------------------------------------------------- fallbacks
    @router.message(StateFilter(Link.ref, Draft.title,
                                Draft.message, Find.query, Brand.name))
    async def wrong_input_type(message: Message):
        await message.answer("⚠️ Please send text for this step, or /cancel to stop.")

    @router.message(StateFilter(Draft.image))
    async def wrong_image_input(message: Message):
        await message.answer("⚠️ Please send a photo or an image file (JPG, PNG or WEBP), or /cancel.")

    @router.message(StateFilter(None))
    async def unknown_message(message: Message):
        await message.answer("I didn't understand that. Please use the buttons below 👇", reply_markup=MAIN_MENU)

    @router.callback_query()
    async def unknown_callback(cb: CallbackQuery):
        await alert(cb, "This button is no longer valid. Open /menu.")

    @router.error()
    async def on_error(event: ErrorEvent):
        ctx.error_counts[type(event.exception).__name__] += 1
        log.error("Unhandled error: %s", type(event.exception).__name__, exc_info=event.exception)
        update = event.update
        chat_id = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query:
            chat_id = update.callback_query.from_user.id
        if chat_id:
            try:
                await ctx.bot.send_message(chat_id, "⚠️ Something went wrong on our side. Please try again, "
                                                    "or open /menu.")
            except TelegramAPIError:
                pass
        return True

    return router


# ============================================================================
# Entry point
# ============================================================================
ALLOWED_UPDATES = ["message", "callback_query"]
KEEP_AWAKE_SECONDS = 240


def main() -> None:
    load_dotenv()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)  # names variables, never values
        sys.exit(1)


    security.register_secret(cfg.bot_token)
    security.setup_logging(cfg.log_level)
    asyncio.run(run(cfg))


async def keep_awake(base_url: str, campaigns, stop: asyncio.Event) -> None:
    """Render's free plan sleeps a service after ~15 minutes without incoming web traffic,
    which would kill a running campaign. While (and only while) a campaign is running,
    request our own /health page every few minutes so the service stays up."""
    import aiohttp

    log = logging.getLogger("keep_awake")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=KEEP_AWAKE_SECONDS)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            if campaigns.active > 0:
                try:
                    async with session.get(base_url + "/health") as resp:
                        await resp.read()
                except Exception as exc:
                    log.warning("Keep-awake request failed: %s", type(exc).__name__)


async def build_persist(cfg, bot):
    """Load saved state. On any read problem, run WITHOUT saving so good data is never overwritten."""

    log = logging.getLogger("storage")
    if cfg.storage_chat_id is not None:

        try:
            store = await asyncio.wait_for(
                PersistentStore.load(TelegramPersister(bot, cfg.storage_chat_id), cfg.blocked_ids), timeout=20)
            log.info("Saved state loaded from the Telegram storage channel.")
            return store
        except Exception as exc:
            log.error("Could not read the storage channel (%s). Running WITHOUT saving so nothing is "
                      "overwritten. Check STORAGE_CHAT_ID and that the bot is an admin there.", type(exc).__name__)
            return PersistentStore(None, None, cfg.blocked_ids)
    log.warning("STORAGE_CHAT_ID is not set: opt-ins and linked chats are kept in %s and are lost whenever "
                "the service restarts or sleeps (Render Free).", cfg.data_dir)
    return PersistentStore.from_dir(cfg.data_dir, cfg.blocked_ids)


async def saver(persist, stop: asyncio.Event) -> None:
    """Saves changes in the background. Handlers never wait for storage, so replies stay fast.
    Bursts of changes are combined into one save a few seconds later."""
    log = logging.getLogger("saver")
    while not stop.is_set():
        try:
            await asyncio.wait_for(persist.changed.wait(), timeout=30)
        except asyncio.TimeoutError:
            continue
        persist.changed.clear()
        await asyncio.sleep(3)
        try:
            await persist.flush()
        except Exception as exc:  # flush() already swallows errors; this is belt and braces
            log.warning("Save failed: %s", type(exc).__name__)
        if persist.pending:  # a save failed: try again shortly instead of waiting for the next change
            await asyncio.sleep(15)
            persist.changed.set()


async def run(cfg) -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
    from aiogram.fsm.storage.memory import MemoryStorage


    log = logging.getLogger("app")
    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("Telegram rejected BOT_TOKEN. Create or revoke the token in @BotFather and update Render.")
        await bot.session.close()
        sys.exit(1)
    except TelegramAPIError as exc:
        log.error("Could not reach Telegram at startup: %s", type(exc).__name__)
        await bot.session.close()
        sys.exit(1)

    persist = await build_persist(cfg, bot)
    engine = DeliveryEngine(persist, BotDMSender(bot))
    campaigns = CampaignManager(cfg.max_concurrent_campaigns, persist, engine)
    ctx = AppContext(
        cfg=cfg, bot=bot, store=TemporaryStore(), persist=persist, chats=ChatManager(bot),
        campaigns=campaigns, limiter=security.RateLimiter(),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(ctx))

    saver_stop = asyncio.Event()
    saver_task = asyncio.create_task(saver(persist, saver_stop))

    async def cleanup() -> None:
        log.info("Shutting down: stopping campaigns and saving state.")
        await campaigns.shutdown()
        saver_stop.set()
        saver_task.cancel()
        await persist.flush()  # final save so opt-outs/links made in the last seconds are kept

    if not cfg.webhook_mode:
        log.info("Bot @%s starting in POLLING mode (no public URL configured).", me.username)
        dp.shutdown.register(cleanup)
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        finally:
            await bot.session.close()
        return

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler
    from aiohttp import web

    secret = security.webhook_secret(cfg.bot_token)
    path = f"/webhook/{secret[:16]}"

    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()

    # The webhook stays registered while the service sleeps, so Telegram's next call wakes it up.
    await bot.set_webhook(
        url=cfg.base_url + path, secret_token=secret,
        allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False,
    )
    log.info("Bot @%s running in WEBHOOK mode on port %d.", me.username, cfg.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass
    awake = asyncio.create_task(keep_awake(cfg.base_url, campaigns, stop))
    try:
        await stop.wait()
    finally:
        awake.cancel()
        await cleanup()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    main()
