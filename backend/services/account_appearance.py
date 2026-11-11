"""A signed-in human account's own appearance.

Three independent choices, durably keyed by the internal account ID
(``auth.accounts`` table ``account_appearance``) and never by a name, email,
browser or OIDC subject: the interface mode, the palette chosen for Dark mode
and the palette chosen for Light mode. Choosing one never sets or constrains
another. An account without a record has the native appearance: Dark mode,
DebridPulse Dark, and DebridPulse Light for Light mode.

The catalog -- each mode's palettes, in picker order, with their labels -- is
``CATALOG`` here, the one authority: the interface receives it with its
first-paint script (``api.auth_routes.theme_bootstrap_script``) and this
module accepts exactly its IDs. The first palette of each mode is its native
DebridPulse palette. A stored ID the catalog no longer holds (a retired
palette) reads as that mode's native palette and is never rewritten by a read.

While interactive sign-in is off there is no account to own an appearance:
the installation's one standalone principal never reads or writes here, and
the interface keeps its choices in the browser (``ui-theme-bootstrap.js``).

The same record holds one more choice, of a different kind: the account's
presentation, Easy or Advanced (``presentation_mode``; ``load_presentation``
and ``set_presentation``). It decides which parts of the interface the
account is shown -- never what it may do -- and an account that never chose
is Advanced; a stored value that is neither reads as Advanced and is never
rewritten by a read. Unlike the theme it is the account's even while
interactive sign-in is off: the standalone principal is the default Admin
account, and its presentation is that account's (``presentation_owner``), the
same in every browser. Writing it changes that one column; writing the theme
never changes it.

The account's interface language (``locale``; ``load_locale`` and
``set_locale``) is the same kind of choice, kept the same way: one BCP 47 tag
of the locales the interface knows (``core.localization.locale_tags``), or
none. An account without a choice inherits the installation's default
language (``core.config.AppSettings.default_locale``, written only by an
Admin, ``set_default_locale``), and English where none is configured; a
chosen English is a choice and stays English whatever the default becomes.
Clearing the choice returns the account to the default. A stored tag the
interface no longer knows -- the account's or the default -- reads as
English without being rewritten. It belongs to the same owner as the
presentation (``presentation_owner``), the default Admin's while sign-in is
off, and writing it changes that one column. It decides the language the
interface is shown in, nothing else.

The sign-in page has one appearance of its own, the installation's
(``auth.accounts`` table ``login_appearance``, one row): a mode and that
mode's palette, nothing else. A personal choice never changes it: only an
Admin's explicit publication does (``publish``) -- of the Admin's own active
pair (its mode and that mode's palette), and only while that pair is the one
the Admin's interface shows and has saved. Eligibility is the account's own
current role and state, read in the publishing transaction; concurrent
publications serialize, and the last to commit is the one the page shows.
Once written the value is the installation's: it names no account, and
nothing an account later does -- signing in, choosing a theme, its demotion,
removal or merge -- changes it. The anonymous page reads that one row and
nothing else (``login``). Without one it is DebridPulse Dark.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, model_validator
from core.localization import CodedValueError

CATALOG = {
    "dark": (
        ("debridpulse-dark", "DebridPulse (Default)"),
        ("catppuccin-mocha", "Catppuccin Mocha"),
        ("dracula", "Dracula"),
        ("gruvbox-dark", "Gruvbox Dark"),
        ("everforest-dark", "Everforest Dark"),
        ("rose-pine", "Rosé Pine"),
        ("kanagawa-wave", "Kanagawa Wave"),
        ("ayu-mirage", "Ayu Mirage"),
        ("synthwave-84", "SynthWave '84"),
        ("oxocarbon-dark", "Oxocarbon Dark"),
    ),
    "light": (
        ("debridpulse-light", "DebridPulse (Default)"),
        ("catppuccin-latte", "Catppuccin Latte"),
        ("nord-light", "Nord Light"),
        ("gruvbox-light", "Gruvbox Light"),
        ("rose-pine-dawn", "Rosé Pine Dawn"),
        ("everforest-light", "Everforest Light"),
        ("ayu-light", "Ayu Light"),
        ("flexoki-light", "Flexoki Light"),
        ("oxocarbon-light", "Oxocarbon Light"),
        ("bluloco-light", "Bluloco Light"),
    ),
}
PALETTES = {mode: tuple(palette for palette, _label in entries) for mode, entries in CATALOG.items()}
Mode = Literal["dark", "light"]
DarkPalette = Literal[PALETTES["dark"]]
LightPalette = Literal[PALETTES["light"]]
DEFAULT = {"mode": "dark", "dark_palette": PALETTES["dark"][0], "light_palette": PALETTES["light"][0]}


def catalog() -> dict:
    """The catalog as the interface receives it."""
    return {mode: [{"id": palette, "label": label} for palette, label in entries]
            for mode, entries in CATALOG.items()}


class LoginAppearance(BaseModel):
    """A login appearance to publish: a mode and a palette of that mode."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Mode
    palette: str

    @model_validator(mode="after")
    def _in_catalog(self):
        if self.palette not in PALETTES[self.mode]:
            raise CodedValueError("Unknown palette for this mode", code="appearance.palette_unknown")
        return self


