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
    end = settings_js.index("const EXECUTOR_WORK_FILTERS")
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
    main = (REPO / "backend/main.py").read_text(encoding="utf-8").lower()
    # The one SABnzbd-named code the application may route to is DebridPulse's
    # own *arr compatibility dialect (``api.arr_sabnzbd``): the protocol
    # answered over DebridPulse's native external contract, never the
    # internal service. Nothing else names the service or its port.
    routed = main.replace("from api.arr_sabnzbd import router as arr_sabnzbd_router", "")
    routed = routed.replace("app.include_router(arr_sabnzbd_router", "")
    for forbidden in ("sabnzbd", "8090"):
        assert forbidden not in routed
    dialect = (REPO / "backend/api/arr_sabnzbd.py").read_text(encoding="utf-8")
    for forbidden in ("executors.sabnzbd", "executors import sabnzbd", "8090", "SERVICE_HOST"):
        assert forbidden not in dialect


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


# --- the service's own saved configuration survives a DebridPulse start ---

# What the bundled service writes once it has saved its configuration
# (characterized against SABnzbd 5.1.3: configobj, a version header above the
# first section, nested [[name]] news-server and category sections).
_SERVICE_SAVED_INI = """\
__encoding__ = utf-8
__version__ = 19
[misc]
api_key = 0123456789abcdef0123456789abcdef
nzb_key = fedcba9876543210fedcba9876543210
config_conversion_version = 5
cache_limit = 500M
direct_write = 1
max_art_tries = 3
host = 0.0.0.0
port = 8080
download_dir = /elsewhere/incomplete
complete_dir = /elsewhere/complete
direct_unpack = 1
enable_unrar = 1
enable_7zip = 1
enable_filejoin = 1
[logging]
log_level = 1
[servers]
[[dp-srv1]]
name = dp-srv1
displayname = news.example.invalid
host = news.example.invalid
port = 563
username = reader
password = secret
connections = 8
ssl = 1
ssl_ciphers = ""
enable = 1
pipelining_requests = 2
priority = 0
quota = 10G
usage_at_start = 1234
[categories]
[[*]]
name = *
order = 0
pp = 3
dir = ""
[[movies]]
name = movies
order = 1
dir = movies
"""


def test_a_service_saved_configuration_survives_the_bootstrap(tmp_path, monkeypatch):
    """The bootstrap rewrites only DebridPulse-owned [misc] values.

    Everything else in the file is the service's own: its version header, news
    servers and categories, and [misc] keys DebridPulse never seeds. The
    installation's control-plane key is read back, never regenerated.
    """
    import configobj
    from executors.sabnzbd import runtime, topology
    monkeypatch.setattr(runtime, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "_api_key", "")  # a fresh DebridPulse process
    ini = tmp_path / "sabnzbd.ini"
    ini.write_text(_SERVICE_SAVED_INI, encoding="utf-8")
    before = configobj.ConfigObj(str(ini), encoding="utf-8")
    stored_key = before["misc"]["api_key"]

    assert runtime._read_ini()["misc"]["api_key"] == stored_key
    runtime._write_bootstrap_ini("/download")
    assert runtime.internal_api_key() == stored_key

    after = configobj.ConfigObj(str(ini), encoding="utf-8")
    for top in ("__version__", "__encoding__"):
        assert after.get(top) == before[top], top
    assert after.sections == before.sections
    for section in before.sections:
        if section != "misc":
            assert after[section].dict() == before[section].dict(), section
    owned = {"api_key", "host", "port", "username", "password", "download_dir",
             "complete_dir", "direct_unpack", "enable_filejoin", "enable_unrar",
             "enable_7zip", "permissions", "auto_browser", "check_new_rel"}
    for key, value in before["misc"].items():
        if key not in owned:
            assert after["misc"][key] == value, key
    # DebridPulse-owned values are (re)applied over whatever the service saved.
    assert after["misc"]["api_key"] == stored_key
    assert after["misc"]["host"] == runtime.SERVICE_HOST
    assert after["misc"]["port"] == str(runtime.SERVICE_PORT)
    assert after["misc"]["download_dir"] == topology.incomplete_root("/download")
    assert after["misc"]["complete_dir"] == topology.complete_root("/download")
    for disabled in ("direct_unpack", "enable_filejoin", "enable_unrar", "enable_7zip"):
        assert after["misc"][disabled] == "0", disabled
    # The control plane reads back the same key the service will use.
    monkeypatch.setattr(runtime, "_api_key", "")
    assert runtime.internal_api_key() == stored_key


