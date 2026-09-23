"""1.0.13 Gate-9 remediation, finding 1: canonical configuration actually applies.

Saving `integrations.usenet` must drive the real native configuration
transaction (working-path topology and the NNTP server set). A save whose
native application failed must say so, and readiness must not claim healthy
while the required native configuration is unusable or detectably drifted.

The seam is generic: composition names no integration, and there is no second
configuration store and no continuous DP<->service synchronization.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from executors.sabnzbd.admin import SabnzbdAdministration
from integrations.definition import ConfigurableIntegration, ConfigurationApplication
from integrations.usenet.definition import UsenetOptions, UsenetServer

from sab_fakes import FakeSab


def options(**kw):
    base = dict(service_url="http://sab:8080", api_key="k",
                servers=[UsenetServer(host="news.a.net", username="u", password="p")])
    base.update(kw)
    return UsenetOptions(**base)


def admin_for(sab, *, root="/download", opts=None):
    return SabnzbdAdministration(sab, opts or options(), root)


# --- the generic seam -----------------------------------------------------

def test_the_administration_surface_is_a_configurable_integration():
    admin = admin_for(FakeSab())
    assert isinstance(admin, ConfigurableIntegration)
    # It declares WHICH canonical namespace it applies, so the seam needs no
    # hard-coded integration name anywhere.
    assert admin.configuration_namespace == "usenet"


def test_composition_discovers_appliers_without_naming_any_integration():
    import inspect
    from application import composition
    source = inspect.getsource(composition.integration_surfaces)
    assert "ConfigurableIntegration" in source
    for name in ("usenet", "sabnzbd", "aria2", "alldebrid"):
        assert name not in source.lower()


# --- application actually happens ----------------------------------------

@pytest.mark.asyncio
async def test_applying_configuration_writes_topology_and_servers():
    sab = FakeSab()
    admin = admin_for(sab, root="/download")
    result = await admin.apply_configuration()
    assert isinstance(result, ConfigurationApplication) and result.ok
    assert sab.download_dir == "/download/.dpwork/incomplete"
    assert sab.complete_dir == "/download/.dpwork/complete"
    assert [entry["host"] for entry in sab.servers.values()] == ["news.a.net"]


@pytest.mark.asyncio
async def test_a_removed_server_is_removed_natively_too():
    sab = FakeSab()
    await admin_for(sab).apply_configuration()
    assert len(sab.servers) == 1
    await admin_for(sab, opts=options(servers=[])).apply_configuration()
    assert sab.servers == {}


@pytest.mark.asyncio
async def test_failed_native_application_is_reported_truthfully_not_swallowed():
    sab = FakeSab(reachable=False)
    result = await admin_for(sab).apply_configuration()
    assert result.ok is False
    assert result.detail


@pytest.mark.asyncio
async def test_a_rejected_topology_change_is_reported_as_failure():
    sab = FakeSab(download_dir="Downloads/incomplete", complete_dir="Downloads/complete")
    sab.refuse_path_changes = True          # e.g. the native queue is not empty
    result = await admin_for(sab).apply_configuration()
    assert result.ok is False
    assert "topology" in result.detail.lower() or result.failures


# --- readiness must not lie ----------------------------------------------

@pytest.mark.asyncio
async def test_readiness_is_healthy_only_after_a_successful_application():
    sab = FakeSab(download_dir="Downloads/incomplete", complete_dir="Downloads/complete")
    admin = admin_for(sab)
    # Before anything is applied the native paths are still the defaults.
    assert (await admin.status())["state"] != "healthy"
    await admin.apply_configuration()
    assert (await admin.status())["state"] == "healthy"


@pytest.mark.asyncio
async def test_readiness_is_not_healthy_when_the_topology_has_drifted():
    sab = FakeSab()
    admin = admin_for(sab)
    await admin.apply_configuration()
    sab.download_dir = "/somewhere/else"     # changed out of band
    status = await admin.status()
    assert status["state"] != "healthy"
    assert status.get("drifted") is True


@pytest.mark.asyncio
async def test_readiness_is_not_healthy_when_a_server_has_drifted():
    sab = FakeSab()
    admin = admin_for(sab)
    await admin.apply_configuration()
    for entry in sab.servers.values():
        entry["enable"] = 0                  # disabled out of band
    assert (await admin.status())["state"] != "healthy"


@pytest.mark.asyncio
async def test_readiness_is_unconfigured_without_a_usable_server():
    sab = FakeSab()
    admin = admin_for(sab, opts=options(servers=[]))
    await admin.apply_configuration()
    assert (await admin.status())["state"] == "unconfigured"


@pytest.mark.asyncio
async def test_drift_detection_still_performs_no_write():
    sab = FakeSab()
    admin = admin_for(sab)
    await admin.apply_configuration()
    before = (dict(sab.servers), sab.download_dir, sab.complete_dir)
    report = await admin.drift()
    assert report.public()["synchronization"] == "none"
    assert (dict(sab.servers), sab.download_dir, sab.complete_dir) == before
