"""Temporary UIARCH-002 remediation applicator. Removed by successful runner."""
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    target.write_text(text.replace(old, new, 1))


# Concrete provider status remains at the AllDebrid edge and probes the currently
# registered provider rather than deriving health from generic application state.
replace_once(
    "backend/providers/alldebrid/admin.py",
    '''"""AllDebrid account administration for its explicit settings endpoint."""\nfrom providers.alldebrid.client import AllDebridService\n\n\nasync def account_status(settings):\n    client = AllDebridService(settings.alldebrid_api_key, settings.alldebrid_agent)\n    user = await client.get_user()\n    value = user.get("user", user)\n    return {"ok": True, "username": value.get("username", ""), "isPremium": value.get("isPremium", False),\n            "premiumUntil": value.get("premiumUntil", value.get("premium_until", 0))}\n''',
    '''"""AllDebrid account administration and provider-specific status truth."""\nfrom providers.alldebrid.client import AllDebridAPIError, AllDebridService\n\n\nasync def account_status(settings):\n    client = AllDebridService(settings.alldebrid_api_key, settings.alldebrid_agent)\n    user = await client.get_user()\n    value = user.get("user", user)\n    return {"ok": True, "username": value.get("username", ""), "isPremium": value.get("isPremium", False),\n            "premiumUntil": value.get("premiumUntil", value.get("premium_until", 0))}\n\n\ndef _auth_failure(code: str) -> bool:\n    normalized = str(code or "").strip().upper()\n    return any(token in normalized for token in ("AUTH", "APIKEY", "API_KEY", "TOKEN"))\n\n\nasync def runtime_status(provider):\n    """Return only facts established by the currently registered provider.\n\n    Disabled/unconfigured state is resolved before network I/O. Enabled,\n    configured providers are probed directly so generic API/statistics success,\n    route activity, aria2 state, and retained host snapshots can never synthesize\n    provider health. A failed provider-specific probe is therefore a truthful\n    provider failure; failure to reach this endpoint is handled by the UI as\n    unknown instead.\n    """\n    if provider is None or not provider.descriptor.enabled:\n        return {"integration": "alldebrid", "state": "disabled", "checked": False}\n\n    client = getattr(provider, "client", None)\n    if client is None or not str(getattr(client, "api_key", "") or "").strip():\n        return {"integration": "alldebrid", "state": "unconfigured", "checked": False}\n\n    try:\n        user = await client.get_user()\n    except AllDebridAPIError as exc:\n        return {\n            "integration": "alldebrid",\n            "state": "auth_required" if _auth_failure(exc.code) else "unhealthy",\n            "checked": True,\n        }\n    except Exception:\n        return {"integration": "alldebrid", "state": "unhealthy", "checked": True}\n\n    value = user.get("user", user) if isinstance(user, dict) else {}\n    if not isinstance(value, dict):\n        value = {}\n    return {\n        "integration": "alldebrid",\n        "state": "healthy",\n        "checked": True,\n        "username": str(value.get("username", "") or ""),\n        "isPremium": bool(value.get("isPremium", False)),\n        "premiumUntil": value.get("premiumUntil", value.get("premium_until", 0)),\n    }\n''',
)

# Expose that provider-owned truth through the existing concrete settings/API edge.
replace_once(
    "backend/api/settings_validation_routes.py",
    "from providers.alldebrid.client import AllDebridService\n",
    "from providers.alldebrid.admin import runtime_status as alldebrid_runtime_status\nfrom providers.alldebrid.client import AllDebridService\n",
)
replace_once(
    "backend/api/settings_validation_routes.py",
    '''@router.post("/settings/validate-alldebrid")\nasync def validate_alldebrid(payload: AllDebridValidationRequest):\n''',
    '''@router.get("/integration-status/alldebrid")\nasync def get_alldebrid_runtime_status(application: ApplicationService = Depends(get_application)):\n    """Return AllDebrid-specific status without inferring from generic health."""\n    provider = application.engine.registry.providers.get("alldebrid")\n    return await alldebrid_runtime_status(provider)\n\n\n@router.post("/settings/validate-alldebrid")\nasync def validate_alldebrid(payload: AllDebridValidationRequest):\n''',
)

