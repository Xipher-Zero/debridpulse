from __future__ import annotations

import io
import lzma
import tarfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.config import AppSettings
from core.config_validator import validate_and_sanitise
from postprocessors.archive.secure import _extract_secure_sync


def _settings(*, count=3, delay=60):
    return SimpleNamespace(
        aria2_error_retry_count=count,
        aria2_error_retry_delay_seconds=delay,
    )


def _tar_payload() -> bytes:
    payload = b"release-safe composite extraction\n"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("nested/payload.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def test_tar_lzma_uses_exact_codec_then_validated_tar(tmp_path):
    source = tmp_path / "payload.tar.lzma"
    source.write_bytes(lzma.compress(_tar_payload(), format=lzma.FORMAT_ALONE))
    dest = tmp_path / "out"

    created = _extract_secure_sync(source, dest)

    extracted = dest / "nested" / "payload.txt"
    assert extracted.read_text() == "release-safe composite extraction\n"
    assert extracted in created
    assert not (dest / ".debridpulse-composite.tar").exists()


def test_retry_delay_zero_is_a_valid_immediate_retry_configuration():
    from transfers.settings import TransferSettings

    cfg = validate_and_sanitise(AppSettings(transfer_policy=TransferSettings(execution_retry_delay_seconds=0)))
    assert cfg.transfer_policy.execution_retry_delay_seconds == 0


