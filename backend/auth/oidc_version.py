from __future__ import annotations

import hashlib
import json
from typing import Any


_OIDC_BASELINE_FIELDS = (
    "auth_oidc_enabled",
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
)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def oidc_configuration_version_from_config(config: Any) -> str:
    """Fingerprint security-critical effective OIDC configuration without retaining secrets."""
    return _digest(
        {
            "issuer": str(getattr(config, "issuer", "") or ""),
            "client_id": str(getattr(config, "client_id", "") or ""),
            "client_secret": str(getattr(config, "client_secret", "") or ""),
            "scopes": list(getattr(config, "scopes", ()) or ()),
            "callback_url": str(getattr(config, "callback_url", "") or ""),
            "allow_all": bool(getattr(config, "allow_all", False)),
            "allowed_subjects": sorted(
                str(item) for item in (getattr(config, "allowed_subjects", ()) or ())
            ),
            "allowed_emails": sorted(
                str(item).casefold() for item in (getattr(config, "allowed_emails", ()) or ())
            ),
            "allowed_groups": sorted(
                str(item) for item in (getattr(config, "allowed_groups", ()) or ())
            ),
            "group_claim": str(getattr(config, "group_claim", "") or ""),
        }
    )


def oidc_configuration_version(settings: Any) -> str:
    """Return the effective critical OIDC configuration fingerprint, or empty if unusable."""
    from auth.oidc import OidcConfigurationError, oidc_configuration

    try:
        config = oidc_configuration(settings)
    except OidcConfigurationError:
        return ""
    return oidc_configuration_version_from_config(config)


def authentication_configuration_baseline_version(settings: Any) -> str:
    """Fingerprint live authentication state for pending-config stale-baseline checks.

    Unlike ``oidc_configuration_version`` this intentionally operates on raw
    stored settings so disabled or temporarily incomplete OIDC state is still
    distinguishable. It also includes whether Username & Password is enabled
    because a pending OIDC commit may explicitly change whether Password
    remains enabled. Local credentials are account state (``auth.accounts``),
    not authentication configuration.
    """
    payload: dict[str, Any] = {
        "auth_password_enabled": bool(getattr(settings, "auth_password_enabled", False)),
    }
    for field in _OIDC_BASELINE_FIELDS:
        value = getattr(settings, field, None)
        if field in {"oidc_scopes", "oidc_allowed_subjects", "oidc_allowed_groups"}:
            value = sorted(str(item) for item in (value or []))
        elif field == "oidc_allowed_emails":
            value = sorted(str(item).casefold() for item in (value or []))
        elif field in {"auth_oidc_enabled", "oidc_allow_all"}:
            value = bool(value)
        else:
            value = str(value or "")
        payload[field] = value
    return _digest(payload)
