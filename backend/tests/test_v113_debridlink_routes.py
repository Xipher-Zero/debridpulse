"""Debrid-Link control plane: the API key is written, tested and erased only
through the canonical integration machinery, like every other provider's."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from providers.debridlink.client import DebridLinkAPIError
from providers.debridlink.definition import definition
from test_v113_torbox_routes import _application, _settings_owner

pytestmark = pytest.mark.asyncio

KEY = "dl-private-api-key-routes"
ACCOUNT = {"username": "amy", "account": {"entitlement": "ready", "service_class": "premium", "plan": "Premium",
                                          "expires_at": None}}


class _Stored:
    def __init__(self, enabled=False, **options):
        from core.config import AppSettings
        from integrations.definition import IntegrationSettings
        self.cfg = AppSettings(integrations={"debridlink": IntegrationSettings(enabled=enabled, options=options)})

    def read(self):
        return self.cfg.model_copy(deep=True)

    def write(self, cfg):
        self.cfg = cfg


def _debridlink_application():
    application = _application()
    application.definitions = (definition,)
    return application


async def test_a_test_of_the_saved_key_verifies_it_and_never_enables():
    from api import settings_validation_routes as routes
    stored, refresh = _Stored(api_key=KEY), _debridlink_application()
    verify = AsyncMock(return_value=ACCOUNT)
    with _settings_owner(stored), patch.object(routes.debridlink_admin, "verify", verify):
        result = await routes.validate_debridlink(routes.DebridLinkValidationRequest(), application=refresh)
    assert verify.await_args.args[0] == KEY
    assert result["integration"]["verified"] is True and result["integration"]["enabled"] is False
    assert stored.cfg.integrations["debridlink"].enabled is False
    refresh.refresh_account_entitlement.assert_awaited_once_with("debridlink")
    assert KEY not in json.dumps(result)


async def test_a_draft_key_is_proven_but_verifies_nothing_saved():
    from api import settings_validation_routes as routes
    stored = _Stored(api_key=KEY)
    with _settings_owner(stored), patch.object(routes.debridlink_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        result = await routes.validate_debridlink(routes.DebridLinkValidationRequest(api_key="draft-key"),
                                                  application=_debridlink_application())
    assert result["ok"] is True and result["verification"] and "integration" not in result
    from integrations.configuration import public_integrations
    assert public_integrations(stored.cfg, (definition,))["debridlink"]["verified"] is False
    assert "draft-key" not in json.dumps(result)


async def test_a_failed_test_of_the_saved_key_retires_its_proof():
    from api import settings_validation_routes as routes
    stored = _Stored(api_key=KEY)
    with _settings_owner(stored), patch.object(routes.debridlink_admin, "verify", AsyncMock(return_value=ACCOUNT)):
        proven = await routes.validate_debridlink(routes.DebridLinkValidationRequest(),
                                                  application=_debridlink_application())
    assert proven["integration"]["verified"] is True
    refused = AsyncMock(side_effect=DebridLinkAPIError("badToken", 401))
    with _settings_owner(stored), patch.object(routes.debridlink_admin, "verify", refused), \
            pytest.raises(Exception) as failure:
        await routes.validate_debridlink(routes.DebridLinkValidationRequest(), application=_debridlink_application())
    assert getattr(failure.value, "status_code", None) == 502
    from integrations.configuration import public_integrations
    assert public_integrations(stored.cfg, (definition,))["debridlink"]["verified"] is False


async def test_no_key_at_all_is_refused_before_any_network():
    from api import settings_validation_routes as routes
    verify = AsyncMock()
    with _settings_owner(_Stored()), patch.object(routes.debridlink_admin, "verify", verify), \
            pytest.raises(Exception) as missing:
        await routes.validate_debridlink(routes.DebridLinkValidationRequest(), application=_debridlink_application())
    assert getattr(missing.value, "status_code", None) == 400 and verify.await_count == 0


async def test_the_key_is_saved_and_erased_through_the_canonical_mutation():
    from api.routes import IntegrationConfigurationUpdate, patch_integration_configuration
    stored = _Stored()
    with _settings_owner(stored):
        saved = await patch_integration_configuration("debridlink", IntegrationConfigurationUpdate(
            options={"api_key": KEY}), _debridlink_application())
        assert stored.cfg.integrations["debridlink"].options["api_key"] == KEY
        assert KEY not in json.dumps(saved, default=str)
        await patch_integration_configuration("debridlink", IntegrationConfigurationUpdate(
            options={}, clear_secrets=["api_key"]), _debridlink_application())
    assert stored.cfg.integrations["debridlink"].options["api_key"] == ""