# Remove every generic source of AllDebrid truth and render only provider endpoint facts.
replace_once(
    "frontend/static/app.js",
    "      setDot('api', 'ok', 'AllDebrid: online');\n",
    "",
)
replace_once(
    "frontend/static/app.js",
    '''async function checkConnections() {\n  // AllDebrid dot is already set by loadStats() — skip duplicate /stats call\n  const cfg = settingsData || {};\n\n  // aria2 check — retry once if first attempt fails\n''',
    '''function renderAllDebridStatus(status) {\n  const state = String(status?.state || 'unknown');\n  const username = String(status?.username || '').trim();\n  const presentation = {\n    disabled:      ['warn',  'AllDebrid: disabled'],\n    unconfigured:  ['warn',  'AllDebrid: not configured'],\n    auth_required: ['error', 'AllDebrid: authentication required'],\n    healthy:       ['ok',    `AllDebrid: ${username || 'online'}`],\n    unhealthy:     ['error', 'AllDebrid: unavailable'],\n    unknown:       ['check', 'AllDebrid: status unknown'],\n  }[state] || ['check', 'AllDebrid: status unknown'];\n  setDot('api', presentation[0], presentation[1]);\n  if (state === 'healthy') _updatePremiumLabel(status);\n  else _updatePremiumLabel(null);\n}\n\nasync function loadAllDebridStatus() {\n  try {\n    const status = await api('GET', '/integration-status/alldebrid');\n    renderAllDebridStatus(status);\n    return status;\n  } catch (_) {\n    // Failure of the generic application/API path cannot establish provider\n    // failure. Preserve that distinction by rendering a neutral unknown state.\n    renderAllDebridStatus({state:'unknown'});\n    return null;\n  }\n}\n\nasync function checkConnections() {\n  const cfg = settingsData || {};\n  await loadAllDebridStatus();\n\n  // aria2 check — retry once if first attempt fails\n''',
)
replace_once(
    "frontend/static/app.js",
    '''\n\nasync function checkPremiumStatus() {\n  try {\n    const cfg = settingsData;\n    if (!cfg || !cfg.alldebrid_api_key_configured) return;\n    const r = await api('POST', '/settings/test-alldebrid');\n    _updatePremiumLabel(r);\n    setDot('api', 'ok', `AllDebrid: ${r.username||'online'}`);\n  } catch { /* silent — dot already set by checkConnections */ }\n}\n''',
    "",
)
replace_once(
    "frontend/static/app.js",
    '''  checkConnections().catch(() => {});\n  checkPremiumStatus().catch(() => {});\n\n  if (!statsLoaded) {\n\n    setDot(\n      'api',\n      'error',\n      'AllDebrid: Error'\n    );\n  }\n\n  setInterval(\n    checkPremiumStatus,\n    12 * 60 * 60 * 1000\n  );\n''',
    '''  checkConnections().catch(() => {});\n\n  // Generic statistics availability says nothing about provider health.\n  // checkConnections() owns the provider-specific status surface.\n''',
)

Path("backend/tests/test_alldebrid_status_contract.py").write_text(r'''"""UIARCH-002 provider-status authority contracts."""
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
''')

Path("backend/tests/test_ui_provider_status_contract.py").write_text(r'''"""Static UIARCH-002 guards against generic provider-truth synthesis."""
from pathlib import Path
import re


APP = (Path(__file__).parents[2] / "frontend" / "static" / "app.js").read_text()


def body(name: str, next_name: str) -> str:
    pattern = rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{(.*?)(?=(?:async )?function {re.escape(next_name)}\()"
    match = re.search(pattern, APP, re.S)
    assert match, f"could not isolate {name}()"
    return match.group(1)


def test_stats_success_never_sets_alldebrid_status():
    section = body("loadStats", "goToTorrentPage")
    assert "setDot('api'" not in section
    assert 'AllDebrid:' not in section


def test_connection_check_uses_provider_specific_status_endpoint():
    section = body("checkConnections", "setDot")
    assert "await loadAllDebridStatus()" in section
    assert "/stats" not in section


def test_provider_endpoint_failure_renders_unknown_not_unhealthy():
    section = body("loadAllDebridStatus", "checkConnections")
    assert "renderAllDebridStatus({state:'unknown'})" in section
    assert "setDot('api', 'error'" not in section


def test_generic_stats_startup_failure_cannot_mark_alldebrid_error():
    init = APP[APP.index("// ── Init") : APP.index("// ── Extraction Password List")]
    assert "AllDebrid: Error" not in init
    assert "checkPremiumStatus" not in init


def test_no_legacy_generic_online_literal_or_stats_comment_remains():
    assert "AllDebrid: online" not in APP
    assert "AllDebrid dot is already set by loadStats" not in APP


def test_renderer_has_truthful_neutral_and_provider_states():
    section = body("renderAllDebridStatus", "loadAllDebridStatus")
    for state in ("disabled", "unconfigured", "auth_required", "healthy", "unhealthy", "unknown"):
        assert state in section
''')
