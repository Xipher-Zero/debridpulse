"""1.0.13 Gate-9 rev-3: SAB is a DP-private internal service.

The supported topology bundles the Usenet acquisition service inside the
DebridPulse container. It binds loopback only, is never published, has no
operator-facing web UI, and its control-plane endpoint and credential are
implementation-private runtime state -- never operator settings.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# --- the runtime is DP-owned and private --------------------------------

def test_the_service_endpoint_is_loopback_and_constructed_in_code():
    import ast
    from executors.sabnzbd import runtime
    assert runtime.SERVICE_HOST == "127.0.0.1"
    assert runtime.service_url().startswith("http://127.0.0.1:")
    # The real invariant: the runtime never reads configuration. Asserted
    # against imports and names, not against prose.
    tree = ast.parse(inspect.getsource(runtime))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
    assert not any("core.config" in name or name.endswith("settings") for name in imported), imported
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("get_settings", "AppSettings", "load_settings"):
        assert forbidden not in names, forbidden


def test_the_internal_api_key_is_generated_not_configured():
    from executors.sabnzbd import runtime
    key = runtime.internal_api_key()
    assert isinstance(key, str) and len(key) >= 32
    # Stable within a run, so the client and the service agree.
    assert runtime.internal_api_key() == key


def test_the_runtime_is_a_managed_integration_lifecycle():
    from integrations.definition import IntegrationLifecycle, ManagedIntegration
    from executors.sabnzbd.admin import SabnzbdAdministration
    for name in ("start", "stop", "maintain"):
        assert callable(getattr(SabnzbdAdministration, name, None)), name
    assert isinstance(IntegrationLifecycle, type(ManagedIntegration))


@pytest.mark.asyncio
async def test_composition_attaches_the_lifecycle_generically(tmp_path):
    from types import SimpleNamespace
    from application.composition import integration_surfaces
    from integrations.catalog import definitions, register
    from integrations.definition import IntegrationEnvironment, IntegrationSettings
    from transfers.registry import IntegrationRegistry

    settings = SimpleNamespace(integrations={d.id: IntegrationSettings() for d in definitions})
    registry = IntegrationRegistry()
    register(registry, settings, IntegrationEnvironment(
        SimpleNamespace(authorize_execution=None, converge_staged_input=None), str(tmp_path)),
        selected=[d for d in definitions if d.id == "usenet"])
    lifecycle, admins, appliers = integration_surfaces(registry)
    assert lifecycle, "the internal service must participate in the application lifecycle"
    assert "usenet" in appliers


# --- nothing about the service is operator-configurable ------------------

def test_usenet_options_carry_no_service_url_or_api_key():
    from integrations.usenet.definition import UsenetOptions
    fields = set(UsenetOptions.model_fields)
    for forbidden in ("service_url", "api_key", "sab_url", "sab_api_key", "host", "port"):
        assert forbidden not in fields, forbidden


def test_the_public_projection_exposes_no_service_endpoint_or_credential():
    from integrations.usenet.definition import UsenetOptions, definition as usenet
    import json
    public = usenet.public_options(UsenetOptions().model_dump())
    blob = json.dumps(public).lower()
    for forbidden in ("service_url", "api_key", "127.0.0.1", "8080", "sabnzbd"):
        assert forbidden not in blob, forbidden


def test_the_settings_ui_offers_no_service_url_or_api_key_control():
    settings_js = (REPO / "frontend/static/ui-settings-page.js").read_text(encoding="utf-8")
    for forbidden in ("usenet_service_url", "usenet_api_key"):
        assert forbidden not in settings_js, forbidden


def test_no_operator_surface_names_the_daemon():
    settings_js = (REPO / "frontend/static/ui-settings-page.js").read_text(encoding="utf-8")
    start = settings_js.index("function usenetServerCard(")
    end = settings_js.index("const ARIA2_LIVE_FILTERS")
    assert "SABnzbd" not in settings_js[start:end]
    servers_js = (REPO / "frontend/static/ui-settings-usenet-servers.js").read_text(encoding="utf-8")
    assert "SABnzbd" not in servers_js and "sabnzbd" not in servers_js


# --- configured means USABLE, not merely reachable -----------------------

def test_configured_requires_at_least_one_usable_server():
    from integrations.usenet.definition import UsenetOptions, UsenetServer, definition as usenet
    assert usenet.configured(UsenetOptions().model_dump()) is False
    disabled = UsenetOptions(servers=[UsenetServer(host="news.a.net", enabled=False)])
    assert usenet.configured(disabled.model_dump()) is False
    hostless = UsenetOptions(servers=[UsenetServer(host="")])
    assert usenet.configured(hostless.model_dump()) is False
    usable = UsenetOptions(servers=[UsenetServer(host="news.a.net")])
    assert usenet.configured(usable.model_dump()) is True


def test_enabled_with_zero_servers_is_enabled_but_unconfigured():
    from integrations.usenet.definition import UsenetOptions, definition as usenet
    # The internal service existing must NOT make the integration "configured".
    assert usenet.configured(UsenetOptions().model_dump()) is False


# --- the container never publishes the service ---------------------------

def test_the_image_bundles_the_service_and_publishes_only_the_app_port():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "sabnzbd" in dockerfile.lower(), "the acquisition service must be bundled"
    exposed = [line for line in dockerfile.splitlines() if line.strip().upper().startswith("EXPOSE")]
    assert exposed, "the image must declare its published port"
    for line in exposed:
        assert "8080" in line
        for port in ("8081", "8090", "9090"):
            assert port not in line
    # The service's own port is never published, and its UI is never proxied.
    assert "EXPOSE 8090" not in dockerfile


def test_no_reverse_proxy_of_the_service_web_ui():
    main = (REPO / "backend/main.py").read_text(encoding="utf-8")
    for forbidden in ("sabnzbd", "proxy", "8090"):
        assert forbidden not in main.lower() or forbidden == "proxy"


def test_the_image_provides_what_the_service_needs_to_acquire_and_repair():
    """The bundled service refuses to acquire at all unless an unrar binary is
    present ("Essential modules are missing"), even though DebridPulse owns
    archive extraction and submits every job repair-only. par2 performs the
    posting's verification and repair.
    """
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    install = dockerfile[dockerfile.index("apt-get install -y --no-install-recommends \\"):
                         dockerfile.index("rm -rf /var/lib/apt/lists")]
    for package in ("par2", "unrar"):
        assert package in install, package
    # A non-free package still ships its licence text with the image.
    assert "path-include=/usr/share/doc/unrar/copyright" in dockerfile


def test_the_service_is_configured_never_to_unpack():
    from executors.sabnzbd import runtime
    import inspect
    source = inspect.getsource(runtime._write_bootstrap_ini)
    for disabled in ('"direct_unpack": "0"', '"enable_unrar": "0"', '"enable_7zip": "0"'):
        assert disabled in source, disabled
