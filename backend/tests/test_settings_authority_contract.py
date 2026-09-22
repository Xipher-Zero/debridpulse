"""One write surface per canonical settings authority (DP 1.0.12 final audit).

``integrations.<id>`` (including AllDebrid credentials and every aria2 option),
``transfer_policy`` and ``execution_runtime_limits`` are written only through
their scoped surfaces. The broad ``PUT /settings`` document never writes them,
and the flat names a pre-canonical client may still send are ignored.
"""
import ast
import asyncio
import json
import shutil
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api import routes
from core.config import AppSettings
from executors.aria2.client import Aria2Service
from executors.aria2.definition import Aria2Options, definition as aria2_definition
from executors.aria2.runtime import RPC_SECRET, Aria2Runtime, Aria2RuntimeConfiguration, rpc_url
from integrations.catalog import definitions
from integrations.configuration import normalize_settings
from integrations.definition import IntegrationEnvironment, IntegrationSettings
from providers.alldebrid.definition import definition as alldebrid_definition
from transfers.settings import TransferSettings

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def _open():
    yield


def _application():
    admin = SimpleNamespace(apply_memory_tuning=AsyncMock())
    return SimpleNamespace(
        definitions=definitions,
        application_operation=lambda: _open(),
        configuration_admission=lambda: _open(),
        configure=MagicMock(),
        reconcile_executions=AsyncMock(),
        integration_admin=lambda _identity: admin,
        validate_configuration=AsyncMock(),
    )


def _current():
    return normalize_settings(AppSettings(
        integrations={
            "alldebrid": IntegrationSettings(options={"api_key": "stored-key", "rate_limit_per_minute": 60}),
            "aria2": IntegrationSettings(options={"split": 24}),
        },
        transfer_policy=TransferSettings(max_concurrent_executions=6, stalled_timeout_hours=9),
    ), definitions)


async def _put(body: dict):
    current = _current()
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"), \
         patch("api.routes.aria2_runtime.ensure_started", AsyncMock()), \
         patch("api.routes.aria2_runtime.restart", AsyncMock()), \
         patch("api.routes.aria2_runtime.stop", AsyncMock()):
        response = await routes.update_settings(routes.SettingsUpdate(**body), application=_application())
    return current, saved["cfg"], response


@pytest.mark.asyncio
async def test_put_settings_never_writes_a_canonical_namespace():
    stale = _current().model_dump()
    stale["integrations"]["aria2"]["options"]["split"] = 1                 # stale echo of a namespace
    stale["transfer_policy"]["max_concurrent_executions"] = 1
    stale["execution_runtime_limits"]["max_download_bytes_per_second"] = 77
    stale["full_sync_interval_minutes"] = 11                                # an ordinary broad field
    current, saved, _ = await _put(stale)

    assert saved.full_sync_interval_minutes == 11
    assert saved.integrations["aria2"].options["split"] == 24
    assert saved.integrations["alldebrid"].options["api_key"] == "stored-key"
    assert saved.transfer_policy.max_concurrent_executions == 6
    assert saved.execution_runtime_limits.max_download_bytes_per_second == 0


@pytest.mark.asyncio
async def test_put_settings_ignores_every_flat_compatibility_name():
    flat = {
        "aria2_split": 2,
        "max_concurrent_downloads": 1, "aria2_max_active_downloads": 1, "aria2_max_download_limit": 5,
        "alldebrid_api_key": "attacker-key", "poll_interval_seconds": 999, "upload_fail_retry_count": 0,
        "stuck_download_timeout_hours": 1,
    }
    _current_before, saved, response = await _put(flat)

    assert saved.integrations["aria2"].options["split"] == 24
    assert saved.integrations["alldebrid"].options["api_key"] == "stored-key"
    assert saved.transfer_policy.max_concurrent_executions == 6
    assert saved.transfer_policy.stalled_timeout_hours == 9
    assert not set(flat) & set(saved.model_dump())
    # ... while the response still shows the CURRENT canonical value under the
    # historical names, as read-only compatibility output.
    assert response["max_concurrent_downloads"] == 6
    assert response["aria2_split"] == 24
    # The document names its own compatibility output, so a client can drop it.
    assert {"max_concurrent_downloads", "aria2_split", "alldebrid_api_key_configured"} <= set(response["compatibility_fields"])
    assert "transfer_policy" not in response["compatibility_fields"]


