from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Mapping

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from auth.models import AuthMechanism, Principal
from auth.oidc import OidcConfigurationError, oidc_configuration
from auth.policy import AUTH_CONFIG_PATH, interactive_auth_enabled, oidc_auth_enabled, password_auth_enabled


CRITICAL_OIDC_FIELDS = frozenset(
    {
        "oidc_issuer_url",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_scopes",
        "public_base_url",
        "oidc_allow_all",
        "oidc_allowed_subjects",
        "oidc_allowed_emails",
        "oidc_allowed_groups",
        "oidc_group_claim",
    }
)
_AUTH_SETTINGS_PATHS = frozenset({"/api/settings", "/api/auth/config"})

# Authentication configuration changes must be evaluated and committed against
# one authoritative state. Middleware holds this lock through the settings route
# so concurrent requests cannot both pass lockout checks against stale state.
authentication_configuration_lock = asyncio.Lock()
_BOOL_ADAPTER = TypeAdapter(bool)


def _coerced_bool(value: Any, *, default: bool = False) -> bool:
    """Apply the same bool coercion used by the Pydantic request models."""
    try:
        return bool(_BOOL_ADAPTER.validate_python(value))
    except ValidationError:
        # Invalid values are rejected later by FastAPI/Pydantic and are never
        # persisted. Falling back here avoids inventing different semantics in
        # the pre-route transition guard.
        return bool(default)


def is_auth_settings_mutation(request: Request) -> bool:
    return request.method.upper() == "PUT" and request.url.path in _AUTH_SETTINGS_PATHS


