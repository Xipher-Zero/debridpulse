"""AllDebrid account administration for its explicit settings endpoint."""
from providers.alldebrid.client import AllDebridService


async def account_status(settings):
    client = AllDebridService(settings.alldebrid_api_key, settings.alldebrid_agent)
    user = await client.get_user()
    value = user.get("user", user)
    return {"ok": True, "username": value.get("username", ""), "isPremium": value.get("isPremium", False),
            "premiumUntil": value.get("premiumUntil", value.get("premium_until", 0))}
