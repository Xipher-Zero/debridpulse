import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.routes import submit_transfer_input


class StubApplication:
    def __init__(self):
        self.received = None

    async def submit_input(self, transfer_id, *, challenge_id, method, values):
        self.received = (transfer_id, challenge_id, method, dict(values))
        return {"ok": True, "accepted": True, "id": transfer_id, "challenge_id": challenge_id}


def request_with_json(payload):
    raw = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/torrents/7/input", "headers": []}, receive)


@pytest.mark.asyncio
async def test_input_api_accepts_declared_fields_without_echoing_secret_values():
    app = StubApplication()
    payload = {
        "challenge_id": "challenge-public-id",
        "method": "username_private_key",
        "username": "api-user-sentinel",
        "private_key": "api-private-key-sentinel",
        "passphrase": "api-passphrase-sentinel",
    }
    result = await submit_transfer_input(7, request_with_json(payload), app)
    assert app.received == (7, "challenge-public-id", "username_private_key", {
        "username": "api-user-sentinel", "private_key": "api-private-key-sentinel", "passphrase": "api-passphrase-sentinel"})
    encoded = json.dumps(result, sort_keys=True)
    assert "api-user-sentinel" not in encoded
    assert "api-private-key-sentinel" not in encoded
    assert "api-passphrase-sentinel" not in encoded


@pytest.mark.asyncio
async def test_input_api_rejects_unknown_top_level_fields_without_forwarding_them():
    app = StubApplication()
    payload = {
        "challenge_id": "challenge-public-id",
        "method": "username_password",
        "username": "api-user-sentinel",
        "password": "api-password-sentinel",
        "otp": "api-unused-secret-sentinel",
    }
    with pytest.raises(HTTPException) as caught:
        await submit_transfer_input(7, request_with_json(payload), app)
    assert caught.value.status_code == 400
    assert app.received is None
    assert "sentinel" not in str(caught.value.detail)