def _normalized_list(value: Any, *, casefold: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    items = []
    for raw in value:
        item = str(raw or "").strip()
        if not item:
            continue
        items.append(item.casefold() if casefold else item)
    return tuple(sorted(set(items)))


def _normalized_critical(field: str, value: Any) -> Any:
    if field == "oidc_allow_all":
        return _coerced_bool(value)
    if field == "oidc_scopes":
        scopes = list(_normalized_list(value))
        if "openid" not in scopes:
            scopes.append("openid")
        return tuple(sorted(set(scopes)))
    if field == "oidc_allowed_emails":
        return _normalized_list(value, casefold=True)
    if field in {"oidc_allowed_subjects", "oidc_allowed_groups"}:
        return _normalized_list(value)
    if field == "public_base_url":
        return str(value or "").strip().rstrip("/")
    if field == "oidc_issuer_url":
        # OIDC issuer identifiers are exact values. A trailing slash is not
        # equivalent to the same string without one.
        return str(value or "").strip()
    return str(value or "").strip()


def oidc_critical_change(payload: Mapping[str, Any], current) -> bool:
    clears = {str(item) for item in payload.get("clear_secrets", []) if str(item)}
    if "oidc_client_secret" in clears or _coerced_bool(payload.get("clear_oidc_client_secret", False)):
        return True
    for field in CRITICAL_OIDC_FIELDS:
        if field not in payload:
            continue
        proposed = payload[field]
        # Blank secret input is the preserve-existing sentinel. Any nonblank
        # replacement is critical even when it happens to equal the old value.
        if field == "oidc_client_secret":
            if str(proposed or "").strip():
                return True
            continue
        if _normalized_critical(field, proposed) != _normalized_critical(
            field,
            getattr(current, field, None),
        ):
            return True
    return False


async def _prospective_password_ready(request: Request, payload: Mapping[str, Any], principal: Principal) -> bool:
    """Whether, after this request, an active Admin could sign in with a password.

    Local credentials are account state (``auth.accounts``). Only the
    dedicated Authentication endpoint edits one -- the signed-in account's own
    -- so only its payload can add or remove a password here."""
    from auth.accounts import Role, admin_password_holders, local_credential_status

    holders = await admin_password_holders()
    own = principal.account_id
    if request.url.path != AUTH_CONFIG_PATH or not own:
        return bool(holders)
    clears = {str(item) for item in payload.get("clear_secrets", []) if str(item)}
    if "auth_password" in clears or _coerced_bool(payload.get("clear_password", False)):
        # Clear intent is destructive and wins over any simultaneously supplied
        # plaintext. The route does not persist that plaintext, so it must not be
        # counted as a viable replacement credential here.
        holders.discard(own)
    elif principal.role == Role.ADMIN.value and str(payload.get("auth_password", "") or ""):
        username, _configured = await local_credential_status(own)
        if str(payload.get("auth_username", username) or "").strip():
            holders.add(own)
    return bool(holders)


def _prospective_oidc_ready(payload: Mapping[str, Any], current) -> bool:
    """Return whether the proposed OIDC mechanism is locally usable.

    This intentionally does not claim provider reachability. It prevents the
    broad compatibility settings API from making an incomplete configuration,
    or a deny-everyone authorization policy, the installation's only
    interactive authentication mechanism.
    """
    candidate = copy.copy(current)
    fields = CRITICAL_OIDC_FIELDS | {"auth_oidc_enabled", "oidc_provider_name"}
    for field in fields:
        if field not in payload:
            continue
        value = payload[field]
        if field in {"auth_oidc_enabled", "oidc_allow_all"}:
            value = _coerced_bool(
                value,
                default=bool(getattr(current, field, False)),
            )
        # Blank secret input preserves the stored secret unless an explicit
        # clear flag is present, matching the normal settings-write contract.
        if field == "oidc_client_secret" and not str(value or "").strip():
            continue
        setattr(candidate, field, value)

    clears = {str(item) for item in payload.get("clear_secrets", []) if str(item)}
    if "oidc_client_secret" in clears or _coerced_bool(payload.get("clear_oidc_client_secret", False)):
        setattr(candidate, "oidc_client_secret", "")

    try:
        config = oidc_configuration(candidate)
    except OidcConfigurationError:
        return False

    # Deny-by-default remains the authorization model, but a deny-everyone
    # policy cannot safely become the sole interactive authentication path.
    return bool(
        config.allow_all
        or config.allowed_subjects
        or config.allowed_emails
        or config.allowed_groups
    )


async def settings_transition_rejection(
    request: Request,
    principal: Principal,
    current,
) -> Response | None:
    """Reject authentication transitions that can strand the appliance owner.

    Both the inherited broad Settings endpoint and the dedicated Authentication
    endpoint pass through this single state machine so UI/API compatibility
    cannot create a policy bypass.
    """
    if not is_auth_settings_mutation(request):
        return None

    try:
        raw = await request.body()
        payload = json.loads(raw or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None  # Let FastAPI return its normal request-validation error.
    if not isinstance(payload, dict):
        return None

    current_password = password_auth_enabled(current)
    current_oidc = oidc_auth_enabled(current)
    proposed_password = _coerced_bool(
        payload.get("auth_password_enabled", current_password),
        default=current_password,
    )
    proposed_oidc = _coerced_bool(
        payload.get("auth_oidc_enabled", current_oidc),
        default=current_oidc,
    )
    critical_oidc_change = oidc_critical_change(payload, current)

    # Explicitly opening an installation is supported, but only as an
    # authenticated and deliberately confirmed transition. Already-open
    # installations do not need to confirm that they remain open.
    if not proposed_password and not proposed_oidc and interactive_auth_enabled(current):
        if not principal.authenticated:
            return JSONResponse(
                {"detail": "Authentication is required for open-mode transition"},
                status_code=401,
            )
        if not _coerced_bool(payload.get("confirm_open_mode", False)):
            return JSONResponse(
                {"detail": "Explicit confirmation is required to disable all interactive authentication"},
                status_code=409,
            )
        return None

    # Any configured-auth state must retain at least one locally usable
    # interactive mechanism. This closes the legacy /api/settings bypass where
    # incomplete Password/OIDC settings could otherwise become authoritative
    # and immediately strand the operator. A broken supplemental mechanism is
    # still permitted when the other mechanism remains usable.
    password_ready = proposed_password and await _prospective_password_ready(request, payload, principal)
    oidc_ready = proposed_oidc and _prospective_oidc_ready(payload, current)
    if (proposed_password or proposed_oidc) and not (password_ready or oidc_ready):
        return JSONResponse(
            {"detail": "At least one enabled interactive authentication mechanism must be locally usable"},
            status_code=409,
        )

    # A fresh/open installation has no authenticated mechanism that can prove a
    # proposed OIDC deployment. Do not let local syntax/discovery configuration
    # become the only login path before any end-to-end OIDC proof exists. The
    # safe bootstrap sequence is a temporary Password fallback, Verify Sign-In,
    # then the already-protected Password -> OIDC-only transition below.
    enabling_unproven_oidc_as_first_auth = (
        not current_password
        and not current_oidc
        and proposed_oidc
        and not proposed_password
    )
    if enabling_unproven_oidc_as_first_auth:
        return JSONResponse(
            {
                "detail": (
                    "OIDC cannot become the first and only interactive authentication mechanism without a verified fallback; "
                    "enable Username & Password temporarily, verify OIDC sign-in, then disable Password"
                )
            },
            status_code=409,
        )

    # Leaving OIDC as the sole interactive mechanism is permitted only when
    # this exact request is made from a real OIDC application session. A local
    # password session, HTTP Basic, API token, discovery check, or configuration
    # test is insufficient proof of end-to-end OIDC browser access.
    disabling_password_to_oidc_only = current_password and not proposed_password and proposed_oidc
    if disabling_password_to_oidc_only:
        if critical_oidc_change:
            return JSONResponse(
                {"detail": "Verify the proposed OIDC configuration with a real sign-in before disabling Password"},
                status_code=409,
            )
        if principal.mechanism is not AuthMechanism.OIDC_SESSION:
            return JSONResponse(
                {"detail": "A current OIDC-authenticated session is required before disabling Password"},
                status_code=403,
            )

    # In OIDC-only mode the currently working config remains authoritative.
    # Critical changes must be staged and proven through the pending-config
    # flow. Enabling a known-usable local password in the same update restores
    # a fallback and therefore removes this lockout constraint.
    if not current_password and current_oidc and proposed_oidc and critical_oidc_change:
        fallback_ready = proposed_password and await _prospective_password_ready(request, payload, principal)
        if not fallback_ready:
            return JSONResponse(
                {"detail": "Critical OIDC changes require pending configuration verification while OIDC is the sole interactive mechanism"},
                status_code=409,
            )

    return None
