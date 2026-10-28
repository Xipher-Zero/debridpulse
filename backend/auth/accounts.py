"""DebridPulse accounts: the ONE owner of durable identity.

An account is the stable, immutable identity every sign-in resolves to. Its
three facts are kept apart: the ROLE (``accounts.role``), the zero-or-one LOCAL
CREDENTIAL (``account_local_credentials``) and the zero-or-more linked OIDC
IDENTITIES (``account_oidc_bindings``, keyed by the validated ``(issuer,
subject)``). A username or an email address is never identity: a local sign-in
finds the account through its credential row, an OIDC sign-in through its
binding, and email is display metadata only -- it never links, merges or
grants anything.

Two protected default identities always exist, with well-known IDs: the
default **Admin** (no password until the operator sets one) and the default
**Automation** Integration account (no token). The default Admin is the
standalone no-authentication principal and the account the offline
configuration-file credential recovers (``import_legacy_credential``), so it is
never deleted, disabled, demoted or merged away.

Every mutation is one ``BEGIN IMMEDIATE`` transaction that re-checks, under the
authentication configuration lock, that a usable Admin remains
(``usable_admin_exists``), bumps the account's ``generation`` and, after it
commits, revokes every session of the accounts it changed: the session store
never holds a session whose role, status or credential is older than the
account's current state.

Accounts are configuration, not transfer history: the explicit database wipe
keeps them; a restore replaces them with the backup's, and is refused when the
restored state would leave no usable Admin (``restored_state_has_usable_admin``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

from auth.passwords import (
    basic_verification_cache,
    hash_password,
    is_usable_password_hash,
    password_credential_version,
)
from auth.policy import interactive_auth_enabled, oidc_auth_enabled, password_auth_enabled


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"
    READONLY = "readonly"
    INTEGRATION = "integration"


HUMAN_ROLES = frozenset({Role.ADMIN, Role.USER, Role.READONLY})
# The roles Authentication settings may give an account created by its first
# admitted OIDC sign-in. Admin is never provisioned automatically.
OIDC_PROVISION_ROLES = frozenset({Role.USER, Role.READONLY})

DEFAULT_ADMIN_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_INTEGRATION_ID = "00000000-0000-4000-8000-000000000002"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        role TEXT NOT NULL CHECK (role IN ('admin','user','readonly','integration')),
        origin TEXT NOT NULL CHECK (origin IN ('local','oidc','integration')),
        display_name TEXT NOT NULL DEFAULT '',
        actual_name TEXT,
        email TEXT NOT NULL DEFAULT '',
        email_verified INTEGER NOT NULL DEFAULT 0,
        email_verified_known INTEGER NOT NULL DEFAULT 0,
        email_self_entered INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        protected TEXT UNIQUE CHECK (protected IN ('default_admin','default_integration')),
        generation INTEGER NOT NULL DEFAULT 1,
        merged_into TEXT REFERENCES accounts(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS account_local_credentials (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS account_oidc_bindings (
        issuer TEXT NOT NULL,
        subject TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        created_at TEXT NOT NULL,
        PRIMARY KEY (issuer, subject)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_account_oidc_bindings_account ON account_oidc_bindings (account_id)",
    # Integration accounts' API tokens. Only a SHA-256 verifier of a 256-bit
    # random secret is stored; a revoked token keeps its row (and label) so
    # every request it carried stays attributable. ``client_id`` is the
    # external client the token authenticates as (``external``): a new token
    # is a new client; only an Admin's explicit rotation (``create_token``
    # with ``succeeds``) gives a token its predecessor's client.
    """CREATE TABLE IF NOT EXISTS account_tokens (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        label TEXT NOT NULL,
        verifier TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        client_id TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_account_tokens_account ON account_tokens (account_id)",
    # A User's own notification configuration (``services.account_notifications``):
    # one document per account, keyed by the durable account ID and never by a
    # name, email, token or OIDC subject. It belongs to exactly that account:
    # Remove and Merge retire it with the account, and nothing inherits it.
    """CREATE TABLE IF NOT EXISTS account_notifications (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        settings TEXT NOT NULL,
        last_report_at REAL,
        updated_at TEXT NOT NULL
    )""",
    # A human account's own appearance (``services.account_appearance``): its
    # interface mode and the palette it chose for each mode, keyed by the
    # durable account ID. It belongs to exactly that account: Remove and Merge
    # retire it with the account, and nothing inherits it.
    """CREATE TABLE IF NOT EXISTS account_appearance (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        mode TEXT NOT NULL CHECK (mode IN ('dark','light')),
        dark_palette TEXT NOT NULL,
        light_palette TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)
TABLES = ("accounts", "account_local_credentials", "account_oidc_bindings", "account_tokens",
          "account_notifications", "account_appearance")
TOKEN_PREFIX = "dp_"
_TOKEN_RANDOM_BYTES = 32


class AccountRefused(Exception):
    """A requested account change would break an account invariant."""


class InvalidEmail(AccountRefused):
    """A self-entered email address that is not a usable address."""


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    role: Role
    origin: str
    display_name: str
    email: str
    email_verified: bool
    enabled: bool
    protected: str
    generation: int
    merged_into: str
    deleted_at: str
    username: str = ""
    credential_version: str = ""
    # The person's name, and whether the identity provider STATED the email's
    # verification (``email_verified`` holds what it stated). Profile only:
    # neither identifies, links or authorizes anything.
    actual_name: str = ""
    email_verified_known: bool = False
    # The account entered the email itself (``set_own_email``): it is recorded
    # unverified and no identity provider's claim ever verifies it.
    email_self_entered: bool = False

    @property
    def active(self) -> bool:
        return self.enabled and not self.deleted_at and not self.merged_into

    @property
    def has_password(self) -> bool:
        return bool(self.credential_version)


def admitted(account: Account | None) -> bool:
    """Whether a resolved account may be signed in: it exists and is active.
    What it may then do is its role's (``auth.permissions``)."""
    return bool(account is not None and account.active)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def ensure_schema(db) -> None:
    """Schema and the two protected default identities (``db.database.init_db``)."""
    from db.database import _ensure_column

    for statement in SCHEMA:
        await db.execute(statement)
    # NULL: never populated (an identity's claims may complete it); a string,
    # even an empty one, is the account's own choice and is never overwritten.
    await _ensure_column(db, "accounts", "actual_name", "TEXT")
    await _ensure_column(db, "accounts", "email_verified_known", "INTEGER NOT NULL DEFAULT 0")
    # An email recorded as verified was recorded only from an explicit
    # ``email_verified: true`` claim, so its verification is known.
    await db.execute("UPDATE accounts SET email_verified_known=1 WHERE email_verified=1 AND email_verified_known=0")
    # Before an account could enter its own email, every email came from an
    # identity provider's claim.
    await _ensure_column(db, "accounts", "email_self_entered", "INTEGER NOT NULL DEFAULT 0")
    # Every token issued before external clients existed was its own client.
    await _ensure_column(db, "account_tokens", "client_id", "TEXT")
    await db.execute("UPDATE account_tokens SET client_id=id WHERE client_id IS NULL")
    now = _now()
    await db.execute(
        "INSERT OR IGNORE INTO accounts(id,role,origin,display_name,protected,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (DEFAULT_ADMIN_ID, Role.ADMIN.value, "local", "Administrator", "default_admin", now, now),
    )
    await db.execute(
        "INSERT OR IGNORE INTO accounts(id,role,origin,display_name,protected,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (DEFAULT_INTEGRATION_ID, Role.INTEGRATION.value, "integration", "Automation",
         "default_integration", now, now),
    )


