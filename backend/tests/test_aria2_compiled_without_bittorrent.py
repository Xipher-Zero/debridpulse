"""DebridPulse's aria2 is compiled without BitTorrent, and DebridPulse runs it.

The package boundary (docs/SUPPLY_CHAIN_POLICY.md section 4a) removes the
BitTorrent implementation and with it every BitTorrent option: naming one on
the command line makes aria2 refuse to start (characterized 1.37.0+debian-3+dp2:
exit 28, "unrecognized option"). So the proof here is twofold -- the exact
daemon options DebridPulse passes start the binary it ships, and the binary
refuses every BitTorrent input (magnet, .torrent, addTorrent). Magnets and
.torrent files are provider-side in DebridPulse; aria2 is never a BitTorrent
client.
"""
from __future__ import annotations

import asyncio

import pytest

from executors.aria2.definition import Aria2Options
from executors.aria2.runtime import build_aria2_global_options
from test_v1111_aria2_security_boundary import _start_aria2, _stop_aria2

pytestmark = [pytest.mark.real_runtime, pytest.mark.asyncio]

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
# A minimal well-formed single-file metainfo dictionary.
TORRENT = b"d4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"


def _daemon_options() -> tuple[str, ...]:
    """The global options DebridPulse's runtime starts aria2 with."""
    options = build_aria2_global_options(Aria2Options(), include_safety=True)
    options["max-overall-download-limit"] = "0"
    return tuple(f"--{key}={value}" for key, value in options.items())


async def test_the_daemon_options_debridpulse_passes_start_the_binary_it_ships(tmp_path) -> None:
    proc, service = await _start_aria2(tmp_path, extra_args=_daemon_options())
    try:
        version = await service._call("aria2.getVersion")
        features = set(version["enabledFeatures"])
        assert "BitTorrent" not in features
        assert {"HTTPS", "SFTP", "Metalink"} <= features
    finally:
        await _stop_aria2(proc, service)


async def test_every_bittorrent_input_is_refused(tmp_path) -> None:
    proc, service = await _start_aria2(tmp_path)
    try:
        methods = await service._call("system.listMethods", inject_token=False)
        assert "aria2.addTorrent" not in methods
        with pytest.raises(Exception, match="No such method"):
            await service._call("aria2.addTorrent", ["ZDQ6aW5mb2Vl"])
        with pytest.raises(Exception, match="No URI to download"):
            await service._call("aria2.addUri", [[MAGNET]])
    finally:
        await _stop_aria2(proc, service)
    torrent = tmp_path / "input.torrent"
    torrent.write_bytes(TORRENT)
    for argument in (str(torrent), MAGNET):
        cli = await asyncio.create_subprocess_exec(
            "aria2c", "--dry-run", "--quiet", f"--dir={tmp_path / 'out'}", argument,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await cli.communicate()
        assert cli.returncode != 0, argument
    assert not (tmp_path / "out").exists()


async def test_a_bittorrent_option_is_unknown_to_the_binary(tmp_path) -> None:
    """Why DebridPulse never names one: the daemon would not start."""
    cli = await asyncio.create_subprocess_exec(
        "aria2c", "--dry-run", "--follow-torrent=false", "http://127.0.0.1:9/never",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _stdout, stderr = await cli.communicate()
    assert cli.returncode == 28
    assert b"unrecognized option" in stderr
