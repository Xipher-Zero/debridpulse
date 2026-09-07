from types import SimpleNamespace

import pytest

import api.routes as routes


class _FakeAria2Admin:
    async def get_global_options(self):
        return {
            "max-overall-download-limit": "0",
            "max-overall-upload-limit": "0",
            "max-concurrent-downloads": "99",
        }


class _FakeApplication:
    def __init__(self, max_active_executions: int):
        self.engine = SimpleNamespace(
            policy=SimpleNamespace(max_active_executions=max_active_executions)
        )
        self._admin = _FakeAria2Admin()

    def integration_admin(self, integration_id: str):
        assert integration_id == "aria2"
        return self._admin


@pytest.mark.parametrize("aria2_mode", ["builtin", "external"])
@pytest.mark.asyncio
async def test_topbar_concurrency_projection_uses_universal_scheduler_limit(monkeypatch, aria2_mode):
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(aria2_mode=aria2_mode, max_concurrent_downloads=3),
    )
    application = _FakeApplication(max_active_executions=7)

    result = await routes.aria2_get_global_options(application=application)

    assert result["max_concurrent_downloads"] == 7
    assert result["raw"]["max-concurrent-downloads"] == "99"