_ACCOUNT_SELECT = (
    "SELECT a.*, COALESCE(c.username,'') AS username, COALESCE(c.password_hash,'') AS password_hash "
    "FROM accounts a LEFT JOIN account_local_credentials c ON c.account_id = a.id"
)


def _account(row: Mapping[str, Any] | None) -> Account | None:
    if row is None:
        return None
    password_hash = str(row.get("password_hash") or "")
    return Account(
        id=str(row["id"]),
        role=Role(row["role"]),
        origin=str(row["origin"]),
        display_name=str(row.get("display_name") or ""),
        email=str(row.get("email") or ""),
        email_verified=bool(row.get("email_verified")),
        enabled=bool(row.get("enabled")),
        protected=str(row.get("protected") or ""),
        generation=int(row.get("generation") or 0),
        merged_into=str(row.get("merged_into") or ""),
        deleted_at=str(row.get("deleted_at") or ""),
        username=str(row.get("username") or ""),
        credential_version=(
            password_credential_version(password_hash) if is_usable_password_hash(password_hash) else ""
        ),
        actual_name=str(row.get("actual_name") or ""),
        email_verified_known=bool(row.get("email_verified_known")),
        email_self_entered=bool(row.get("email_self_entered")),
    )


async def _fetch(db, account_id: str) -> Account | None:
    return _account(await db.fetchone(f"{_ACCOUNT_SELECT} WHERE a.id=?", (str(account_id or ""),)))


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


# ── reads ───────────────────────────────────────────────────────────────────

async def get_account(account_id: str) -> Account | None:
    from db.database import get_db

    async with get_db() as db:
        return await _fetch(db, account_id)


async def canonical_account_id(account_id: str) -> str:
    """The current account an historical account ID now means: a merged
    account redirects to the account it was merged into; every other ID,
    deleted ones included, means itself. Historical rows are never rewritten."""
    from db.database import get_db

    current = str(account_id or "")
    seen: set[str] = set()
    async with get_db() as db:
        while current and current not in seen:
            seen.add(current)
            row = await db.fetchone("SELECT merged_into FROM accounts WHERE id=?", (current,))
            if row is None or not row["merged_into"]:
                return current
            current = str(row["merged_into"])
    return current


async def local_credential(username: str) -> tuple[Account, str] | None:
    """The active account owning ``username`` and its stored verifier."""
    from db.database import get_db

    async with get_db() as db:
        row = await db.fetchone(f"{_ACCOUNT_SELECT} WHERE c.username=?", (str(username or ""),))
    account = _account(row)
    if account is None or not account.active or not account.has_password:
        return None
    return account, str(row["password_hash"])


# ── the last-usable-Admin invariant ─────────────────────────────────────────

def _oidc_issuer(settings) -> str:
    if not oidc_auth_enabled(settings):
        return ""
    from auth.oidc import OidcConfigurationError, oidc_configuration

    try:
        return oidc_configuration(settings).issuer
    except OidcConfigurationError:
        return ""


def usable_admin_exists(admins: Iterable[tuple[bool, frozenset[str]]], settings) -> bool:
    """Whether some active Admin can still sign in under ``settings``.

    ``admins`` holds, per active Admin account, whether it has a usable local
    password and the OIDC issuers it is bound under. Without interactive
    authentication the installation is standalone: its one principal is the
    default Admin, so an Admin is always usable."""
    if not interactive_auth_enabled(settings):
        return True
    password = password_auth_enabled(settings)
    issuer = _oidc_issuer(settings)
    return any((password and has_password) or (issuer and issuer in issuers) for has_password, issuers in admins)


_ADMIN_FACTS = (
    "SELECT a.id, COALESCE(c.password_hash,'') AS password_hash, b.issuer "
    "FROM accounts a LEFT JOIN account_local_credentials c ON c.account_id = a.id "
    "LEFT JOIN account_oidc_bindings b ON b.account_id = a.id "
    "WHERE a.role='admin' AND a.enabled=1 AND a.deleted_at IS NULL AND a.merged_into IS NULL"
)


def _admin_facts(rows) -> dict[str, tuple[bool, frozenset[str]]]:
    facts: dict[str, tuple[bool, set[str]]] = {}
    for account_id, password_hash, issuer in rows:
        has_password, issuers = facts.setdefault(str(account_id), (is_usable_password_hash(password_hash), set()))
        if issuer:
            issuers.add(str(issuer))
    return {key: (has_password, frozenset(issuers)) for key, (has_password, issuers) in facts.items()}


async def _usable_admin(db, settings) -> bool:
    rows = await db.fetchall(_ADMIN_FACTS)
    facts = _admin_facts((row["id"], row["password_hash"], row["issuer"]) for row in rows)
    return usable_admin_exists(facts.values(), settings)


async def usable_admin_present(settings) -> bool:
    from db.database import get_db

    async with get_db() as db:
        return await _usable_admin(db, settings)


async def admin_password_holders() -> set[str]:
    """Active Admin accounts holding a usable local password."""
    from db.database import get_db

    async with get_db() as db:
        rows = await db.fetchall(_ADMIN_FACTS)
    facts = _admin_facts((row["id"], row["password_hash"], row["issuer"]) for row in rows)
    return {account_id for account_id, (has_password, _issuers) in facts.items() if has_password}


