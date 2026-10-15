"""1.0.13: invalid operator input can no longer disable the managed aria2.

Production: ``disk_cache = "18EGGS"`` was accepted, persisted and passed to
aria2 1.37.0, which exits 28 on any option it cannot parse; the start reported
only an exit code and an old log tail, and nothing ever retried it.

* ``Aria2Options`` is the one contract, bounded by what the packaged aria2
  accepts: size grammar (digits + optional K/M), ranges, choices, strict types.
* A persisted value it refuses is reset to its default at load, observably --
  aria2's opt-in alone; every other namespace keeps numeric clamping only.
* The scoped write refuses invalid input before persistence, and if aria2
  itself refuses a saved configuration the previous one is kept (409).
* A failed start reports what aria2 said, even when its reader runs late, and
  never hangs; maintenance recovers a daemon that should be running, with
  backoff, never one an operator stopped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

import core.config as config
import executors.aria2.runtime as runtime_module
from executors.aria2.admin import Aria2Administration
from executors.aria2.client import Aria2ConnectionError, Aria2ResponseError
from executors.aria2.definition import STARTUP_ONLY_OPTIONS, Aria2Options, aria2_size_bytes
from executors.aria2.runtime import Aria2Runtime, Aria2RuntimeConfiguration, build_aria2_global_options
from integrations.configuration import clamp_persisted_namespaces

# ── the contract ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["18EGGS", "1G", "1.5M", "-1", "18MB", "18 M", " 64M", "64M ", "", "M",
                                   "0x10", "1e6", "64MiB", "9223372036854775808", "9007199254740992K",
                                   "8796093022208M", "12345678901234567890"])
def test_disk_cache_refuses_the_whole_class_of_values_aria2_cannot_use(value):
    with pytest.raises(ValidationError) as caught:
        Aria2Options(disk_cache=value)
    assert caught.value.errors()[0]["loc"] == ("disk_cache",)
    assert "Disk cache must be a whole number with an optional K or M suffix" in caught.value.errors()[0]["msg"]


@pytest.mark.parametrize("value,size", [("0", 0), ("0M", 0), ("512k", 512 * 1024), ("64M", 64 << 20),
                                        ("1024M", 1024 << 20), ("1048576", 1 << 20), ("2048M", 2048 << 20),
                                        ("8796093022207M", 8796093022207 << 20),
                                        ("9007199254740991K", 9007199254740991 << 10),
                                        ("9223372036854775807", 2**63 - 1)])
def test_disk_cache_accepts_every_size_aria2_accepts(value, size):
    assert Aria2Options(disk_cache=value).disk_cache == value
    assert aria2_size_bytes(value) == size


@pytest.mark.parametrize("field,good,bad", [
    ("max_connection_per_server", [1, 16], [0, 17, 32, True, "16", 16.0]),
    ("split", [1, 64], [0, 65, "8", 1.5]),
    ("min_split_size", ["1M", "1024M", "1048576", "10m"], ["512K", "1048575", "1025M", "1G", "10MB", "0"]),
    ("lowest_speed_limit", ["0", "10K", "5M", "1024M", "1025M", "8796093022207M"],
     ["1.5K", "abc", "-1", "1G", "8796093022208M"]),
    ("file_allocation", ["none", "prealloc", "trunc", "falloc"], ["bogus", "FALLOC", ""]),
    ("continue_downloads", [True, False], ["yes", "true", 1]),
    ("keep_unfinished_download_result", [True, False], ["maybe", 0]),
    ("auto_start", [True, False], ["false"]),
    ("max_download_result", [10, 5000], [9, 5001]),
    ("restart_interval_hours", [0, 1.5], [-1, float("inf"), float("nan"), "2"]),
    ("log_file", ["/app/data/aria2/aria2.log"], ["aria2.log", "/app/../etc/x", "/app/data/\nx", ""]),
    ("session_file", ["/app/data/aria2/aria2.session"], ["relative", "/a//b"]),
])
def test_every_audited_field_holds_its_contract(field, good, bad):
    for value in good:
        Aria2Options(**{field: value})
    for value in bad:
        with pytest.raises(ValidationError):
            Aria2Options(**{field: value})


def test_the_live_rpc_update_never_carries_a_startup_only_option():
    options = Aria2Options()
    assert "disk-cache" in build_aria2_global_options(options, include_safety=True)
    live = build_aria2_global_options(options, include_safety=True, live=True)
    assert not STARTUP_ONLY_OPTIONS & set(live)
    assert set(build_aria2_global_options(options, include_safety=True)) - set(live) == STARTUP_ONLY_OPTIONS


# ── legacy recovery at load ─────────────────────────────────────────────────

def test_a_persisted_invalid_value_is_reset_at_load_observably_and_never_written_again(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"download_folder": str(tmp_path / "dl"), "integrations": {"aria2": {"options": {
        "max_connection_per_server": 15, "split": 5, "min_split_size": "10M", "disk_cache": "18EGGS",
        "file_allocation": "falloc", "lowest_speed_limit": "0"}}}}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    caplog.set_level(logging.WARNING)
    settings = config.load_settings()
    options = settings.integrations["aria2"].options
    # The production configuration, recovered: only the invalid value changes.
    assert options["disk_cache"] == "64M"
    assert (options["max_connection_per_server"], options["split"], options["min_split_size"],
            options["file_allocation"], options["lowest_speed_limit"]) == (15, 5, "10M", "falloc", "0")
    assert "Config [integrations.aria2.disk_cache]: stored value is not valid - reset to default" in caplog.text
    assert "18EGGS" not in caplog.text
    # Corrected on disk, so the warning is reported once.
    stored = json.loads(path.read_text())["integrations"]["aria2"]["options"]
    assert stored["disk_cache"] == "64M"
    caplog.clear()
    config.load_settings()
    assert "stored value is not valid" not in caplog.text


def test_a_persisted_connection_count_above_16_is_clamped_to_16(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"download_folder": str(tmp_path / "dl"), "integrations": {"aria2": {"options": {
        "max_connection_per_server": 32, "file_allocation": "bogus", "continue_downloads": "yes"}}}}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    caplog.set_level(logging.WARNING)
    options = config.load_settings().integrations["aria2"].options
    assert options["max_connection_per_server"] == 16
    assert options["file_allocation"] == "falloc" and options["continue_downloads"] is True
    assert "integrations.aria2.max_connection_per_server]: value out of range - clamped" in caplog.text
    assert "integrations.aria2.file_allocation]: stored value is not valid - reset to default" in caplog.text


def test_resetting_an_invalid_stored_value_is_aria2s_alone(tmp_path, monkeypatch, caplog):
    # Another integration keeps its prior load behavior: an out-of-range
    # number is clamped, and a value of the wrong kind still fails the load
    # rather than being silently replaced.
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    caplog.set_level(logging.WARNING)
    path.write_text(json.dumps({"download_folder": str(tmp_path / "dl"), "integrations": {
        "realdebrid": {"options": {"rate_limit_per_minute": 99999}}}}))
    assert config.load_settings().integrations["realdebrid"].options["rate_limit_per_minute"] == 250
    assert "integrations.realdebrid.rate_limit_per_minute]: value out of range - clamped" in caplog.text
    for document in ({"integrations": {"realdebrid": {"options": {"rate_limit_per_minute": "many"}}}},
                     {"integrations": {"aria2": {"options": {"disk_cache": "18EGGS"}},
                                       "realdebrid": {"options": {"rate_limit_per_minute": "many"}}}}):
        path.write_text(json.dumps({"download_folder": str(tmp_path / "dl"), **document}))
        caplog.clear()
        with pytest.raises(RuntimeError, match="could not be read safely"):
            config.load_settings()
        assert "integrations.realdebrid.rate_limit_per_minute]: stored value is not valid - reset" not in caplog.text
        assert json.loads(path.read_text())["integrations"]["realdebrid"]["options"]["rate_limit_per_minute"] == "many"


def test_only_a_declaring_integration_has_invalid_values_reset():
    from integrations.catalog import definitions as catalog
    assert [d.id for d in catalog if d.reset_invalid_options] == ["aria2"]
    raw = {"integrations": {"aria2": {"options": {"disk_cache": "18EGGS"}},
                            "realdebrid": {"options": {"rate_limit_per_minute": "many"}}},
           "transfer_policy": {"max_concurrent_executions": "many"}}
    corrected = clamp_persisted_namespaces(raw, catalog)
    assert corrected == [("integrations.aria2.disk_cache", "reset to default")]
    assert raw["integrations"]["realdebrid"]["options"]["rate_limit_per_minute"] == "many"
    assert raw["transfer_policy"]["max_concurrent_executions"] == "many"


def test_a_backup_document_holding_an_invalid_value_is_refused_not_rewritten():
    with pytest.raises(ValueError):
        config.validate_settings_document({"integrations": {"aria2": {"options": {"disk_cache": "18EGGS"}}}})


# ── the scoped write ────────────────────────────────────────────────────────

def _route_world(current, runtime):
    from test_settings_namespace_mutation import _application

    saved = []
    application = _application(current)
    patches = (patch("api.routes.get_settings", return_value=current),
               # Like the settings owner, a load builds a fresh document.
               patch("api.routes.load_settings", side_effect=lambda: current.model_copy(deep=True)),
               patch("api.routes.save_settings", side_effect=saved.append),
               patch("api.routes.apply_settings"),
               patch("api.routes.aria2_runtime", runtime))
    return application, saved, patches


async def _patch(application, patches, options):
    from fastapi import HTTPException

    import api.routes as routes

    for item in patches:
        item.start()
    try:
        return await routes.patch_integration_configuration(
            "aria2", routes.IntegrationConfigurationUpdate(options=options), application=application)
    except HTTPException as exc:
        return exc
    finally:
        for item in reversed(patches):
            item.stop()


def _current():
    from test_settings_namespace_mutation import _settings_with_full_integrations
    return _settings_with_full_integrations()


def _fake_runtime(**status):
    return SimpleNamespace(ensure_started=AsyncMock(return_value={"process_running": True, **status}),
                           last_exit_code=None, pending_restart=lambda: [])


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"disk_cache": "18EGGS"}, {"split": 8, "disk_cache": "18EGGS"},
                                     {"max_connection_per_server": 32}, {"min_split_size": "512K"},
                                     {"split": 4, "file_allocation": "bogus"}, {"continue_downloads": "yes"}])
async def test_an_invalid_submission_is_refused_whole_and_nothing_is_saved_or_applied(options):
    runtime = _fake_runtime()
    application, saved, patches = _route_world(_current(), runtime)
    refused = await _patch(application, patches, options)
    assert refused.status_code == 400
    assert saved == []
    runtime.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_valid_live_update_is_saved_and_reports_what_took_effect():
    runtime = _fake_runtime()
    runtime.pending_restart = lambda: ["disk-cache"]
    application, saved, patches = _route_world(_current(), runtime)
    tuning = AsyncMock()
    application.integration_admin = lambda _id: SimpleNamespace(apply_memory_tuning=tuning)
    result = await _patch(application, patches, {"split": 8, "disk_cache": "32M"})
    assert result["ok"] is True and saved[-1].integrations["aria2"].options["disk_cache"] == "32M"
    assert result["runtime"] == {"running": True, "applied": True, "pending_restart": ["disk-cache"]}
    tuning.assert_awaited_once()


@pytest.mark.asyncio
async def test_aria2_refusing_a_saved_configuration_keeps_the_previous_one():
    current = _current()
    before = dict(current.integrations["aria2"].options)
    runtime = _fake_runtime()
    application, saved, patches = _route_world(current, runtime)
    calls = []

    async def refuse_new():
        calls.append(len(saved))
        if len(calls) == 1:
            raise Aria2ResponseError("We encountered a problem while processing the option '--split'.")

    application.integration_admin = lambda _id: SimpleNamespace(apply_memory_tuning=refuse_new)
    refused = await _patch(application, patches, {"split": 9})
    assert refused.status_code == 409 and "previous configuration was kept" in refused.detail
    # Saved, refused by aria2, and the previous configuration saved and applied again.
    assert saved[0].integrations["aria2"].options["split"] == 9
    assert saved[-1].integrations["aria2"].options == before
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_start_aria2_refuses_with_exit_28_keeps_the_previous_configuration():
    current = _current()
    before = dict(current.integrations["aria2"].options)
    runtime = SimpleNamespace(ensure_started=AsyncMock(return_value={"process_running": False,
                                                                     "last_error": "exit code 28"}),
                              last_exit_code=28, pending_restart=lambda: [])
    application, saved, patches = _route_world(current, runtime)
    refused = await _patch(application, patches, {"split": 9})
    assert refused.status_code == 409
    assert saved[-1].integrations["aria2"].options == before


@pytest.mark.asyncio
async def test_no_daemon_by_choice_or_a_lost_connection_is_reported_never_refused():
    for runtime, failure in ((_fake_runtime(process_running=False), None),
                             (_fake_runtime(), Aria2ConnectionError("Connection to aria2 lost"))):
        application, saved, patches = _route_world(_current(), runtime)
        application.integration_admin = lambda _id, failure=failure: SimpleNamespace(
            apply_memory_tuning=AsyncMock(side_effect=failure))
        result = await _patch(application, patches, {"split": 7})
        assert result["ok"] is True and result["runtime"]["applied"] is False
        assert saved[-1].integrations["aria2"].options["split"] == 7


# ── startup diagnostics ─────────────────────────────────────────────────────

ARIA2_REFUSAL = ("echo \"Exception: [AbstractOptionHandler.cc:69] errorCode=28 We encountered a problem while "
                 "processing the option '--disk-cache'.\" >&2\n"
                 "echo '  -> [OptionHandlerImpl.cc:184] errorCode=1 Bad or negative value detected: 18EGGS' >&2\n")


def _fake_aria2c(tmp_path, monkeypatch, body: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "aria2c"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _runtime(tmp_path, **options) -> Aria2Runtime:
    runtime = Aria2Runtime()
    runtime.configure(Aria2RuntimeConfiguration(options=Aria2Options(
        log_file=str(tmp_path / "aria2" / "aria2.log"), session_file=str(tmp_path / "aria2" / "aria2.session"),
        **options), download_root=str(tmp_path / "download")))
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_delay", [0, 0.3])
async def test_a_refused_start_reports_what_aria2_said_even_when_its_reader_runs_late(
        tmp_path, monkeypatch, reader_delay):
    _fake_aria2c(tmp_path, monkeypatch, ARIA2_REFUSAL + "exit 28\n")
    (tmp_path / "aria2").mkdir()
    (tmp_path / "aria2" / "aria2.log").write_text("2026-10-13 notice an earlier run\n")
    if reader_delay:
        original = Aria2Runtime._drain_stream

        async def late(self, stream, name):
            await asyncio.sleep(reader_delay)
            await original(self, stream, name)

        monkeypatch.setattr(Aria2Runtime, "_drain_stream", late)
    runtime = _runtime(tmp_path)
    status = await runtime.start()
    assert status["process_running"] is False and status["last_exit_code"] == 28
    error = status["last_error"]
    assert "exit code 28" in error and "Bad or negative value detected: 18EGGS" in error
    assert "earlier aria2 log (this start wrote none)" in error
    assert runtime_module.RPC_SECRET not in error


@pytest.mark.asyncio
async def test_a_start_whose_output_never_closes_is_still_bounded(tmp_path, monkeypatch):
    # The daemon exits 28 but a child it left holds its output open.
    _fake_aria2c(tmp_path, monkeypatch, ARIA2_REFUSAL + "(sleep 30 &)\nexit 28\n")
    runtime = _runtime(tmp_path)
    started = time.monotonic()
    status = await runtime.start()
    assert time.monotonic() - started < 8
    assert status["last_exit_code"] == 28


# ── managed recovery ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovery_retries_with_backoff_and_never_restarts_a_stopped_daemon(tmp_path, monkeypatch):
    _fake_aria2c(tmp_path, monkeypatch, ARIA2_REFUSAL + "exit 28\n")
    runtime = _runtime(tmp_path)
    starts = []
    original = Aria2Runtime.start

    async def counted(self):
        starts.append(1)
        return await original(self)

    monkeypatch.setattr(Aria2Runtime, "start", counted)
    backoff = lambda: runtime._next_recovery_at - time.monotonic()   # noqa: E731
    await runtime.ensure_started()                          # the failed application startup
    assert 55 < backoff() <= 60
    assert await runtime.recover() is None                  # within its backoff: nothing attempted
    runtime._next_recovery_at = 0.0                         # the backoff elapses
    assert (await runtime.recover())["process_running"] is False   # retried once, failed again
    assert 115 < backoff() <= 120                           # doubled
    assert await runtime.recover() is None
    for _ in range(6):
        runtime._next_recovery_at = 0.0
        await runtime.recover()
    assert 895 < backoff() <= 900                           # never beyond 15 minutes
    assert len(starts) == 8
    # A changed configuration clears the backoff: it may be the fix.
    runtime.configure(Aria2RuntimeConfiguration(options=Aria2Options(
        disk_cache="32M", log_file=str(tmp_path / "aria2" / "aria2.log"),
        session_file=str(tmp_path / "aria2" / "aria2.session")), download_root=str(tmp_path / "download")))
    await runtime.recover()
    assert len(starts) == 9
    # An operator's Stop is respected; so is auto_start off.
    await runtime.stop()
    runtime._next_recovery_at = 0.0
    assert await runtime.recover() is None and len(starts) == 9
    assert await _runtime(tmp_path, auto_start=False).recover() is None


@pytest.mark.asyncio
async def test_maintenance_recovers_a_dead_daemon_instead_of_failing_against_it():
    runtime = SimpleNamespace(options=Aria2Options(), process_running=False, recover=AsyncMock(return_value=None),
                              ensure_log_rotation=AsyncMock(), _started_at=0)
    client = SimpleNamespace(get_global_stat=AsyncMock(), get_all=AsyncMock(), change_global_options=AsyncMock())
    admin = Aria2Administration(SimpleNamespace(client=client, configuration=None), None, None, runtime)
    await admin.maintain()                                  # no exception, no RPC against a dead daemon
    runtime.recover.assert_awaited_once()
    client.change_global_options.assert_not_awaited()
    runtime.ensure_log_rotation.assert_not_awaited()


# ── the real aria2 executable ───────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.real_runtime
@pytest.mark.asyncio
async def test_the_real_daemon_starts_on_a_recovered_configuration_and_reports_a_refused_one(tmp_path, monkeypatch):
    if not __import__("shutil").which("aria2c"):
        pytest.fail("real_runtime requires aria2c")
    monkeypatch.setattr(runtime_module, "RPC_PORT", _free_port())
    # The production configuration, as loaded after recovery.
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"download_folder": str(tmp_path / "dl"), "integrations": {"aria2": {"options": {
        "max_connection_per_server": 15, "split": 5, "disk_cache": "18EGGS", "log_file": str(tmp_path / "a.log"),
        "session_file": str(tmp_path / "a.session")}}}}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    options = Aria2Options(**config.load_settings().integrations["aria2"].options)
    runtime = Aria2Runtime()
    runtime.configure(Aria2RuntimeConfiguration(options=options, download_root=str(tmp_path / "dl")))
    try:
        status = await runtime.start()
        assert status["process_running"] and status["rpc_ok"], status["last_error"]
        effective = await runtime._service().get_global_options()
        assert effective["disk-cache"] == str(64 << 20) and effective["max-connection-per-server"] == "15"
        # A live update carries only what aria2 can change live.
        assert (await runtime.apply_options())["options"].get("disk-cache") is None
    finally:
        await runtime.stop()
    # What the old schema let through, straight to the real daemon: refused,
    # reported in aria2's own words.
    bypass = Aria2Options.model_construct(**{**options.model_dump(), "max_connection_per_server": 32})
    runtime.configure(Aria2RuntimeConfiguration(options=bypass, download_root=str(tmp_path / "dl")))
    status = await runtime.start()
    assert status["process_running"] is False and status["last_exit_code"] == 28
    assert "max-connection-per-server must be between 1 and 16" in status["last_error"]
