import pytest
from fastapi import Request, Response

from account_support import account
from api import auth_routes
from auth.models import Principal


def _request():
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/auth/session",
            "raw_path": b"/api/auth/session",
            "query_string": b"",
            "headers": [(b"host", b"pulse.example")],
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 443),
        }
    )
    request.state.principal = Principal.password_session(account("operator"))
    return request


@pytest.mark.asyncio
async def test_session_status_response_is_never_cacheable():
    response = Response()
    data = await auth_routes.auth_session_status(_request(), response)
    assert data["authenticated"] is True
    assert response.headers["Cache-Control"] == "no-store"