def _legacy_credential(document: Mapping[str, Any]) -> tuple[str, str]:
    username = str(document.get("auth_username") or "").strip()
    password_hash = str(document.get("auth_password_hash") or "").strip()
    return username, (password_hash if is_usable_password_hash(password_hash) else "")


def restored_state_has_usable_admin(conn: sqlite3.Connection, document: Mapping[str, Any]) -> bool:
    """Whether a restore point's state, once started, leaves a usable Admin.

    ``conn`` reads the restore point's (or its staged copy's) database and
    ``document`` is its configuration. A database from before accounts existed
    holds only what startup will create: the default Admin, given the
    configuration's own pre-account credential (``import_legacy_credential``)."""
    from core.config import validate_settings_document

    settings = validate_settings_document(dict(document))
    _username, legacy_hash = _legacy_credential(document)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "accounts" in tables:
        facts = _admin_facts(conn.execute(_ADMIN_FACTS).fetchall())
    else:
        facts = {DEFAULT_ADMIN_ID: (False, frozenset())}
    if legacy_hash:
        _has_password, issuers = facts.get(DEFAULT_ADMIN_ID, (False, frozenset()))
        facts[DEFAULT_ADMIN_ID] = (True, issuers)
    return usable_admin_exists(facts.values(), settings)


# ── mutation core ───────────────────────────────────────────────────────────

async def _touch(db, account_ids: Iterable[str]) -> None:
    now = _now()
    for account_id in account_ids:
        await db.execute(
            "UPDATE accounts SET generation = generation + 1, updated_at=? WHERE id=?", (now, account_id),
        )


async def _rebind(account_ids: Iterable[str]) -> None:
    """A change of authority only (a role): the accounts' live sessions are
    re-bound to their current state instead of ended."""
    from auth.sessions import session_store

    for account_id in account_ids:
        account = await get_account(account_id)
        if account is None or not account.active:
            _revoke([account_id])
        else:
            session_store.rebind_account(account)


def _revoke(account_ids: Iterable[str]) -> None:
    from auth.sessions import session_store

    for account_id in account_ids:
        session_store.revoke_account(account_id)
    basic_verification_cache.clear()


@asynccontextmanager
async def _mutation(touched: list[str], settings=None, persist=None, *, rebind: bool = False):
    """One account change: a transaction that must not be the change that
    leaves no usable Admin under ``settings`` (the live authentication
    settings unless the same critical section is about to commit others).

    ``persist`` writes those other settings. It runs inside the still-open
    transaction, after every check, so a failed write rolls the account change
    back and nothing is changed anywhere; the caller undoes the write if the
    transaction then fails to commit.

    The caller holds ``authentication_configuration_lock``, which serializes
    this change against every other account change and every authentication
    setting the invariant is evaluated under."""
    from core.config import get_settings

    current = get_settings()
    async with _transaction() as db:
        had_usable_admin = await _usable_admin(db, current)
        yield db
        if had_usable_admin and not await _usable_admin(db, settings if settings is not None else current):
            raise AccountRefused("At least one usable Admin account must remain")
        await _touch(db, touched)
        if persist is not None:
            persist()
    if rebind:
        await _rebind(touched)
    else:
        _revoke(touched)


@asynccontextmanager
async def _locked_mutation(touched: list[str], *, rebind: bool = False):
    from auth.transitions import authentication_configuration_lock

    async with authentication_configuration_lock:
        async with _mutation(touched, rebind=rebind) as db:
            yield db


async def _require(db, account_id: str) -> Account:
    account = await _fetch(db, account_id)
    if account is None or account.deleted_at or account.merged_into:
        raise AccountRefused("Account does not exist")
    return account


def _not_self(actor_id: str, account_id: str) -> None:
    if str(actor_id or "") == str(account_id or ""):
        raise AccountRefused("An account cannot make this change to itself")


# A password set from now on -- created, changed by its account or reset by an
# Admin -- is at least this long. A stored shorter one still signs in.
MIN_NEW_PASSWORD_LENGTH = 8


async def _new_verifier(password: str | None) -> str | None:
    """The verifier for a password being set now, or ``None`` when none is:
    the one place the new-password rule is enforced, hashed off the event
    loop and before any write transaction opens."""
    if not password:
        return None
    if len(password) < MIN_NEW_PASSWORD_LENGTH:
        raise AccountRefused(f"A password must be at least {MIN_NEW_PASSWORD_LENGTH} characters")
    return await asyncio.to_thread(hash_password, password)


async def _set_password_row(db, account_id: str, username: str | None, verifier: str | None) -> None:
    """Create or replace an account's one local credential row; ``verifier``
    is a prepared ``_new_verifier`` (``None`` keeps the stored one)."""
    existing = await db.fetchone(
        "SELECT username, password_hash FROM account_local_credentials WHERE account_id=?", (account_id,),
    )
    name = str(username if username is not None else (existing["username"] if existing else "")).strip()
    if not name or len(name) > 256:
        raise AccountRefused("A username is required")
    owner = await db.fetchone("SELECT account_id FROM account_local_credentials WHERE username=?", (name,))
    if owner is not None and owner["account_id"] != account_id:
        raise AccountRefused("That username belongs to another account")
    verifier = verifier or (existing["password_hash"] if existing else "")
    await db.execute(
        "INSERT INTO account_local_credentials(account_id,username,password_hash,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET username=excluded.username, "
        "password_hash=excluded.password_hash, updated_at=excluded.updated_at",
        (account_id, name, verifier, _now()),
    )


# ── sign-in resolution ──────────────────────────────────────────────────────

def _claim_text(claims: Mapping[str, Any], key: str, limit: int) -> str:
    value = claims.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def oidc_profile(claims: Mapping[str, Any]) -> dict:
    """The profile an OIDC identity's claims offer, by standard precedence.
    Absent claims stay absent: nothing is fabricated, and an email's
    verification is ``None`` unless the provider stated a boolean."""
    actual = _claim_text(claims, "name", 256) or " ".join(
        part for part in (_claim_text(claims, "given_name", 128), _claim_text(claims, "family_name", 128)) if part)
    verified = claims.get("email_verified")
    email = _claim_text(claims, "email", 320)
    return {
        "display_name": (_claim_text(claims, "preferred_username", 256) or _claim_text(claims, "nickname", 256)
                         or _claim_text(claims, "name", 256)),
        "actual_name": actual[:256],
        "email": email,
        "email_verified": verified if isinstance(verified, bool) and email else None,
    }