@pytest.mark.asyncio
async def test_broad_secret_clear_no_longer_accepts_integration_owned_secrets():
    for secret in ("alldebrid_api_key",):
        with pytest.raises(Exception, match="Unsupported secret field"):
            await _put({"clear_secrets": [secret]})


@pytest.mark.asyncio
async def test_transfer_policy_patch_owns_every_operator_tunable_policy_field():
    current = _current()
    saved = {}
    body = routes.TransferPolicyUpdate(
        max_concurrent_executions=8, execution_retry_count=1, execution_retry_delay_seconds=30,
        resolution_retry_count=5, resolution_retry_delay_minutes=2, execution_poll_interval_seconds=4,
        provider_poll_interval_seconds=60, stalled_timeout_hours=24,
    )
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_transfer_policy(body, application=_application())
    policy = saved["cfg"].transfer_policy
    assert (policy.max_concurrent_executions, policy.execution_retry_count, policy.execution_retry_delay_seconds) == (8, 1, 30)
    assert (policy.resolution_retry_count, policy.resolution_retry_delay_minutes) == (5, 2)
    assert (policy.execution_poll_interval_seconds, policy.provider_poll_interval_seconds) == (4, 60)
    assert policy.stalled_timeout_hours == 24
    assert result["provider_poll_interval_seconds"] == 60 and result["stalled_timeout_hours"] == 24


@pytest.mark.asyncio
async def test_alldebrid_credentials_are_written_only_through_the_scoped_integration_surface():
    current = _current()
    saved = {}
    body = routes.IntegrationConfigurationUpdate(options={"api_key": "", "rate_limit_per_minute": 90}, enabled=False)
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current.model_copy(deep=True)), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration("alldebrid", body, application=_application())
    options = saved["cfg"].integrations["alldebrid"].options
    assert options["api_key"] == "stored-key"                      # blank means keep
    assert options["rate_limit_per_minute"] == 90
    assert saved["cfg"].integrations["alldebrid"].enabled is False
    assert result["options"]["api_key"] == "" and result["options"]["api_key_configured"] is True


# --------------------------------------------------------------------------- #
# The daemon owner constructs the one RPC client; nothing looks it up
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_client_may_change_global_options_and_purge_results():
    service = Aria2Service("http://127.0.0.1:6800/jsonrpc")
    service._call = AsyncMock(return_value="ok")
    assert await service.change_global_options({"max-overall-download-limit": "1"}) == "ok"
    assert await service.purge_download_results(force=True) == "ok"


def test_composition_and_runtime_construct_the_same_daemon_client():
    options = Aria2Options(operation_timeout_seconds=30)
    executor = aria2_definition.factory(options, IntegrationEnvironment(repository=MagicMock(), download_root="/download"))
    runtime = Aria2Runtime()
    runtime.configure(Aria2RuntimeConfiguration(options=options))
    for client in (executor.client, runtime._service()):
        assert (client.url, client.secret) == (rpc_url(), RPC_SECRET) == ("http://127.0.0.1:6800/jsonrpc", RPC_SECRET)
        assert client.timeout.total == 30


def test_the_client_module_has_no_global_settings_lookup():
    source = (ROOT / "executors/aria2/client.py").read_text()
    tree = ast.parse(source)
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not [n for n in imports if "core.config" in ast.dump(n)]
    assert "get_settings" not in source
    for path in (ROOT / "executors/aria2").glob("*.py"):
        if path.name != "migration.py":     # startup-only legacy handle decoder; reads the canonical namespace
            assert "get_settings" not in path.read_text(), path.name


