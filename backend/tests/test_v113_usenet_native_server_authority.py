"""1.0.13 Gate-9 rev-4, item 4: DP is the COMPLETE native server authority.

The bundled acquisition service is internal and private: there is no operator
web UI, no published port and no operator service credential. Nothing but
DebridPulse can legitimately configure a news server inside it, so the
canonical ``integrations.usenet`` server list is not merely the source of the
servers DP added -- it is the source of EVERY server the service may hold.

A server DebridPulse does not declare therefore has no legitimate origin. It
would still take part in acquisition, using credentials DP never stored and an
operator cannot see, so it must be removed rather than tolerated, and while it
is present the configuration must never read as synchronized.
"""
from __future__ import annotations

import pytest

from executors.sabnzbd.admin import MANAGED_PREFIX, SabnzbdAdministration, server_keyword
from integrations.usenet.definition import UsenetOptions, UsenetServer

from sab_fakes import FakeSab


def options(servers=None):
    return UsenetOptions(
        service_url="http://sab:8080", api_key="k",
        servers=servers if servers is not None
        else [UsenetServer(host="news.a.net", username="u", password="p")])


def admin_for(sab, opts=None, root="/download"):
    return SabnzbdAdministration(sab, opts or options(), root)


def foreign(sab, name="operator-added", **kw):
    """A native server that DebridPulse never declared."""
    entry = dict(name=name, host="foreign.example", port=563, ssl=1,
                 username="who", password="secret", connections=8,
                 priority=0, enable=1, displayname=name)
    entry.update(kw)
    sab.servers[name] = entry
    return name


@pytest.mark.asyncio
async def test_a_foreign_native_server_is_removed_not_tolerated():
    """The decisive property: DP owns the whole native server set."""
    sab = FakeSab()
    name = foreign(sab)
    opts = options()
    await admin_for(sab, opts).apply_servers()
    assert name not in sab.servers, "a server DP never declared survived application"
    # ...and the canonical ones are present.
    assert set(sab.servers) == {server_keyword(s) for s in opts.servers}


@pytest.mark.asyncio
async def test_a_foreign_server_is_removed_even_when_disabled():
    """Disabled today is one API call away from enabled, and DP cannot see it."""
    sab = FakeSab()
    name = foreign(sab, "dormant", enable=0)
    await admin_for(sab).apply_servers()
    assert name not in sab.servers


@pytest.mark.asyncio
async def test_removal_is_not_limited_to_the_managed_prefix():
    """Both an orphaned managed server and a foreign one go."""
    sab = FakeSab()
    orphan = MANAGED_PREFIX + "gone"
    sab.servers[orphan] = dict(name=orphan, host="old.example", port=563, ssl=1,
                               username="u", password="p", connections=1,
                               priority=0, enable=1)
    alien = foreign(sab)
    await admin_for(sab).apply_servers()
    assert orphan not in sab.servers and alien not in sab.servers


@pytest.mark.asyncio
async def test_drift_reports_any_unexpected_server_whatever_its_name():
    sab = FakeSab()
    admin = admin_for(sab)
    await admin.apply_servers()
    name = foreign(sab)
    report = await admin.drift()
    assert report.reachable and report.drifted
    assert f"servers.{name}.unexpected" in report.differences


@pytest.mark.asyncio
async def test_a_reinjected_foreign_server_cannot_survive_convergence():
    """Injected out of band, it is gone again the next time DP applies."""
    sab = FakeSab()
    admin = admin_for(sab)
    assert (await admin.apply_configuration()).ok is True
    name = foreign(sab, "reinjected")
    assert (await admin.drift()).drifted is True
    result = await admin.apply_configuration()
    assert result.ok is True
    assert name not in sab.servers
    assert (await admin.drift()).drifted is False


@pytest.mark.asyncio
async def test_an_empty_canonical_list_empties_the_native_set():
    """No declared servers means no servers at all -- not 'keep what is there'."""
    sab = FakeSab()
    foreign(sab)
    await admin_for(sab, options(servers=[])).apply_servers()
    assert sab.servers == {}


@pytest.mark.asyncio
async def test_status_does_not_read_ready_while_a_foreign_server_is_present():
    sab = FakeSab()
    admin = admin_for(sab)
    await admin.apply_configuration()
    foreign(sab)
    status = await admin.status()
    assert status.get("drifted") is True