async def resolve_oidc(issuer: str, subject: str, claims: Mapping[str, Any], *, default_role) -> Account | None:
    """The account an admitted OIDC identity signs in as.

    A bound identity resolves to its account; an unbound one is provisioned
    exactly once -- the binding's primary key makes a concurrent first sign-in
    of the same identity find the account the first one created -- with the
    configured default role. Only ``(issuer, subject)`` identifies: a matching
    email never links an existing account.

    An identity whose account was REMOVED (its binding still names that
    tombstone, the durable marker) returns as a new account under a fresh ID,
    created DISABLED whatever the default role: the binding moves to it, the
    tombstone keeps its history, and no session results until an Admin
    enables the new account. A DISABLED account is never provisioned around:
    its identity keeps resolving to it. Returns ``None`` for any account that
    is not active and for an identity that cannot be provisioned."""
    issuer, subject = str(issuer or ""), str(subject or "")
    if not issuer or not subject:
        return None
    role = Role(default_role) if default_role in {r.value for r in OIDC_PROVISION_ROLES} else Role.USER
    profile = oidc_profile(claims)
    email, verified = profile["email"], profile["email_verified"]
    display = (profile["display_name"] or email or subject)[:256]
    async with _transaction() as db:
        row = await db.fetchone(
            "SELECT account_id FROM account_oidc_bindings WHERE issuer=? AND subject=?", (issuer, subject),
        )
        async def provision(enabled: bool) -> str:
            account_id = str(uuid.uuid4())
            now = _now()
            await db.execute(
                "INSERT INTO accounts(id,role,origin,display_name,actual_name,email,email_verified,"
                "email_verified_known,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, role.value, "oidc", display, profile["actual_name"] or None, email, int(verified is True),
                 int(verified is not None), int(enabled), now, now),
            )
            return account_id

        if row is None:
            account_id = await provision(True)
            await db.execute(
                "INSERT INTO account_oidc_bindings(issuer,subject,account_id,created_at) VALUES(?,?,?,?)",
                (issuer, subject, account_id, _now()),
            )
        else:
            account_id = str(row["account_id"])
            bound = await _fetch(db, account_id)
            if bound is not None and bound.deleted_at and not bound.merged_into:
                removed, account_id = account_id, await provision(False)
                await db.execute(
                    "UPDATE account_oidc_bindings SET account_id=? WHERE issuer=? AND subject=? AND account_id=?",
                    (account_id, issuer, subject, removed))
            else:
                await _complete_oidc_profile(db, account_id, profile)
        account = await _fetch(db, account_id)
    return account if account is not None and account.active else None


async def _complete_oidc_profile(db, account_id: str, profile: Mapping[str, Any]) -> None:
    """A returning identity's claims only complete what the account lacks.
    Display and actual names the account already has are its own choices and
    are never overwritten -- an actual name the account set or cleared is a
    string, and only a never-populated one (NULL) is completed; an empty
    email is filled; the provider may restate the verification of the SAME
    address only, and never of one the account entered itself. Nothing here
    identifies, links, enables or changes the role of anything."""
    account = await _fetch(db, account_id)
    if account is None or not account.active:
        return
    changes: dict[str, Any] = {}
    never_named = (await db.fetchone("SELECT actual_name IS NULL AS unset FROM accounts WHERE id=?",
                                     (account.id,)))["unset"]
    if never_named and profile["actual_name"]:
        changes["actual_name"] = profile["actual_name"]
    email, verified = profile["email"], profile["email_verified"]
    if email and not account.email:
        changes.update(email=email, email_verified=int(verified is True), email_verified_known=int(verified is not None),
                       email_self_entered=0)
    elif (email and verified is not None and not account.email_self_entered
          and email.casefold() == account.email.casefold()):
        changes.update(email_verified=int(verified), email_verified_known=1)
    if changes:
        assignments = ",".join(f"{column}=?" for column in changes)
        await db.execute(f"UPDATE accounts SET {assignments}, updated_at=? WHERE id=?",
                         (*changes.values(), _now(), account.id))


async def bind_verified_oidc(issuer: str, subject: str, account_id: str) -> Account | None:
    """Verify Sign-In's binding: the Admin who started the verification has
    just proven, in this browser, that they control ``(issuer, subject)``.

    An unbound identity is linked to that Admin; an identity already bound
    keeps its account, which must itself be an active Admin. ``None`` refuses
    the verification. The caller holds the authentication configuration lock."""
    issuer, subject = str(issuer or ""), str(subject or "")
    async with _transaction() as db:
        row = await db.fetchone(
            "SELECT account_id FROM account_oidc_bindings WHERE issuer=? AND subject=?", (issuer, subject),
        )
        target = str(row["account_id"]) if row is not None else str(account_id or "")
        account = await _fetch(db, target) if target else None
        if not issuer or not subject or account is None or not account.active or account.role is not Role.ADMIN:
            await db.rollback()
            return None
        if row is None:
            await db.execute(
                "INSERT INTO account_oidc_bindings(issuer,subject,account_id,created_at) VALUES(?,?,?,?)",
                (issuer, subject, target, _now()),
            )
    return account


# ── Stage 1 self-service through Authentication settings ───────────────────

async def set_own_local_credential(
    account_id: str, *, username: str | None = None, password: str | None = None, clear: bool = False,
    settings=None, persist=None,
) -> None:
    """The signed-in account's own local credential, as Settings ->
    Authentication edits it, committed together with the authentication
    ``settings`` that ``persist`` writes (``_mutation``). The caller holds the
    authentication configuration lock (the Authentication settings mutation
    path)."""
    touched = [str(account_id)]
    verifier = None if clear else await _new_verifier(password)
    async with _mutation(touched, settings, persist) as db:
        account = await _require(db, account_id)
        if account.role is Role.INTEGRATION:
            raise AccountRefused("Integration accounts have no password")
        if clear:
            if username is None:
                await db.execute(
                    "UPDATE account_local_credentials SET password_hash='', updated_at=? WHERE account_id=?",
                    (_now(), account.id),
                )
            else:
                await _set_password_row(db, account.id, username, None)
                await db.execute(
                    "UPDATE account_local_credentials SET password_hash='' WHERE account_id=?", (account.id,),
                )
        elif username is not None and not username.strip() and not password:
            # Clearing the username releases a credential that holds no
            # password; a stored password always keeps its username.
            if account.has_password:
                raise AccountRefused("A username is required while a password is stored")
            await db.execute("DELETE FROM account_local_credentials WHERE account_id=?", (account.id,))
        else:
            await _set_password_row(db, account.id, username, verifier)