def test_flat_aliases_are_not_read_by_production_runtime_code():
    forbidden = (
        'getattr(cfg, "aria2_split"', "cfg.aria2_split", ".aria2_split", "settings.aria2_",
        "cfg.max_concurrent_downloads", "cfg.alldebrid_api_key", "settings.alldebrid_api_key",
    )
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or path.name in {"legacy_settings_view.py"}:
            continue
        text = path.read_text()
        for needle in forbidden:
            assert needle not in text, f"{path.relative_to(ROOT)} reads flat alias {needle!r}"


# --------------------------------------------------------------------------- #
# Obsolete persisted values have zero authority over the one aria2 daemon
# --------------------------------------------------------------------------- #

class _TrapRpcServer:
    """Counts every connection made to an address no current code may dial."""

    def __init__(self):
        self.connections = 0

    async def __aenter__(self):
        async def accept(_reader, writer):
            self.connections += 1
            writer.close()
        self.server = await asyncio.start_server(accept, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/jsonrpc"
        return self

    async def __aexit__(self, *_exc):
        self.server.close()
        await self.server.wait_closed()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("aria2c") is None, reason="the packaged aria2c binary is required")
async def test_obsolete_persisted_values_never_redirect_the_dp_owned_daemon(tmp_path, monkeypatch):
    import core.config as config
    from executors.aria2 import runtime as runtime_module
    from test_executor_configuration_ownership import CURRENT_ARIA2_OPTIONS

    downloads = tmp_path / "downloads"
    async with _TrapRpcServer() as trap:
        obsolete = {"mode": "external", "url": trap.url, "secret": "trap-secret", "download_path": "/elsewhere"}
        path = tmp_path / "config.json"
        # Values an earlier release persisted, both as pre-canonical flat keys and
        # inside the canonical namespace, all naming the trap.
        path.write_text(json.dumps({
            **{f"aria2_{name}": value for name, value in obsolete.items()},
            "download_folder": str(downloads),
            "integrations": {"aria2": {"options": {**obsolete, "split": 4}}},
        }))
        monkeypatch.setattr(config, "CONFIG_PATH", path)
        monkeypatch.setattr(config, "_settings", config.AppSettings())
        port = _free_port()
        monkeypatch.setattr(runtime_module, "RPC_PORT", port, raising=False)

        settings = config.load_settings()
        options = settings.integrations["aria2"].options

        environment = IntegrationEnvironment(
            repository=SimpleNamespace(authorize_execution=AsyncMock(return_value=True)),
            download_root=settings.download_folder,
        )
        executor = aria2_definition.build(settings.integrations["aria2"], environment)
        assert executor.client.url == f"http://127.0.0.1:{port}/jsonrpc"
        assert executor.configuration.local_root == str(downloads)
        assert set(options) == CURRENT_ARIA2_OPTIONS and options["split"] == 4

        runtime = runtime_module.Aria2Runtime()
        runtime.configure(Aria2RuntimeConfiguration(
            options=Aria2Options(**options).model_copy(update={
                "log_file": str(tmp_path / "aria2" / "aria2.log"),
                "session_file": str(tmp_path / "aria2" / "aria2.session"),
            }),
            download_root=settings.download_folder,
        ))
        try:
            status = await runtime.start()
            assert status["running"] and status["rpc_ok"], status["last_error"]
            assert status["download_dir"] == str(downloads)
            assert (await executor.health()).reachable
        finally:
            await runtime.stop()

        public = routes._public_settings(settings, definitions)
        assert set(public["integrations"]["aria2"]["options"]) == CURRENT_ARIA2_OPTIONS
        assert not [name for name in public if "migrat" in name]
        config.save_settings(settings)
        rewritten = json.loads(path.read_text())
        assert set(rewritten["integrations"]["aria2"]["options"]) == CURRENT_ARIA2_OPTIONS
        assert not {f"aria2_{name}" for name in obsolete} & set(rewritten)
        assert trap.connections == 0
