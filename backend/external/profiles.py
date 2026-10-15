"""External delivery profiles: the Admin-owned catalog an external client's
category selects from (``AppSettings.external_delivery``).

A profile decides what a client may deliver and how a delivered path reads
from the client's side; it never decides where DebridPulse writes, which
provider or executor works, or a queue position. Its ``local_root`` confines
delivery to completed material DebridPulse already wrote beneath it, and its
``import_root`` is the same directory as the external client sees it (a
shared volume mounted elsewhere). Nothing is copied, linked or moved.

Written only through ``PUT /api/external/profiles`` (Admin); the whole-
settings ``PUT /api/settings`` carries it forward unchanged.
"""
from __future__ import annotations

import posixpath
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Every source kind the native submit operation accepts (``external.intake``).
SOURCE_KINDS = ("links", "magnet", "nzb_link", "nzb", "torrent")
MAX_PROFILES = 64
MAX_CATEGORIES = 32
MAX_CLIENTS = 256
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


def _absolute(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("/") or "\x00" in text or len(text) > 1024:
        raise ValueError(f"{field} must be an absolute path")
    normalized = posixpath.normpath(text)
    if normalized != text.rstrip("/") and not (text == "/" and normalized == "/"):
        raise ValueError(f"{field} must be a normalized absolute path")
    return normalized


def category_key(value: str) -> str:
    """How a category is compared: case-insensitively, trimmed."""
    return str(value or "").strip().casefold()


class DeliveryProfile(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    # The external categories that select this profile.
    categories: list[str] = Field(default_factory=list, max_length=MAX_CATEGORIES)
    # The external clients (``account_tokens.client_id``) allowed to use it.
    # Explicit: an empty list admits nobody.
    clients: list[str] = Field(default_factory=list, max_length=MAX_CLIENTS)
    source_kinds: list[Literal["links", "magnet", "nzb_link", "nzb", "torrent"]] = Field(
        default_factory=lambda: list(SOURCE_KINDS))
    representation: Literal["shared_path"] = "shared_path"
    # DebridPulse's own view of the shared directory: the download folder or
    # a directory beneath it.
    local_root: str
    # The same directory as the external client sees it; empty = the same path.
    import_root: str = ""
    # How long a committed delivery is advertised. Expiry withdraws the
    # advertisement; it deletes nothing.
    retention_hours: int = Field(default=168, ge=1, le=8760)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("profile id must be lowercase letters, digits, '.', '_' or '-'")
        return value

    @field_validator("categories")
    @classmethod
    def _categories(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            text = str(value or "").strip()
            if not text or len(text) > 64 or any(ord(char) < 32 for char in text):
                raise ValueError("a category is 1-64 printable characters")
            cleaned.append(text)
        if len({category_key(item) for item in cleaned}) != len(cleaned):
            raise ValueError("a profile lists a category twice")
        return cleaned

    @field_validator("clients")
    @classmethod
    def _clients(cls, values: list[str]) -> list[str]:
        cleaned = [str(value or "").strip() for value in values]
        if any(not value or len(value) > 64 for value in cleaned):
            raise ValueError("a client is a client ID")
        return sorted(set(cleaned))

    @field_validator("source_kinds")
    @classmethod
    def _kinds(cls, values: list[str]) -> list[str]:
        return [kind for kind in SOURCE_KINDS if kind in set(values)]

    @field_validator("local_root")
    @classmethod
    def _local_root(cls, value: str) -> str:
        return _absolute(value, "local_root")

    @field_validator("import_root")
    @classmethod
    def _import_root(cls, value: str) -> str:
        return _absolute(value, "import_root") if str(value or "").strip() else ""

    @property
    def effective_import_root(self) -> str:
        return self.import_root or self.local_root

    def public(self) -> dict:
        """What a permitted client may know about the profile: never its
        client list or DebridPulse's own local root."""
        return {"id": self.id, "categories": list(self.categories), "source_kinds": list(self.source_kinds),
                "representation": self.representation, "import_root": self.effective_import_root,
                "retention_hours": self.retention_hours}


class ExternalDeliverySettings(BaseModel):
    profiles: list[DeliveryProfile] = Field(default_factory=list, max_length=MAX_PROFILES)
    # Bumped by every write: a write names the revision it read.
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _unique(self) -> "ExternalDeliverySettings":
        ids = [profile.id for profile in self.profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("two profiles share an id")
        seen: set[str] = set()
        for profile in self.profiles:
            for category in profile.categories:
                key = category_key(category)
                if key in seen:
                    raise ValueError(f"category {category!r} selects more than one profile")
                seen.add(key)
        return self

    def select(self, client_id: str, category: str) -> DeliveryProfile | None:
        """The profile ``category`` selects for ``client_id``, or ``None``
        (unknown, or not authorized for that client: indistinguishable)."""
        key = category_key(category)
        for profile in self.profiles:
            if key in {category_key(item) for item in profile.categories}:
                return profile if client_id in profile.clients else None
        return None

    def for_client(self, client_id: str) -> list[DeliveryProfile]:
        return [profile for profile in self.profiles if client_id in profile.clients]

    def by_id(self, profile_id: str) -> DeliveryProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)


def confined_to(download_folder: str, local_root: str) -> bool:
    """Whether a profile's ``local_root`` is the download folder or beneath it."""
    base = posixpath.normpath(str(download_folder or "/download"))
    root = posixpath.normpath(str(local_root or ""))
    return root == base or root.startswith(base.rstrip("/") + "/")