async def local_credential_status(account_id: str) -> tuple[str, bool]:
    account = await get_account(account_id) if account_id else None
    return (account.username, account.has_password) if account is not None else ("", False)


# ── offline recovery / pre-account migration ────────────────────────────────

async def import_legacy_credential(username: str, password_hash: str) -> bool:
    """Install the configuration file's local credential on the default Admin.

    Before accounts, the one local credential lived in ``config.json``; it is
    that installation's operator, the default Admin. The same file input
    remains the offline recovery path: an operator who can no longer sign in
    stops DebridPulse, writes ``auth_username`` and a plaintext
    ``auth_password`` into the configuration file (``core.config`` turns the
    plaintext into a verifier at load) and starts it again; this replaces the
    default Admin's credential. Idempotent. Returns whether anything changed."""
    username = str(username or "").strip()
    password_hash = str(password_hash or "").strip()
    if not is_usable_password_hash(password_hash):
        return False
    async with _transaction() as db:
        current = await db.fetchone(
            "SELECT username, password_hash FROM account_local_credentials WHERE account_id=?",
            (DEFAULT_ADMIN_ID,),
        )
        name = username or (str(current["username"]) if current else "")
        if not name:
            raise RuntimeError("The configuration file's password has no username to sign in with")
        if current is not None and current["username"] == name and current["password_hash"] == password_hash:
            await db.rollback()
            return False
        owner = await db.fetchone("SELECT account_id FROM account_local_credentials WHERE username=?", (name,))
        if owner is not None and owner["account_id"] != DEFAULT_ADMIN_ID:
            raise RuntimeError("The configuration file's username belongs to another account")
        await db.execute(
            "INSERT INTO account_local_credentials(account_id,username,password_hash,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET username=excluded.username, "
            "password_hash=excluded.password_hash, updated_at=excluded.updated_at",
            (DEFAULT_ADMIN_ID, name, password_hash, _now()),
        )
        await _touch(db, [DEFAULT_ADMIN_ID])
    _revoke([DEFAULT_ADMIN_ID])
    return True


# ── account management domain (Stage 2 wires permission-gated endpoints) ───

async def create_local_account(username: str, password: str, *, role, display_name: str = "") -> Account:
    role = Role(role)
    if role not in HUMAN_ROLES:
        raise AccountRefused("Local accounts hold a human role")
    account_id = str(uuid.uuid4())
    verifier = await _new_verifier(password)
    async with _locked_mutation([]) as db:
        now = _now()
        await db.execute(
            "INSERT INTO accounts(id,role,origin,display_name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (account_id, role.value, "local", str(display_name or username).strip()[:256], now, now),
        )
        await _set_password_row(db, account_id, username, verifier)
    return await get_account(account_id)


async def create_integration_account(display_name: str) -> Account:
    account_id = str(uuid.uuid4())
    async with _locked_mutation([]) as db:
        now = _now()
        await db.execute(
            "INSERT INTO accounts(id,role,origin,display_name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (account_id, Role.INTEGRATION.value, "integration", str(display_name or "").strip()[:256], now, now),
        )
    return await get_account(account_id)


async def set_role(actor_id: str, account_id: str, role) -> None:
    """A role change takes effect on the account's next request: its live
    sessions are re-bound to the new role (never ended), and each connected
    browser is told to reset (``account_changed``)."""
    role = Role(role)
    _not_self(actor_id, account_id)
    async with _locked_mutation([str(account_id)], rebind=True) as db:
        account = await _require(db, account_id)
        if account.protected:
            raise AccountRefused("A default account's role cannot change")
        if (account.role is Role.INTEGRATION) != (role is Role.INTEGRATION):
            raise AccountRefused("Integration and human roles are not interchangeable")
        await db.execute("UPDATE accounts SET role=? WHERE id=?", (role.value, account.id))


async def _withdraw_owned_demand(db, account_id: str, reason: str) -> list[int]:
    """Withdraw every outstanding demand the account owns, in the caller's
    account transaction (accounts and transfers share one database), through
    the transfer owner. Its consequences are the transfer core's
    (``TransferEngine.settle_withdrawn_demand``), derived from this durable
    withdrawal by the next resolution cycle even if nothing else runs."""
    from transfers.recovery_repository import TransferRepository

    changed: list[int] = []
    for owner in await _owned_account_ids(db, account_id):
        changed.extend(await TransferRepository.withdraw_demand_in(db, account_id=owner, by=reason))
    return sorted(set(changed))


async def _owned_account_ids(db, account_id: str) -> list[str]:
    """The account and every account merged into it: demand recorded under
    any of them is this account's (``canonical_account_id``)."""
    rows = await db.fetchall("SELECT id, merged_into FROM accounts WHERE merged_into IS NOT NULL")
    parent = {str(row["id"]): str(row["merged_into"]) for row in rows}

    def root(identity: str) -> str:
        seen = set()
        while identity in parent and identity not in seen:
            seen.add(identity)
            identity = parent[identity]
        return identity

    return [str(account_id), *sorted(identity for identity in parent if root(identity) == str(account_id))]


async def owned_account_ids(account_id: str) -> list[str]:
    from db.database import get_db

    async with get_db() as db:
        return await _owned_account_ids(db, account_id)


async def set_enabled(actor_id: str, account_id: str, enabled: bool) -> list[int]:
    """Disable revokes access at once and, in the same transaction, withdraws
    the account's outstanding demand. Re-enabling restores access only: a
    withdrawn demand is never restored. Returns the transfers whose demand
    was withdrawn."""
    _not_self(actor_id, account_id)
    withdrawn: list[int] = []
    async with _locked_mutation([str(account_id)]) as db:
        account = await _require(db, account_id)
        if account.protected == "default_admin" and not enabled:
            raise AccountRefused("The default Admin account cannot be disabled")
        await db.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(bool(enabled)), account.id))
        if not enabled:
            withdrawn = await _withdraw_owned_demand(db, account.id, "account_disabled")
    return withdrawn