class LoginAppearanceRefused(Exception):
    """The caller is not an active Admin now."""


class LoginAppearanceStale(Exception):
    """The pair to publish is not the Admin's saved active pair (a newer choice
    was saved, or this one is not saved yet)."""


class AppearanceUpdate(BaseModel):
    """What an account may change: any of its three choices, nothing else.

    Unknown keys -- an account ID, a role, a fourth preference -- are refused,
    as are an unknown mode or palette, a value of the wrong JSON type, an
    explicit null and an empty change."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Mode | None = None
    dark_palette: DarkPalette | None = None
    light_palette: LightPalette | None = None

    @model_validator(mode="after")
    def _named_and_set(self):
        if not self.model_fields_set:
            raise CodedValueError("Name at least one appearance choice", code="appearance.choice_required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise CodedValueError("An appearance choice cannot be null", code="appearance.choice_null")
        return self


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


async def _read(db, account_id: str) -> dict:
    row = await db.fetchone(
        "SELECT mode, dark_palette, light_palette FROM account_appearance WHERE account_id=?", (str(account_id),))
    if row is None:
        return dict(DEFAULT)
    # A stored choice the catalog no longer holds is that choice's default.
    return {
        "mode": row["mode"] if row["mode"] in get_args(Mode) else DEFAULT["mode"],
        "dark_palette": row["dark_palette"] if row["dark_palette"] in PALETTES["dark"] else DEFAULT["dark_palette"],
        "light_palette": (row["light_palette"] if row["light_palette"] in PALETTES["light"]
                          else DEFAULT["light_palette"]),
    }


async def load(account_id: str) -> dict:
    from db.database import get_db

    async with get_db() as db:
        return await _read(db, account_id)


def _active(choices: dict) -> tuple[str, str]:
    """The pair an appearance shows: its mode and that mode's palette."""
    return choices["mode"], choices[f"{choices['mode']}_palette"]


async def _publishes_login(db, account_id: str) -> bool:
    """Whether this account may publish the login appearance: it is an active
    Admin now, in the transaction that would record it."""
    from auth.accounts import Role, _fetch

    account = await _fetch(db, account_id)
    return account is not None and account.active and account.role is Role.ADMIN


async def _project(db, pair: tuple[str, str]) -> None:
    await db.execute(
        "INSERT INTO login_appearance(id,mode,palette) VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, palette=excluded.palette", pair)


