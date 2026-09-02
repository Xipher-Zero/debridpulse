from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise RuntimeError(f"anchor not found in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "backend/transfers/engine.py",
    '''        submitted = await self.inputs.take(challenge)\n        if submitted is None:\n            return\n        try:\n            result = await provider.resolve_with_input(record.request, submitted)\n            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")\n            await self._apply_resolution(record, attempt, provider, result, challenge=challenge)\n        except Exception as exc:\n            secrets = submitted.secret_values()\n            error = exc.error if isinstance(exc, TransferError) else unknown_failure(\n                exc, integration_id=challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)\n            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")\n            await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))\n            await self.challenges.clear(challenge)\n            await self._request_failure(record, error, attempts=record.attempts + 1)\n        finally:\n            submitted.discard()\n''',
    '''        submitted = None\n        try:\n            # Waiting for input releases provider capacity. Reacquire the normal\n            # resolution slot before consuming the transient secret bundle so\n            # continuation cannot bypass provider concurrency or retain secrets\n            # while merely waiting for capacity.\n            async with self._resolution_slots:\n                if not await self._live(challenge.transfer_id, admission=True):\n                    return\n                submitted = await self.inputs.take(challenge)\n                if submitted is None:\n                    return\n                result = await provider.resolve_with_input(record.request, submitted)\n            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")\n            await self._apply_resolution(record, attempt, provider, result, challenge=challenge)\n        except Exception as exc:\n            secrets = submitted.secret_values() if submitted else ()\n            error = exc.error if isinstance(exc, TransferError) else unknown_failure(\n                exc, integration_id=challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)\n            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")\n            await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))\n            await self.challenges.clear(challenge)\n            await self._request_failure(record, error, attempts=record.attempts + 1)\n        finally:\n            if submitted:\n                submitted.discard()\n''')

replace_once(
    "backend/api/routes.py",
    '''    challenge_id = body.get("challenge_id")\n    method = body.get("method")\n    if not isinstance(challenge_id, str) or not challenge_id or not isinstance(method, str) or not method:\n        raise HTTPException(400, "challenge_id and method are required")\n    values = {name: body[name] for name in ("username", "password", "private_key", "passphrase") if name in body}\n    try:\n        return await application.submit_input(torrent_id, challenge_id=challenge_id, method=method, values=values)\n    except KeyError:\n        raise HTTPException(404, "Transfer not found") from None\n    except ValueError:\n        raise HTTPException(409, "Authentication input was not accepted") from None\n''',
    '''    allowed_fields = {"challenge_id", "method", "username", "password", "private_key", "passphrase"}\n    if set(body) - allowed_fields:\n        body.clear()\n        raise HTTPException(400, "Authentication input contains unsupported fields")\n    challenge_id = body.get("challenge_id")\n    method = body.get("method")\n    if not isinstance(challenge_id, str) or not challenge_id or not isinstance(method, str) or not method:\n        body.clear()\n        raise HTTPException(400, "challenge_id and method are required")\n    values = {name: body[name] for name in ("username", "password", "private_key", "passphrase") if name in body}\n    try:\n        return await application.submit_input(torrent_id, challenge_id=challenge_id, method=method, values=values)\n    except KeyError:\n        raise HTTPException(404, "Transfer not found") from None\n    except ValueError:\n        raise HTTPException(409, "Authentication input was not accepted") from None\n    finally:\n        values.clear()\n        body.clear()\n''')

p = Path("backend/tests/test_input_required_lifecycle.py")
text = p.read_text()
if "test_provider_continuation_reacquires_resolution_capacity_before_consuming_secret" not in text:
    text += r'''

@pytest.mark.asyncio
async def test_provider_continuation_reacquires_resolution_capacity_before_consuming_secret(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})

    await engine._resolution_slots.acquire()
    task = asyncio.create_task(engine.resolve_pending())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert provider.continuation_calls == 0
    assert await engine.inputs.has(challenge)
    engine._resolution_slots.release()
    await task
    assert provider.continuation_calls == 1
    assert not await engine.inputs.has(challenge)
'''
    p.write_text(text)

Path("backend/tests/test_input_required_api.py").write_text(r'''import json

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
''')
