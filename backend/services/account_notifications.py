"""A standard User's own notification configuration.

The global Notifications (``core.config`` + ``services.notification_service``)
stay the Admin's, instance-wide and unchanged. This module owns the SAME two
cards' configuration for one human User, durably keyed by the internal account
ID (``auth.accounts`` table ``account_notifications``):

* the document uses the global field names, so the one notification boundary
  (``services.notification_service``) answers participation, effective
  destination (the same-owner primary fallback), configuration and
  verification for it exactly as it does for the global document -- there is
  no second definition of any of them;
* nothing here ever reads a global destination: an unset personal field is
  unset, never inherited;
* every destination is an exact Discord webhook endpoint
  (``services.notifications.is_discord_webhook_url``), on save, on Test and on
  delivery, and the client built here posts only there, without following
  redirects;
* update notices and their check interval are instance facts and are not part
  of a personal document at all.

Delivery eligibility is decided at dispatch: an ACTIVE account whose role is
User, whose own outstanding demand the event concerns, with the section
participating and the event chosen.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from auth import accounts
from auth.accounts import Role
from services import notification_service as boundary
from services.notifications import NotificationService as DiscordNotificationClient
from services.notifications import is_discord_webhook_url

logger = logging.getLogger("debridpulse.notify")

DESTINATIONS = ("discord_webhook_url", "discord_webhook_added", "stats_report_webhook_url")
# How many personal webhook posts one dispatch keeps in flight at once. It
# bounds outbound concurrency only: every eligible owner is still delivered to.
MAX_CONCURRENT_SENDS = 8
# Owners per eligibility read, well inside SQLite's bound-parameter limit.
_ELIGIBILITY_BATCH = 500
# Bound one scheduler pass of personal reports.
MAX_REPORTS_PER_PASS = 20


class AccountNotificationSettings(BaseModel):
    """One User's notification document. New accounts take part in nothing."""

    model_config = ConfigDict(extra="ignore")

    discord_notifications_enabled: bool = False
    discord_webhook_url: str = ""
    discord_webhook_added: str = ""
    discord_username: str = ""
    discord_avatar_url: str = ""
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True
    discord_notify_extract: bool = True
    stats_reporting_enabled: bool = False
    stats_report_interval_hours: int = Field(default=0, ge=0, le=168)
    stats_report_window_hours: int = Field(default=24, ge=1, le=8760)
    stats_report_webhook_url: str = ""
    notification_verification: dict[str, str] = Field(default_factory=dict)


class AccountNotificationUpdate(BaseModel):
    """What a User may change: exactly the personal fields, nothing else.

    Unknown keys -- a global option, an account ID, a role, an update policy
    -- are refused, not ignored. A blank destination keeps the stored one; a
    destination is erased only by naming it in ``clear_secrets``.
    """

    model_config = ConfigDict(extra="forbid")

    discord_notifications_enabled: bool | None = None
    discord_webhook_url: str | None = Field(default=None, max_length=512)
    discord_webhook_added: str | None = Field(default=None, max_length=512)
    discord_username: str | None = Field(default=None, max_length=80)
    discord_avatar_url: str | None = Field(default=None, max_length=2048)
    discord_notify_added: bool | None = None
    discord_notify_finished: bool | None = None
    discord_notify_error: bool | None = None
    discord_notify_extract: bool | None = None
    stats_reporting_enabled: bool | None = None
    stats_report_interval_hours: int | None = Field(default=None, ge=0, le=168)
    stats_report_window_hours: int | None = Field(default=None, ge=1, le=8760)
    stats_report_webhook_url: str | None = Field(default=None, max_length=512)
    clear_secrets: list[str] = Field(default_factory=list, max_length=len(DESTINATIONS))


class NotificationRefused(ValueError):
    """A personal notification change or action is not acceptable."""


def _avatar_acceptable(url: str) -> bool:
    """A sender avatar is a public HTTPS image URL that Discord itself fetches.

    DebridPulse never fetches it, so it cannot become a server-side request;
    the bounds here keep the stored value a plain URL and nothing more."""
    if not url:
        return True
    if url != url.strip() or not url.isascii() or any(ord(ch) < 0x21 for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.hostname) and not parts.username
            and parts.password is None and port in (None, 443) and not url.lower().endswith(".svg"))


