import pytest

from account_support import account_database, admin_with_password
from api.auth_config_routes import AuthenticationConfigUpdate, _prospective_password_ready
from auth.models import Principal
from core.config import AppSettings


@pytest.mark.asyncio
async def test_route_clear_password_intent_wins_over_simultaneous_plaintext(tmp_path, monkeypatch):
    await account_database(tmp_path, monkeypatch)
    operator = await admin_with_password("operator", "old-secret")
    candidate = AppSettings(auth_password_enabled=True)
    update = AuthenticationConfigUpdate(
        auth_password_enabled=True,
        auth_password="new-but-discarded",
        clear_password=True,
    )

    principal = Principal.password_session(operator)
    assert await _prospective_password_ready(candidate, update, principal, operator.username) is False