async def delete_account(actor_id: str, account_id: str) -> list[int]:
    """Remove an account from the active directory, keeping its tombstone.

    The ID stays interpretable for every historical record. The local
    credential is released; OIDC bindings stay on the tombstone as the
    removed-identity marker, so that identity signing in again gets a NEW,
    disabled account (``resolve_oidc``), never this one back."""
    _not_self(actor_id, account_id)
    async with _locked_mutation([str(account_id)]) as db:
        account = await _require(db, account_id)
        if account.protected:
            raise AccountRefused("A default account cannot be deleted")
        await db.execute("DELETE FROM account_local_credentials WHERE account_id=?", (account.id,))
        await db.execute("UPDATE accounts SET enabled=0, deleted_at=? WHERE id=?", (_now(), account.id))
        await db.execute("UPDATE account_tokens SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
                         (_now(), account.id))
        await db.execute("DELETE FROM account_notifications WHERE account_id=?", (account.id,))
        await db.execute("DELETE FROM account_appearance WHERE account_id=?", (account.id,))
        return await _withdraw_owned_demand(db, account.id, "account_deleted")


async def merge_accounts(actor_id: str, source_ids, target_id: str) -> None:
    """Fold one or more duplicate accounts into one surviving account,
    explicitly and atomically: every source is folded in the same transaction,
    or nothing changes.

    Each source's OIDC identities, and its local credential when it holds the
    only one among them all, move to the target; each source becomes a
    tombstone redirecting to the target (``canonical_account_id``). No
    historical row is rewritten. The target keeps its own role."""
    sources = [str(source_ids)] if isinstance(source_ids, str) else [str(item) for item in source_ids]
    sources = list(dict.fromkeys(sources))
    if not sources:
        raise AccountRefused("Choose at least one account to merge")
    _not_self(actor_id, target_id)
    for source_id in sources:
        _not_self(actor_id, source_id)
    if str(target_id) in sources:
        raise AccountRefused("An account cannot be merged into itself")
    async with _locked_mutation([*sources, str(target_id)]) as db:
        target = await _require(db, target_id)
        holders = []
        for source_id in sources:
            source = await _require(db, source_id)
            if source.protected:
                raise AccountRefused("A default account cannot be merged away")
            if (source.role is Role.INTEGRATION) != (target.role is Role.INTEGRATION):
                raise AccountRefused("Integration and human accounts cannot be merged")
            if await db.fetchone("SELECT 1 FROM account_local_credentials WHERE account_id=?", (source.id,)):
                holders.append(source.id)
        if await db.fetchone("SELECT 1 FROM account_local_credentials WHERE account_id=?", (target.id,)):
            holders.append(target.id)
        if len(holders) > 1:
            raise AccountRefused("More than one of these accounts holds a local credential; remove all but one first")
        now = _now()
        for source_id in sources:
            if source_id in holders:
                await db.execute(
                    "UPDATE account_local_credentials SET account_id=? WHERE account_id=?", (target.id, source_id))
            await db.execute("UPDATE account_oidc_bindings SET account_id=? WHERE account_id=?", (target.id, source_id))
            # A folded account's notification destinations and appearance are
            # its own: they are retired, never merged into the survivor's.
            await db.execute("DELETE FROM account_notifications WHERE account_id=?", (source_id,))
            await db.execute("DELETE FROM account_appearance WHERE account_id=?", (source_id,))
            await db.execute(
                "UPDATE accounts SET enabled=0, merged_into=?, deleted_at=? WHERE id=?", (target.id, now, source_id))


async def reset_password(actor_id: str, account_id: str, password: str, *, username: str | None = None) -> None:
    """An Admin sets or resets another account's local credential."""
    _not_self(actor_id, account_id)
    known = await get_account(account_id)
    if known is not None and known.role is Role.INTEGRATION:
        raise AccountRefused("Integration accounts have no password")
    if not password:
        raise AccountRefused("A password is required")
    verifier = await _new_verifier(password)
    async with _locked_mutation([str(account_id)]) as db:
        account = await _require(db, account_id)
        if account.role is Role.INTEGRATION:
            raise AccountRefused("Integration accounts have no password")
        await _set_password_row(db, account.id, username, verifier)


async def rename_local_credential(actor_id: str, account_id: str, username: str) -> None:
    """An Admin renames another account's local credential, keeping its
    password. A credential holds a password: an account without one gets its
    username together with its first password (``reset_password``), never a
    username alone that could not sign in. Like every credential change, the
    account's sessions end (``_mutation``), so nothing stays signed in under
    the former name."""
    _not_self(actor_id, account_id)
    async with _locked_mutation([str(account_id)]) as db:
        account = await _require(db, account_id)
        if account.role is Role.INTEGRATION:
            raise AccountRefused("Integration accounts have no password")
        if not account.has_password:
            raise AccountRefused("A username is set together with the account's first password")
        await _set_password_row(db, account.id, str(username or ""), None)


async def set_display_name(account_id: str, display_name: str) -> None:
    """An account's display name, set by itself or by an Admin."""
    name = str(display_name or "").strip()[:256]
    if not name:
        raise AccountRefused("A display name is required")
    async with _locked_mutation([]) as db:
        account = await _require(db, account_id)
        await db.execute("UPDATE accounts SET display_name=?, updated_at=? WHERE id=?", (name, _now(), account.id))


async def set_actual_name(account_id: str, actual_name: str) -> None:
    """An account's own record of the person's name. Blank clears it, and a
    cleared name stays cleared: it is a choice, not a missing value."""
    name = str(actual_name or "").strip()[:256]
    async with _locked_mutation([]) as db:
        account = await _require(db, account_id)
        await db.execute("UPDATE accounts SET actual_name=?, updated_at=? WHERE id=?", (name, _now(), account.id))