# ── storage ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _transaction():
    from db.database import get_db

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            await db.rollback()
            raise
        await db.commit()


def _document(raw) -> AccountNotificationSettings:
    try:
        return AccountNotificationSettings.model_validate(json.loads(raw or "{}"))
    except Exception:
        # A document that cannot be read takes part in nothing.
        return AccountNotificationSettings()


async def _read(db, account_id: str) -> AccountNotificationSettings:
    row = await db.fetchone("SELECT settings FROM account_notifications WHERE account_id=?", (str(account_id),))
    return _document(row["settings"] if row else None)


async def _write(db, account_id: str, settings: AccountNotificationSettings) -> None:
    await db.execute(
        "INSERT INTO account_notifications(account_id,settings,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET settings=excluded.settings, updated_at=excluded.updated_at",
        (str(account_id), settings.model_dump_json(),
         datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))


async def load(account_id: str) -> AccountNotificationSettings:
    from db.database import get_db

    async with get_db() as db:
        return await _read(db, account_id)


async def update(account_id: str, change: AccountNotificationUpdate) -> AccountNotificationSettings:
    """Apply one User's change to their own document, atomically.

    A supplied destination is authoritative (and must be a Discord webhook);
    a named clear erases one; a blank keeps what is stored. Verification
    evidence that no longer describes the saved material is retired by the
    boundary's own rule."""
    clears = set(change.clear_secrets)
    if not clears <= set(DESTINATIONS):
        raise NotificationRefused("Only a stored webhook can be cleared")
    supplied = change.model_dump(exclude_none=True, exclude={"clear_secrets"})
    for field in DESTINATIONS:
        value = str(supplied.get(field) or "").strip()
        if value and not is_discord_webhook_url(value):
            raise NotificationRefused("Enter a Discord webhook URL (https://discord.com/api/webhooks/…)")
        if field in supplied:
            supplied[field] = value
    if "discord_avatar_url" in supplied:
        supplied["discord_avatar_url"] = str(supplied["discord_avatar_url"]).strip()
        if not _avatar_acceptable(supplied["discord_avatar_url"]):
            raise NotificationRefused("The avatar must be a public HTTPS image URL (PNG, JPG, GIF or WEBP)")
    if "discord_username" in supplied:
        supplied["discord_username"] = str(supplied["discord_username"]).strip()

    async with _transaction() as db:
        current = await _read(db, account_id)
        merged = current.model_dump()
        for field, value in supplied.items():
            if field in DESTINATIONS and not value:
                continue
            merged[field] = value
        for field in DESTINATIONS:
            if field in clears and not supplied.get(field):
                merged[field] = ""
        saved = boundary.carried_verification(AccountNotificationSettings.model_validate(merged))
        await _write(db, account_id, saved)
    return saved


async def record_verification(account_id: str, subject: str, fingerprint: str, ok: bool) -> AccountNotificationSettings:
    """Commit or retire Test evidence for the account's CURRENT saved material."""
    async with _transaction() as db:
        current = await _read(db, account_id)
        updated = boundary.record_verification_outcome(current, subject, fingerprint, ok)
        if updated is None:
            return current
        await _write(db, account_id, updated)
    return updated


def public(settings: AccountNotificationSettings) -> dict:
    """The secret-free projection the User's page reads: destinations are
    write-only (blank plus ``<field>_configured``), status is derived."""
    document = settings.model_dump(exclude={"notification_verification"})
    for field in DESTINATIONS:
        document[f"{field}_configured"] = bool(document[field])
        document[field] = ""
    return {**document, **boundary.notification_state(settings)}


# ── delivery ────────────────────────────────────────────────────────────────

def client(settings: AccountNotificationSettings) -> DiscordNotificationClient:
    """The account's own Discord client: its destinations, its identity, Discord
    webhooks only, no redirects. A non-participating section has none."""
    if not boundary.discord_participates(settings):
        return DiscordNotificationClient(webhook_url="", added_webhook_url="", discord_only=True, dedup=False)
    return DiscordNotificationClient(
        webhook_url=boundary.discord_destination(settings),
        added_webhook_url=str(settings.discord_webhook_added or ""),
        identity=(settings.discord_username, settings.discord_avatar_url),
        discord_only=True,
        dedup=False,
    )