def test_the_first_bootstrap_seeds_a_file_the_service_can_read(tmp_path, monkeypatch):
    import configobj
    from executors.sabnzbd import runtime
    monkeypatch.setattr(runtime, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "_api_key", "")
    runtime._write_bootstrap_ini("/download")
    key = runtime.internal_api_key()
    seeded = configobj.ConfigObj(str(tmp_path / "sabnzbd.ini"), encoding="utf-8")
    assert seeded["misc"]["api_key"] == key and seeded["misc"]["enable_unrar"] == "0"
    # A second start keeps the key it seeded.
    monkeypatch.setattr(runtime, "_api_key", "")
    runtime._write_bootstrap_ini("/download")
    assert runtime.internal_api_key() == key


def test_service_values_are_kept_verbatim_and_an_unreadable_file_behaves_as_before(tmp_path, monkeypatch):
    """Values are written back exactly as saved (no %(name)s or $ expansion, no
    requoting), a file that cannot be parsed still yields a fresh [misc] seed,
    and one that is not UTF-8 still raises."""
    import configobj
    from executors.sabnzbd import runtime
    monkeypatch.setattr(runtime, "CONFIG_ROOT", tmp_path)
    ini = tmp_path / "sabnzbd.ini"
    ini.write_text('__version__ = 19\n[misc]\napi_key = k\n[servers]\n[[dp-1]]\n'
                   'password = "p%(x)s$y%%z#q"\nname = "a, b"\n', encoding="utf-8")
    monkeypatch.setattr(runtime, "_api_key", "")
    runtime._write_bootstrap_ini("/download")
    server = configobj.ConfigObj(str(ini), encoding="utf-8", interpolation=False)["servers"]["dp-1"]
    assert server["password"] == "p%(x)s$y%%z#q" and server["name"] == "a, b"

    ini.write_bytes(b"\x00[[[\n= =\n")
    monkeypatch.setattr(runtime, "_api_key", "")
    runtime._write_bootstrap_ini("/download")
    seeded = configobj.ConfigObj(str(ini), encoding="utf-8")
    assert seeded.sections == ["misc"] and seeded["misc"]["api_key"] == runtime.internal_api_key()

    ini.write_bytes(b"[misc]\napi_key = \xff\xfe\n")
    monkeypatch.setattr(runtime, "_api_key", "")
    with pytest.raises(UnicodeDecodeError):
        runtime._write_bootstrap_ini("/download")


def test_a_misc_value_that_is_not_a_section_is_reseeded_as_before(tmp_path, monkeypatch):
    import configobj
    from executors.sabnzbd import runtime
    monkeypatch.setattr(runtime, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "_api_key", "")
    ini = tmp_path / "sabnzbd.ini"
    ini.write_text("misc = 5\n[logging]\nx = 1\n", encoding="utf-8")
    runtime._write_bootstrap_ini("/download")
    seeded = configobj.ConfigObj(str(ini), encoding="utf-8")
    assert seeded.sections == ["misc"] and seeded["misc"]["api_key"] == runtime.internal_api_key()


def test_folder_paths_reach_the_service_exactly_whatever_they_contain(tmp_path, monkeypatch):
    """A Download Folder whose path holds ',' or '#' is written quoted, so the
    service reads back the one path, never a list or a cut-off comment."""
    import configobj
    from executors.sabnzbd import runtime, topology
    monkeypatch.setattr(runtime, "CONFIG_ROOT", tmp_path / "usenet")
    monkeypatch.setattr(runtime, "_api_key", "")
    root = str(tmp_path / "a, b #c")
    runtime._write_bootstrap_ini(root)
    misc = configobj.ConfigObj(str(tmp_path / "usenet" / "sabnzbd.ini"), default_encoding="utf-8", encoding="utf-8")["misc"]
    assert misc["download_dir"] == topology.incomplete_root(root)
    assert misc["complete_dir"] == topology.complete_root(root)