# A self-entered address: one ``local@domain`` of printable characters within
# the SMTP path limits, with a dotted host name. Syntax only -- it proves
# nothing about who receives mail there.
_EMAIL_LIMIT = 254
_EMAIL_LOCAL = re.compile(r'[^\s@<>()\[\]\\,;:"\x00-\x1f\x7f]{1,64}')
_EMAIL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def entered_email(value: str) -> str:
    """The normalized form of an address an account enters itself, or
    ``InvalidEmail``: surrounding space removed and the domain lower-cased."""
    email = str(value or "").strip()
    local, _, domain = email.rpartition("@")
    domain = domain.lower()
    labels = domain.split(".")
    if (len(email) > _EMAIL_LIMIT or not _EMAIL_LOCAL.fullmatch(local) or len(labels) < 2
            or not all(_EMAIL_LABEL.fullmatch(label) for label in labels)):
        raise InvalidEmail("Enter a valid email address")
    return f"{local}@{domain}"


async def set_own_email(account_id: str, email: str, expected: str = "") -> str:
    """An account records its own email while it has none, and corrects an
    email it entered itself. ``expected`` is the email the caller's form
    showed ('' for none): the write is conditional on the stored email still
    being exactly that, so a stale form or a concurrent save can never replace
    a newer address. An email the account's identity provider supplied is
    never replaced here. The address is recorded unverified
    (``email_verified_known`` with ``email_verified`` false) and as
    self-entered: it never links, binds, admits or authorizes anything. There
    is no clearing: an empty address is not a valid one."""
    address = entered_email(email)
    expected = str(expected or "")
    async with _locked_mutation([]) as db:
        account = await _require(db, account_id)
        if account.role is Role.INTEGRATION:
            raise AccountRefused("Integration accounts have no email")
        cursor = await db.execute(
            "UPDATE accounts SET email=?, email_verified=0, email_verified_known=1, email_self_entered=1, updated_at=? "
            "WHERE id=? AND email=? AND (email='' OR email_self_entered=1)", (address, _now(), account.id, expected))
        if cursor.rowcount != 1:
            current = await _require(db, account_id)
            if current.email and not current.email_self_entered:
                raise AccountRefused("This email comes from your identity provider and cannot be changed here")
            if not expected:
                raise AccountRefused("This account already has an email address")
            raise AccountRefused("This email was changed since this form was opened")
    return address


async def list_accounts() -> list[dict]:
    """The account directory with each account's sign-in facts; never a
    secret or verifier."""
    from db.database import get_db

    async with get_db() as db:
        rows = await db.fetchall(
            f"""SELECT a.*, COALESCE(c.username,'') AS username, COALESCE(c.password_hash,'') AS password_hash,
                    (SELECT COUNT(*) FROM account_oidc_bindings b WHERE b.account_id=a.id) AS oidc_identities,
                    (SELECT COUNT(*) FROM account_tokens t WHERE t.account_id=a.id AND t.revoked_at IS NULL)
                        AS active_tokens
                FROM accounts a LEFT JOIN account_local_credentials c ON c.account_id=a.id
                ORDER BY a.created_at, a.id""")
    result = []
    for row in rows:
        account = _account(row)
        result.append({
            "id": account.id, "role": account.role.value, "origin": account.origin,
            "display_name": account.display_name, "actual_name": account.actual_name, "email": account.email,
            "email_verified": account.email_verified, "email_verified_known": account.email_verified_known,
            "email_self_entered": account.email_self_entered, "enabled": account.enabled,
            "protected": account.protected or None, "generation": account.generation,
            "merged_into": account.merged_into or None, "deleted_at": account.deleted_at or None,
            "username": account.username, "has_password": account.has_password,
            "oidc_identities": int(row["oidc_identities"]), "active_tokens": int(row["active_tokens"]),
        })
    return result


async def requester_identities(account_ids, token_ids) -> tuple[dict[str, dict], dict[str, str]]:
    """What attributing a request may show about who made it: each account's
    display name, kind and standing, read from that account's own row (a
    deleted or merged account keeps it, so attribution never moves to a
    survivor), and each token's label (a revoked token keeps it). Never a
    credential, verifier, username, email or identity-provider claim."""
    from db.database import get_db

    accounts_ = sorted({str(item) for item in account_ids if item})
    tokens = sorted({str(item) for item in token_ids if item})
    people: dict[str, dict] = {}
    labels: dict[str, str] = {}
    async with get_db() as db:
        if accounts_:
            for row in await db.fetchall(
                    f"SELECT id, role, display_name, enabled, merged_into, deleted_at FROM accounts "
                    f"WHERE id IN ({','.join('?' * len(accounts_))})", accounts_):
                standing = ("merged" if row["merged_into"] else "deleted" if row["deleted_at"]
                            else "active" if row["enabled"] else "disabled")
                people[str(row["id"])] = {
                    "name": str(row["display_name"] or ""),
                    "kind": "integration" if row["role"] == Role.INTEGRATION.value else "person",
                    "standing": standing,
                }
        if tokens:
            for row in await db.fetchall(
                    f"SELECT id, label FROM account_tokens WHERE id IN ({','.join('?' * len(tokens))})", tokens):
                labels[str(row["id"])] = str(row["label"] or "")
    return people, labels


async def change_own_password(account_id: str, current_password: str, new_password: str, *,
                              peer: str = "unknown") -> None:
    """A signed-in account replaces its own password, proving the current one.

    The proof is a sign-in attempt and takes the sign-in path's own measures:
    the transport peer's admission and failure throttle (failures attributed
    to this account's username) and the Argon2 verification off the event
    loop, outside the authentication lock and any write transaction. Under
    the lock the stored credential must still be the one just proven."""
    from auth.manager import PasswordAuthenticationBusy, password_attempt_admission
    from auth.passwords import verify_password_candidate_async
    from auth.throttle import password_failure_throttle

    if not new_password:
        raise AccountRefused("A password is required")
    if len(new_password) < MIN_NEW_PASSWORD_LENGTH:
        raise AccountRefused(f"A password must be at least {MIN_NEW_PASSWORD_LENGTH} characters")
    # Admission covers the proof only: the change itself is re-checked under
    # the lock, so waiting for the lock never holds a sign-in slot.
    if not password_attempt_admission.acquire(peer):
        raise PasswordAuthenticationBusy
    try:
        delay = password_failure_throttle.delay_for(peer)
        if delay:
            await asyncio.sleep(delay)
        account = await get_account(account_id)
        found = await local_credential(account.username) if account is not None and account.username else None
        proven = found[1] if found is not None and found[0].id == str(account_id) else ""
        identity = account.username if account is not None else str(account_id)
        verified = await verify_password_candidate_async(proven, str(current_password or ""),
                                                         use_configured_hash=bool(proven))
    finally:
        password_attempt_admission.release(peer)
    if not verified:
        password_failure_throttle.record_failure(peer, identity)
        raise AccountRefused("The current password is not correct")
    verifier = await _new_verifier(new_password)
    async with _locked_mutation([str(account_id)]) as db:
        account = await _require(db, account_id)
        row = await db.fetchone(
            "SELECT password_hash FROM account_local_credentials WHERE account_id=?", (account.id,))
        if row is None or not secrets.compare_digest(password_credential_version(row["password_hash"]),
                                                     password_credential_version(proven)):
            raise AccountRefused("The password was changed meanwhile; try again")
        await _set_password_row(db, account.id, None, verifier)
    password_failure_throttle.record_success(peer, identity)