def sender(settings: AccountNotificationSettings, destination: str) -> DiscordNotificationClient:
    """A client for one of the account's OWN effective destinations (a Test, or
    a report), carrying its identity; participation is not consulted."""
    return DiscordNotificationClient(
        webhook_url=destination,
        identity=(settings.discord_username, settings.discord_avatar_url),
        discord_only=True,
        dedup=False,
    )


# Documents whose account may use them now: an active (enabled, neither
# removed nor merged away) account whose role is User.
_ELIGIBLE = """FROM account_notifications n JOIN accounts a ON a.id=n.account_id
    WHERE a.role='user' AND a.enabled=1 AND a.deleted_at IS NULL AND a.merged_into IS NULL"""


async def recipients(demand_rows) -> list[tuple[str, AccountNotificationSettings]]:
    """The eligible Users whose OWN outstanding demand is in ``demand_rows``.

    Ownership is the demand ledger's, through merge redirection
    (``canonical_account_id``); a row with no recorded owner (legacy or
    unattributed) names nobody. Each account appears once, however many of
    its requests the event concerns."""
    recorded = dict.fromkeys(str(row["account_id"]) for row in demand_rows
                             if row.get("withdrawn_at") is None and row.get("account_id"))
    owners: list[str] = []
    for account_id in recorded:
        owner = await accounts.canonical_account_id(account_id)
        if owner and owner not in owners:
            owners.append(owner)
    if not owners:
        return []
    from db.database import get_db

    # Every owner is evaluated: the population entitled to delivery is never
    # truncated (``MAX_CONCURRENT_SENDS`` bounds only how they are sent).
    found: dict[str, AccountNotificationSettings] = {}
    async with get_db() as db:
        for start in range(0, len(owners), _ELIGIBILITY_BATCH):
            batch = owners[start:start + _ELIGIBILITY_BATCH]
            marks = ",".join("?" * len(batch))
            rows = await db.fetchall(
                f"SELECT n.account_id, n.settings {_ELIGIBLE} AND n.account_id IN ({marks})", batch)
            found.update((str(row["account_id"]), _document(row["settings"])) for row in rows)
    return [(owner, found[owner]) for owner in owners
            if owner in found and boundary.discord_participates(found[owner])]


async def eligible(account_id: str) -> bool:
    """Whether the account may use its personal notifications at all now."""
    account = await accounts.get_account(account_id)
    return bool(account is not None and account.active and account.role is Role.USER)


# ── scheduled personal reports ──────────────────────────────────────────────

async def due_reports(now: float | None = None) -> list[tuple[str, AccountNotificationSettings]]:
    """Personal reports whose own cadence has elapsed, bounded per pass.

    An account seen with a cadence for the first time starts its interval now
    (as the global schedule does), so enabling never sends at once."""
    from db.database import get_db

    now = time.time() if now is None else float(now)
    due: list[tuple[str, AccountNotificationSettings]] = []
    async with get_db() as db:
        rows = await db.fetchall(f"SELECT n.account_id, n.settings, n.last_report_at {_ELIGIBLE} "
                                 "ORDER BY COALESCE(n.last_report_at, 0), n.account_id")
        started = []
        for row in rows:
            settings = _document(row["settings"])
            interval = int(settings.stats_report_interval_hours or 0)
            if (not boundary.reporting_participates(settings) or interval <= 0
                    or not boundary.reporting_configured(settings)):
                continue
            last = row["last_report_at"]
            if last is None:
                started.append(str(row["account_id"]))
            elif now - float(last) >= interval * 3600 and len(due) < MAX_REPORTS_PER_PASS:
                due.append((str(row["account_id"]), settings))
        for account_id in started:
            await db.execute("UPDATE account_notifications SET last_report_at=? WHERE account_id=?",
                             (now, account_id))
        if started:
            await db.commit()
    return due


async def mark_reported(account_id: str, at: float | None = None) -> None:
    from db.database import get_db

    async with get_db() as db:
        await db.execute("UPDATE account_notifications SET last_report_at=? WHERE account_id=?",
                         (time.time() if at is None else float(at), str(account_id)))
        await db.commit()
