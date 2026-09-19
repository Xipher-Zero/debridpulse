import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_builtin_automatic_purge_preserves_bounded_result_state(monkeypatch):
    import executors.aria2.client as aria2_module

    service = aria2_module.Aria2Service("http://127.0.0.1:6800/jsonrpc", owns_daemon=True)
    service._call = AsyncMock()

    result = await service.purge_download_results()

    assert result == {
        "skipped": True,
        "reason": "bounded built-in aria2 result state is operator-visible",
    }
    service._call.assert_not_awaited()


@pytest.mark.asyncio
async def test_builtin_explicit_force_purge_remains_available(monkeypatch):
    import executors.aria2.client as aria2_module

    service = aria2_module.Aria2Service("http://127.0.0.1:6800/jsonrpc", owns_daemon=True)
    service._call = AsyncMock(return_value="ok")

    result = await service.purge_download_results(force=True)

    assert result == "ok"
    service._call.assert_awaited_once_with("aria2.purgeDownloadResult")


@pytest.mark.asyncio
async def test_external_force_purge_remains_blocked(monkeypatch):
    import executors.aria2.client as aria2_module

    service = aria2_module.Aria2Service("http://external.example/jsonrpc", owns_daemon=False)
    service._call = AsyncMock()

    result = await service.purge_download_results(force=True)

    assert result == {
        "skipped": True,
        "reason": "external aria2 result history is daemon-owned",
    }
    service._call.assert_not_awaited()






