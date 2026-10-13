"""Test support for proofs whose subject is an authenticated account.

Local credentials are account state (``auth.accounts``), held in the database.
``account_database`` gives a test a real database initialized by the canonical
schema owner (which creates the two default accounts); ``admin_with_password``
puts a local password on the default Admin through the same owner the
configuration-file credential uses. ``account`` is an in-memory account for
proofs that never leave the session store.
"""
from __future__ import annotations

from pathlib import Path

from auth.accounts import DEFAULT_ADMIN_ID, Account, Role


async def account_database(tmp_path: Path, monkeypatch) -> Path:
    import db.database as database

    path = tmp_path / "accounts.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    return path


async def admin_with_password(username: str = "operator", password: str = "secret") -> Account:
    from auth.accounts import get_account, import_legacy_credential
    from auth.passwords import hash_password

    await import_legacy_credential(username, hash_password(password))
    return await get_account(DEFAULT_ADMIN_ID)


def account(username: str = "operator", *, account_id: str = DEFAULT_ADMIN_ID, role: Role = Role.ADMIN,
            generation: int = 1, credential_version: str = "0" * 64) -> Account:
    return Account(
        id=account_id, role=role, origin="local", display_name=username, email="", email_verified=False,
        enabled=True, protected="", generation=generation, merged_into="", deleted_at="",
        username=username, credential_version=credential_version,
    )


def standalone_boundary(app) -> None:
    """Attach, to an application built without the authentication boundary,
    the principal that boundary attaches in open mode: the standalone
    installation's default Admin."""
    from auth.models import Principal

    @app.middleware("http")
    async def _standalone(request, call_next):
        request.state.principal = Principal.standalone()
        return await call_next(request)
