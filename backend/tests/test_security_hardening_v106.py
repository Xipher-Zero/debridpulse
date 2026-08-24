import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.alldebrid import (
    AllDebridService,
    flatten_files,
    validate_provider_download_url,
)
from services.extractor import (
    _extract_7z_to,
    _extract_rar_to,
    _preflight_7z,
    _seven_zip_type_switches,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/download/file.bin?token=opaque",
        "http://downloads.example.net:8080/file.bin",
        "https://8.8.8.8/file.bin",
    ],
)
def test_provider_download_url_accepts_public_http_targets(url):
    assert validate_provider_download_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file.bin",
        "http://localhost/file.bin",
        "http://service.local/file.bin",
        "http://127.0.0.1/file.bin",
        "http://10.0.0.1/file.bin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/file.bin",
        "http://2130706433/file.bin",
        "https://user:pass@example.com/file.bin",
    ],
)
def test_provider_download_url_rejects_local_or_unsafe_targets(url):
    with pytest.raises(Exception):
        validate_provider_download_url(url)


@pytest.mark.asyncio
async def test_unlock_link_validates_immediate_provider_target():
    service = AllDebridService("test-key")
    service._post = AsyncMock(return_value={"link": "http://127.0.0.1/private"})

    with pytest.raises(Exception, match="non-public"):
        await service.unlock_link("https://host.example/source")


@pytest.mark.asyncio
async def test_unlock_link_validates_delayed_provider_target():
    service = AllDebridService("test-key")
    service._post = AsyncMock(
        side_effect=[
            {"delayed": "job-42", "filename": "archive.zip"},
            {"status": 2, "link": "http://169.254.169.254/metadata"},
        ]
    )

    with patch("services.alldebrid.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(Exception, match="non-public"):
            await service.unlock_link("https://host.example/source")


def test_magnet_file_capability_is_validated_before_materialization():
    with pytest.raises(Exception, match="non-public"):
        flatten_files(
            [{"n": "payload.bin", "s": 1, "l": "http://127.0.0.1/payload"}]
        )


def test_7zip_type_switches_pin_plain_7z_and_magic_validate_rar(tmp_path):
    assert _seven_zip_type_switches(Path("payload.7z")) == ["-t7z"]

    rar5 = tmp_path / "payload.rar"
    rar5.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"payload")
    assert _seven_zip_type_switches(rar5) == []

    rar4 = tmp_path / "payload.r00"
    rar4.write_bytes(b"Rar!\x1a\x07\x00" + b"payload")
    assert _seven_zip_type_switches(rar4) == []


def test_7zip_composite_formats_keep_nested_mode_but_exclude_xz():
    for name in ("payload.tar.zst", "payload.tzst", "payload.tar.lzma"):
        switches = _seven_zip_type_switches(Path(name))
        assert "-t*:r" in switches
        assert "-stxxz" in switches


def test_7zip_preflight_uses_same_strict_parser_policy():
    with patch("services.extractor._run_tool", return_value=(0, "listing")) as run_tool, patch(
        "services.extractor.validate_7z_listing"
    ):
        _preflight_7z(Path("payload.7z"), "7z", [""])

    command = run_tool.call_args.args[0]
    assert "-t7z" in command
    assert "-t*:r" not in command


def test_7zip_extract_uses_strict_7z_parser():
    with patch("services.extractor._tool_available", return_value=True), patch(
        "services.extractor._preflight_7z"
    ), patch("services.extractor._run_tool", return_value=(0, "")) as run_tool:
        _extract_7z_to(Path("payload.7z"), Path("/tmp/dp-security-test"))

    command = run_tool.call_args.args[0]
    assert "-t7z" in command


def test_rar_extract_uses_magic_validated_7zip_autodetect(tmp_path):
    archive = tmp_path / "payload.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"payload")

    with patch("services.extractor._tool_available", return_value=True), patch(
        "services.extractor._preflight_7z"
    ), patch("services.extractor._run_tool", return_value=(0, "")) as run_tool:
        _extract_rar_to(archive, Path("/tmp/dp-security-test"))

    command = run_tool.call_args.args[0]
    assert "-trar" not in command
    assert str(archive) in command
