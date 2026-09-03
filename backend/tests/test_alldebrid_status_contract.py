"""UIARCH-002 provider-status authority contracts."""
from types import SimpleNamespace

import pytest

from providers.alldebrid.admin import runtime_status
from providers.alldebrid.client import AllDebridAPIError


class Client:
    def __init__(self, api_key="key", *, response=None, error=None):
        self.api_key = api_key
        self.response = response or {"user": {"username": "alice", "isPremium": True, "premiumUntil": 123}}
        self.error = error
        self.calls = 0

    async def get_user(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def provider(*, enabled=True, api_key="key", response=None, error=None):
    client = Client(api_key, response=response, error=error)
    return SimpleNamespace(descriptor=SimpleNamespace(enabled=enabled), client=client), client


@pytest.mark.asyncio
async def test_disabled_overrides_any_retained_or_reachable_provider_state():
    item, client = provider(enabled=False)
    result = await runtime_status(item)
    assert result == {"integration": "alldebrid", "state": "disabled", "checked": False}
    assert client.calls == 0


@pytest.mark.asyncio
async def test_missing_credentials_are_unconfigured_without_provider_contact():
    item, client = provider(enabled=True, api_key="")
    result = await runtime_status(item)
    assert result == {"integration": "alldebrid", "state": "unconfigured", "checked": False}
    assert client.calls == 0


@pytest.mark.asyncio
async def test_live_provider_success_is_the_only_source_of_healthy_state():
    item, client = provider()
    result = await runtime_status(item)
    assert result["state"] == "healthy"
    assert result["checked"] is True
    assert result["username"] == "alice"
    assert result["isPremium"] is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_provider_auth_failure_is_truthful_auth_required_state():
    item, _client = provider(error=AllDebridAPIError("AUTH_BAD_APIKEY", "bad key"))
    result = await runtime_status(item)
    assert result["state"] == "auth_required"
    assert result["checked"] is True


@pytest.mark.asyncio
async def test_provider_network_failure_is_truthful_unhealthy_state():
    item, _client = provider(error=TimeoutError("provider timeout"))
    result = await runtime_status(item)
    assert result["state"] == "unhealthy"
    assert result["checked"] is True


@pytest.mark.asyncio
async def test_absent_provider_is_disabled_not_inferred_from_generic_health():
    assert await runtime_status(None) == {
        "integration": "alldebrid", "state": "disabled", "checked": False,
    }
