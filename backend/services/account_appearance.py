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


async def update(account_id: str, change: AppearanceUpdate) -> dict:
    """Apply one account's change to its own record, atomically: only the
    named choices change; the others keep what is stored (or their default)."""
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
