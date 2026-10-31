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

The sign-in page has one appearance of its own, the installation's
(``auth.accounts`` table ``login_appearance``, one row): a mode and that
mode's palette, nothing else. Only an effective selection by an eligible
Admin replaces it -- a change of that Admin's active pair (its mode, and the
palette of the mode active after the change) -- and it is replaced in the
same transaction that saves the Admin's own choice, so the last such write
to commit is the one the page shows. Eligibility is the account's own
current role and state, read in that transaction. Once written the value is
the installation's: it names no account, and nothing an account later does
-- signing in, a change to its inactive palette, its demotion, removal or
merge -- changes it. The anonymous page reads that one row and nothing else
(``login``). Without one it is DebridPulse Dark.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, model_validator

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
            raise ValueError("Name at least one appearance choice")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("An appearance choice cannot be null")
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


async def _selects_login(db, account_id: str) -> bool:
    """Whether this account's selection is the installation's: it is an
    active Admin now, in the transaction that would record it."""
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
    An eligible Admin's change of its active pair is, in the same
    transaction, the installation's login appearance."""
    async with _transaction() as db:
        before = await _read(db, account_id)
        saved = {**before, **change.model_dump(exclude_unset=True)}
        await db.execute(
            "INSERT INTO account_appearance(account_id,mode,dark_palette,light_palette,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET mode=excluded.mode, "
            "dark_palette=excluded.dark_palette, light_palette=excluded.light_palette, "
            "updated_at=excluded.updated_at",
            (str(account_id), saved["mode"], saved["dark_palette"], saved["light_palette"],
             datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
        if _active(saved) != _active(before) and await _selects_login(db, account_id):
            await _project(db, _active(saved))
    return saved


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
