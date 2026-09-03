"""AllDebrid account administration and provider-specific status truth."""
from providers.alldebrid.client import AllDebridAPIError, AllDebridService


async def account_status(settings):
    client = AllDebridService(settings.alldebrid_api_key, settings.alldebrid_agent)
    user = await client.get_user()
    value = user.get("user", user)
    return {"ok": True, "username": value.get("username", ""), "isPremium": value.get("isPremium", False),
            "premiumUntil": value.get("premiumUntil", value.get("premium_until", 0))}


def _auth_failure(code: str) -> bool:
    normalized = str(code or "").strip().upper()
    return any(token in normalized for token in ("AUTH", "APIKEY", "API_KEY", "TOKEN"))


async def runtime_status(provider):
    """Return only facts established by the currently registered provider.

    Disabled/unconfigured state is resolved before network I/O. Enabled,
    configured providers are probed directly so generic API/statistics success,
    route activity, aria2 state, and retained host snapshots can never synthesize
    provider health. A failed provider-specific probe is therefore a truthful
    provider failure; failure to reach this endpoint is handled by the UI as
    unknown instead.
    """
    if provider is None or not provider.descriptor.enabled:
        return {"integration": "alldebrid", "state": "disabled", "checked": False}

    client = getattr(provider, "client", None)
    if client is None or not str(getattr(client, "api_key", "") or "").strip():
        return {"integration": "alldebrid", "state": "unconfigured", "checked": False}

    try:
        user = await client.get_user()
    except AllDebridAPIError as exc:
        return {
            "integration": "alldebrid",
            "state": "auth_required" if _auth_failure(exc.code) else "unhealthy",
            "checked": True,
        }
    except Exception:
        return {"integration": "alldebrid", "state": "unhealthy", "checked": True}

    value = user.get("user", user) if isinstance(user, dict) else {}
    if not isinstance(value, dict):
        value = {}
    return {
        "integration": "alldebrid",
        "state": "healthy",
        "checked": True,
        "username": str(value.get("username", "") or ""),
        "isPremium": bool(value.get("isPremium", False)),
        "premiumUntil": value.get("premiumUntil", value.get("premium_until", 0)),
    }
