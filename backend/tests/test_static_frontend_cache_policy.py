import pytest
from fastapi import Request, Response

from main import request_id_middleware


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 80),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/app.js", "/style-v11.css", "/oidc-verify-complete.html"])
async def test_frontend_executable_and_presentation_assets_require_revalidation(path: str):
    async def call_next(_request):
        return Response(content="asset")

    response = await request_id_middleware(_request(path), call_next)
    assert response.headers["Cache-Control"] == "no-cache, must-revalidate"


@pytest.mark.asyncio
async def test_existing_no_store_policy_wins_over_frontend_revalidation():
    async def call_next(_request):
        return Response(content="login", headers={"Cache-Control": "no-store"})

    response = await request_id_middleware(_request("/"), call_next)
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_api_responses_are_not_assigned_static_frontend_cache_policy():
    async def call_next(_request):
        return Response(content="{}", media_type="application/json")

    response = await request_id_middleware(_request("/api/health"), call_next)
    assert "Cache-Control" not in response.headers