async def update(account_id: str, change: AppearanceUpdate) -> dict:
    """Apply one account's change to its own record, atomically: only the
    named choices change; the others keep what is stored (or their default).
    It is the account's own appearance alone: never the login appearance."""
    async with _transaction() as db:
        saved = {**await _read(db, account_id), **change.model_dump(exclude_unset=True)}
        await db.execute(
            "INSERT INTO account_appearance(account_id,mode,dark_palette,light_palette,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET mode=excluded.mode, "
            "dark_palette=excluded.dark_palette, light_palette=excluded.light_palette, "
            "updated_at=excluded.updated_at",
            (str(account_id), saved["mode"], saved["dark_palette"], saved["light_palette"],
             datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
    return saved


async def publish(account_id: str, shown: LoginAppearance) -> dict:
    """Publish an Admin's own active pair as the login appearance, in one
    transaction: the account must be an active Admin now, and ``shown`` -- the
    pair its interface shows -- must be the active pair saved for it, so an
    older saved pair is never published in place of the one on screen."""
    async with _transaction() as db:
        if not await _publishes_login(db, account_id):
            raise LoginAppearanceRefused()
        own = _active(await _read(db, account_id))
        if own != (shown.mode, shown.palette):
            raise LoginAppearanceStale()
        await _project(db, own)
    return {"mode": own[0], "palette": own[1]}


PresentationMode = Literal["easy", "advanced"]
DEFAULT_PRESENTATION = "advanced"


class PresentationUpdate(BaseModel):
    """An account's presentation choice: exactly one value, nothing else --
    an account ID, a role or a theme key is refused, as is any other value,
    type or an explicit null."""

    model_config = ConfigDict(extra="forbid", strict=True)

    presentation_mode: PresentationMode


def presentation_owner(principal, settings) -> str | None:
    """The account whose presentation a request reads and writes: the
    signed-in human account, or -- while interactive sign-in is off -- the
    standalone principal's default Admin, which is every request's account
    then. No Integration, no anonymous caller and never an account the
    request names."""
    from auth.accounts import DEFAULT_ADMIN_ID, HUMAN_ROLES
    from auth.policy import interactive_auth_enabled

    account_id = str(getattr(principal, "account_id", "") or "")
    if not account_id or getattr(principal, "role", "") not in {role.value for role in HUMAN_ROLES}:
        return None
    if principal.authenticated:
        return account_id
    if (principal.mechanism is None and account_id == DEFAULT_ADMIN_ID
            and not interactive_auth_enabled(settings)):
        return account_id
    return None


async def load_presentation(account_id: str) -> str:
    from db.database import get_db

    async with get_db() as db:
        row = await db.fetchone(
            "SELECT presentation_mode FROM account_appearance WHERE account_id=?", (str(account_id),))
    mode = row["presentation_mode"] if row is not None else None
    return mode if mode in get_args(PresentationMode) else DEFAULT_PRESENTATION


async def set_presentation(account_id: str, mode: str) -> str:
    """Record one account's presentation, atomically and alone: an account
    without a record gets the native theme with it, and an existing record
    keeps its theme exactly as stored."""
    if mode not in get_args(PresentationMode):
        raise CodedValueError("Unknown presentation", code="appearance.presentation_unknown")
    async with _transaction() as db:
        await db.execute(
            "INSERT INTO account_appearance(account_id,mode,dark_palette,light_palette,presentation_mode,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
            "presentation_mode=excluded.presentation_mode, updated_at=excluded.updated_at",
            (str(account_id), DEFAULT["mode"], DEFAULT["dark_palette"], DEFAULT["light_palette"], mode,
             datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
    return mode


class LocaleUpdate(BaseModel):
    """An account's language: one known locale tag, or null to clear the
    choice and inherit the installation's default. Nothing else: the field is
    required, and any other key is refused."""

    model_config = ConfigDict(extra="forbid", strict=True)

    locale: str | None

    @model_validator(mode="after")
    def _known(self):
        _admitted(self.locale, allow_none=True)
        return self


class DefaultLocaleUpdate(BaseModel):
    """The installation's default language: exactly one known locale tag.
    There is no null: English is chosen as English."""

    model_config = ConfigDict(extra="forbid", strict=True)

    locale: str

    @model_validator(mode="after")
    def _known(self):
        _admitted(self.locale)
        return self


def _admitted(tag: str | None, *, allow_none: bool = False) -> None:
    from core.localization import locale_tags

    if tag is None and allow_none:
        return
    if tag not in locale_tags():
        raise CodedValueError("Unknown locale", code="appearance.locale_unknown")


def _effective(tag: str | None) -> str | None:
    """A stored tag as the interface may show it, or None where it is unset
    or no longer admitted."""
    from core.localization import locale_tags

    return tag if tag is not None and tag in locale_tags() else None


def default_locale(settings=None) -> dict:
    """The installation's default language for accounts without their own:
    ``configured``, the tag an Admin chose (null: never chosen), and
    ``locale``, the one it gives -- English where none is configured or the
    configured one is no longer admitted."""
    from core.config import get_settings
    from core.localization import DEFAULT_LOCALE

    configured = getattr(settings if settings is not None else get_settings(), "default_locale", None)
    return {"locale": _effective(configured) or DEFAULT_LOCALE, "configured": configured}


async def load_locale(account_id: str) -> dict:
    """One account's language: ``preference``, its own stored choice (null:
    none, so it inherits); ``default``, the installation's default it would
    inherit; and ``locale``, the one the interface is shown in. A choice wins
    over the default; a choice that is no longer admitted shows English,
    never the default, and stays as stored."""
    from core.localization import DEFAULT_LOCALE
    from db.database import get_db

    async with get_db() as db:
        row = await db.fetchone("SELECT locale FROM account_appearance WHERE account_id=?", (str(account_id),))
    preference = row["locale"] if row is not None else None
    inherited = default_locale()["locale"]
    shown = inherited if preference is None else (_effective(preference) or DEFAULT_LOCALE)
    return {"locale": shown, "preference": preference, "default": inherited}


async def set_locale(account_id: str, tag: str | None) -> dict:
    """Record one account's language, atomically and alone, and return it as
    ``load_locale`` reads it. A tag: an account without a record gets the
    native theme with it, and an existing record keeps its theme and
    presentation exactly as stored. None clears the choice (the account
    inherits the default); an account without a record has none to clear."""
    _admitted(tag, allow_none=True)
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    async with _transaction() as db:
        if tag is None:
            await db.execute("UPDATE account_appearance SET locale=NULL, updated_at=? WHERE account_id=?",
                             (stamp, str(account_id)))
        else:
            await db.execute(
                "INSERT INTO account_appearance(account_id,mode,dark_palette,light_palette,locale,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "locale=excluded.locale, updated_at=excluded.updated_at",
                (str(account_id), DEFAULT["mode"], DEFAULT["dark_palette"], DEFAULT["light_palette"], tag, stamp))
    return await load_locale(account_id)


async def set_default_locale(tag: str) -> dict:
    """Record the installation's default language: the one settings field,
    saved with every other setting exactly as it is, under the configuration
    write lock that serializes every settings writer. The file is written
    before the running settings change, so a refused or failed write changes
    neither."""
    from core.config import apply_settings, config_write_lock, get_settings, journal_configuration_change, \
        save_settings

    _admitted(tag)
    async with config_write_lock():
        previous = get_settings()
        current = previous.model_copy(update={"default_locale": tag}, deep=True)
        save_settings(current)
        apply_settings(current)
    await journal_configuration_change(previous, current)
    return default_locale(current)


def public(row) -> dict:
    """The sign-in page's appearance from its stored row: a known mode and a
    palette of that mode. No row, or an unknown mode, is DebridPulse Dark; a
    palette the catalog no longer holds is that mode's native palette."""
    if row is None or row["mode"] not in PALETTES:
        return {"mode": DEFAULT["mode"], "palette": DEFAULT["dark_palette"]}
    mode = row["mode"]
    return {"mode": mode, "palette": row["palette"] if row["palette"] in PALETTES[mode] else PALETTES[mode][0]}


async def login() -> dict:
    """The installation's login appearance: the one ``login_appearance`` row
    and no other table -- no account, no account appearance, no role."""
    from db.database import get_db

    async with get_db() as db:
        return public(await db.fetchone("SELECT mode, palette FROM login_appearance WHERE id=1"))
