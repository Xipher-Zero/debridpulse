from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class AuthMechanism(StrEnum):
    """Authentication mechanism that admitted the request."""

    PASSWORD_SESSION = "password_session"
    OIDC_SESSION = "oidc_session"
    HTTP_BASIC = "http_basic"
    API_TOKEN = "api_token"


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity attached to an admitted request at the authentication boundary."""

    authenticated: bool = False
    mechanism: AuthMechanism | None = None
    subject: str = ""
    display_name: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)
    # Non-secret fingerprint of the credential/configuration that actually
    # authenticated this principal. Session creation may bind to it so a proof
    # made under stale policy cannot be relabeled as current policy.
    credential_version: str = ""
    # The durable account (``auth.accounts``) this request acts as, its role
    # and the account generation the principal was resolved at. Empty only for
    # the unauthenticated principal and the legacy global API token, which
    # belongs to no account until its Stage 2 replacement.
    account_id: str = ""
    role: str = ""
    account_generation: int = 0

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls()

    @classmethod
    def standalone(cls) -> "Principal":
        """No interactive authentication: the one explicit Admin-equivalent
        principal of a standalone installation, the default Admin account. It
        authenticated nothing, so it is not ``authenticated``."""
        from auth.accounts import DEFAULT_ADMIN_ID, Role

        return cls(account_id=DEFAULT_ADMIN_ID, role=Role.ADMIN.value)

    @classmethod
    def password_session(cls, account) -> "Principal":
        return cls(
            authenticated=True,
            mechanism=AuthMechanism.PASSWORD_SESSION,
            subject=account.username,
            display_name=account.username,
            credential_version=account.credential_version,
        ).bound_to(account)

    @classmethod
    def oidc_session(
        cls,
        subject: str,
        *,
        display_name: str = "",
        claims: Mapping[str, Any] | None = None,
        credential_version: str = "",
    ) -> "Principal":
        return cls(
            authenticated=True,
            mechanism=AuthMechanism.OIDC_SESSION,
            subject=str(subject or ""),
            display_name=str(display_name or subject or ""),
            claims=dict(claims or {}),
            credential_version=str(credential_version or ""),
        )

    @classmethod
    def http_basic(cls, account) -> "Principal":
        return cls(
            authenticated=True,
            mechanism=AuthMechanism.HTTP_BASIC,
            subject=account.username,
            display_name=account.username,
        ).bound_to(account)

    @classmethod
    def api_token(cls) -> "Principal":
        return cls(
            authenticated=True,
            mechanism=AuthMechanism.API_TOKEN,
            subject="api-token",
            display_name="API token",
        )

    def bound_to(self, account) -> "Principal":
        """This principal acting as ``account``."""
        return dataclasses.replace(
            self,
            account_id=account.id,
            role=account.role.value,
            account_generation=int(account.generation),
        )