# ── Integration API tokens ──────────────────────────────────────────────────

def _token_verifier(raw: str) -> str:
    return hashlib.sha256(str(raw or "").encode("utf-8", errors="surrogatepass")).hexdigest()


async def create_token(account_id: str, label: str, *, succeeds: str | None = None) -> tuple[dict, str]:
    """Issue a labeled token for an Integration account. The raw secret is
    returned once, here, and never again.

    A token is a new external client (``client_id``) unless it ``succeeds``
    an earlier token: an Admin's explicit rotation, which gives the new token
    that token's client -- and so its external jobs -- and revokes the
    predecessor in the same transaction. The predecessor may already be
    revoked (recovering a client whose token was lost), but it must belong to
    this account or to an account merged into it. Rotation withdraws nothing."""
    label = str(label or "").strip()[:128]
    if not label:
        raise AccountRefused("A token needs a label")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)
    token_id = str(uuid.uuid4())
    client_id = token_id
    async with _transaction() as db:
        account = await _require(db, account_id)
        if account.role is not Role.INTEGRATION:
            raise AccountRefused("Only Integration accounts hold API tokens")
        if succeeds is not None:
            predecessor = await db.fetchone("SELECT id, account_id, client_id FROM account_tokens WHERE id=?",
                                            (str(succeeds),))
            if predecessor is None or str(predecessor["account_id"]) not in await _owned_account_ids(db, account.id):
                raise AccountRefused("The token to succeed does not exist")
            client_id = str(predecessor["client_id"] or predecessor["id"])
            await db.execute("UPDATE account_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                             (_now(), predecessor["id"]))
        await db.execute(
            "INSERT INTO account_tokens(id,account_id,label,verifier,created_at,client_id) VALUES(?,?,?,?,?,?)",
            (token_id, account.id, label, _token_verifier(raw), _now(), client_id))
        if succeeds is not None:
            from db.event_journal import JournalEvent, record

            await record(db, JournalEvent(
                "administration", "administration.integration_token_rotated", "info",
                "An Integration token was rotated: its external client continues under the new token",
                "account", account.id, detail=f"client {client_id}; predecessor token {predecessor['id']}"))
    return {"id": token_id, "account_id": str(account_id), "label": label, "revoked_at": None,
            "client_id": client_id}, raw


async def list_tokens(account_id: str) -> list[dict]:
    """An Integration account's ACTIVE tokens: the ones that can still be
    revoked. A revoked token keeps its row for attribution
    (``requester_identities``) but is no longer the account's to manage."""
    from db.database import get_db

    async with get_db() as db:
        return await db.fetchall(
            "SELECT id, account_id, label, created_at, revoked_at, client_id FROM account_tokens "
            "WHERE account_id=? AND revoked_at IS NULL ORDER BY created_at, id", (str(account_id),))


async def revoke_token(account_id: str, token_id: str) -> bool:
    """Revoke one token at once. Its row stays for attribution; work it
    submitted is untouched."""
    async with _transaction() as db:
        cursor = await db.execute(
            "UPDATE account_tokens SET revoked_at=? WHERE id=? AND account_id=? AND revoked_at IS NULL",
            (_now(), str(token_id), str(account_id)))
        return cursor.rowcount == 1


async def resolve_token(raw: str) -> tuple[Account, str, str] | None:
    """The active Integration account, token ID and external client ID a
    bearer secret names.

    Lookup is by verifier, so tokens cannot be enumerated or probed by
    prefix; the matched verifier is still compared in constant time."""
    from db.database import get_db

    raw = str(raw or "")
    if not raw.startswith(TOKEN_PREFIX) or len(raw) > 256:
        return None
    verifier = _token_verifier(raw)
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT id, account_id, verifier, client_id FROM account_tokens WHERE verifier=? AND revoked_at IS NULL",
            (verifier,))
        account = await _fetch(db, row["account_id"]) if row is not None else None
    if row is None or not secrets.compare_digest(str(row["verifier"]), verifier):
        return None
    if account is None or not account.active or account.role is not Role.INTEGRATION:
        return None
    return account, str(row["id"]), str(row["client_id"] or row["id"])


async def any_active_client(client_ids) -> bool:
    """Whether any of these external clients is currently backed by a token
    ``resolve_token`` would accept: not revoked, of an active Integration
    account. Answers only that -- one bounded existence read, no token,
    verifier or account leaves it. A token's client is its ``client_id``, or
    its own ID for a token issued before clients existed (as in
    ``resolve_token``)."""
    from db.database import get_db

    clients = sorted({str(client) for client in client_ids if client})
    if not clients:
        return False
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT 1 FROM account_tokens t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.revoked_at IS NULL AND COALESCE(t.client_id, t.id) IN (SELECT value FROM json_each(?)) "
            # ``Account.active`` and the Integration role, as ``resolve_token`` reads them.
            "AND a.role = 'integration' AND COALESCE(a.enabled, 0) != 0 "
            "AND COALESCE(a.deleted_at, '') = '' AND COALESCE(a.merged_into, '') = '' "
            "LIMIT 1", (json.dumps(clients),))
    return row is not None


async def retire_legacy_api_token(path) -> bool:
    """The pre-account global API token stops authenticating: its verifier
    file is removed and the operator is told to issue an Integration token.
    It never becomes any account's credential."""
    from pathlib import Path

    target = Path(path)
    if not target.exists():
        return False
    target.unlink()
    from db.event_journal import JournalEvent, record_now

    await record_now(JournalEvent(
        "administration", "administration.api_token_retired", "warning",
        "The global API token was retired: issue a token for an Integration account and update its clients",
        "installation"))
    return True
