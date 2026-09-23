"""Canonical per-server mutation of the ONE `integrations.usenet` namespace.

A news server is a durable record with a stable canonical id, so one card can be
edited, saved or removed without disturbing another. Every function here is a
pure transformation of `UsenetOptions` -- the caller performs the persisted
load -> mutate -> save under the existing configuration write lock, so there is
no second configuration owner and no parallel secret store.

Secret rule (the same contract the rest of Settings already uses): a blank
password means "keep the stored one for THIS server"; erasing requires an
explicit clear; a supplied non-empty value always wins.
"""
from __future__ import annotations

from integrations.usenet.definition import UsenetOptions, UsenetServer

SERVER_NOT_FOUND = "server_not_found"

# Fields a caller may set on a server record. `id` is canonical identity and is
# never reassigned; `password` is governed by the secret rule below.
MUTABLE_FIELDS = frozenset({
    "host", "port", "ssl", "username", "password", "connections", "priority",
    "enabled", "display_name",
})


class ServerMutationError(ValueError):
    """A per-server mutation that cannot be applied to the canonical state."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _replace(options: UsenetOptions, servers: list[UsenetServer]) -> UsenetOptions:
    return options.model_copy(update={"servers": servers})


def _apply(existing: UsenetServer | None, values: dict, *, clear_password: bool) -> dict:
    """The field values a record should end up with."""
    base = existing.model_dump() if existing is not None else {}
    supplied = {key: value for key, value in (values or {}).items() if key in MUTABLE_FIELDS}

    password = supplied.pop("password", None)
    base.update(supplied)
    if password:
        # A supplied value is authoritative and overrides a contradictory clear.
        base["password"] = password
    elif clear_password:
        base["password"] = ""
    elif existing is not None:
        base["password"] = existing.password
    else:
        base["password"] = ""
    return base


def create_server(options: UsenetOptions, values: dict, *,
                  clear_password: bool = False) -> tuple[UsenetOptions, UsenetServer]:
    """Append one new server, minting its canonical id. Others are untouched."""
    created = UsenetServer(**_apply(None, values, clear_password=clear_password))
    return _replace(options, [*options.servers, created]), created


def merge_server(options: UsenetOptions, server_id: str, values: dict, *,
                 clear_password: bool = False) -> UsenetOptions:
    """Update exactly the server with ``server_id``; never any other record."""
    servers = list(options.servers)
    index = _index_of(servers, server_id)
    existing = servers[index]
    servers[index] = UsenetServer(
        **{**_apply(existing, values, clear_password=clear_password), "id": existing.id})
    return _replace(options, servers)


def remove_server(options: UsenetOptions, server_id: str) -> UsenetOptions:
    """Drop exactly one server, leaving every survivor byte-identical."""
    servers = list(options.servers)
    del servers[_index_of(servers, server_id)]
    return _replace(options, servers)


def find_server(options: UsenetOptions, server_id: str) -> UsenetServer | None:
    return next((item for item in options.servers if item.id == str(server_id)), None)


def _index_of(servers: list[UsenetServer], server_id: str) -> int:
    for position, item in enumerate(servers):
        if item.id == str(server_id):
            return position
    raise ServerMutationError(SERVER_NOT_FOUND)
