"""Neutral INPUT_REQUIRED challenge persistence and transient secret delivery.

Only non-secret challenge metadata is durable. Submitted values are process-local,
bounded, redacted from repr, and structurally rejected by the persistence codec.
"""
from __future__ import annotations

import asyncio
import json
from types import MappingProxyType
import time
from typing import Mapping

from db.database import get_db
from transfers.models import (
    Artifact, InputChallenge, InputField, InputFieldDescriptor, InputMethod,
    InputMethodDescriptor, InputOrigin, InputReason, InputRequirement,
    ResolutionAttempt, new_identity,
)


_TERMINAL_FOR_INPUT = {"completed", "deleted", "cancelled"}


class InputSubmissionRejected(ValueError):
    """A transient submission is invalid, stale, duplicate, or no longer current."""


def username_password() -> InputMethodDescriptor:
    return InputMethodDescriptor(InputMethod.USERNAME_PASSWORD, (
        InputFieldDescriptor(InputField.USERNAME, True),
        InputFieldDescriptor(InputField.PASSWORD, True),
    ))


def username_private_key() -> InputMethodDescriptor:
    return InputMethodDescriptor(InputMethod.USERNAME_PRIVATE_KEY, (
        InputFieldDescriptor(InputField.USERNAME, True),
        InputFieldDescriptor(InputField.PRIVATE_KEY, True),
        InputFieldDescriptor(InputField.PASSPHRASE, False),
    ))


def auth_required(*methods: InputMethodDescriptor) -> InputRequirement:
    return InputRequirement(InputReason.AUTH_REQUIRED, tuple(methods))


def _methods_payload(methods) -> str:
    return json.dumps([
        {
            "method": item.method.value,
            "fields": [{"name": field.name.value, "required": field.required} for field in item.fields],
        }
        for item in methods
    ], separators=(",", ":"), sort_keys=True)


def _methods(value: str) -> tuple[InputMethodDescriptor, ...]:
    raw = json.loads(value)
    return tuple(InputMethodDescriptor(
        InputMethod(item["method"]),
        tuple(InputFieldDescriptor(InputField(field["name"]), bool(field["required"])) for field in item["fields"]),
    ) for item in raw)


def _challenge(row) -> InputChallenge:
    return InputChallenge(
        id=row["challenge_id"], transfer_id=int(row["transfer_id"]), generation=int(row["generation"]),
        reason=InputReason(row["reason"]), origin=InputOrigin(row["origin"]),
        integration_id=row["integration_id"], operation_id=row["operation_id"],
        methods=_methods(row["methods"]), request_id=row.get("request_id"), artifact_id=row.get("artifact_id"),
    )


def public_challenge(value) -> dict | None:
    if value is None:
        return None
    challenge = value if isinstance(value, InputChallenge) else _challenge(value)
    return {
        "id": challenge.id,
        "generation": challenge.generation,
        "reason": challenge.reason.value,
        "origin": challenge.origin.value,
        "methods": [
            {
                "method": item.method.value,
                "fields": [{"name": field.name.value, "required": field.required} for field in item.fields],
            }
            for item in challenge.methods
        ],
    }


class SubmittedInput:
    """Process-local credential bundle. No serialization or public projection exists."""

    __slots__ = ("challenge_id", "generation", "method", "_values")

    def __init__(self, challenge_id: str, generation: int, method: InputMethod, values: Mapping[InputField, str]):
        self.challenge_id = challenge_id
        self.generation = generation
        self.method = method
        self._values = MappingProxyType(dict(values))

    def value(self, field: InputField) -> str | None:
        return self._values.get(field)

    def secret_values(self) -> tuple[str, ...]:
        return tuple(self._values.values())

    def discard(self) -> None:
        self._values = MappingProxyType({})

    def __repr__(self) -> str:
        return f"SubmittedInput(challenge_id={self.challenge_id!r}, generation={self.generation}, method={self.method.value!r}, values=<redacted>)"


def validate_submission(challenge: InputChallenge, method, values: Mapping[str, object]) -> SubmittedInput:
    try:
        selected = InputMethod(method)
    except (TypeError, ValueError):
        raise InputSubmissionRejected("Selected authentication method is not accepted") from None
    descriptor = next((item for item in challenge.methods if item.method == selected), None)
    if descriptor is None:
        raise InputSubmissionRejected("Selected authentication method is not accepted")
    allowed = {field.name for field in descriptor.fields}
    converted = {}
    for raw_name, raw_value in values.items():
        try:
            field = InputField(raw_name)
        except (TypeError, ValueError):
            raise InputSubmissionRejected("Input contains an unsupported field") from None
        if field not in allowed:
            raise InputSubmissionRejected("Input contains a field outside the selected method")
        if not isinstance(raw_value, str):
            raise InputSubmissionRejected("Authentication fields must be text")
        if raw_value:
            converted[field] = raw_value
    for field in descriptor.fields:
        if field.required and not converted.get(field.name):
            raise InputSubmissionRejected("Required authentication input is missing")
    return SubmittedInput(challenge.id, challenge.generation, selected, converted)


class EphemeralInputBroker:
    def __init__(self, *, clock=time.time, lifetime_seconds: float = 120.0):
        self.clock = clock
        self.lifetime_seconds = max(1.0, float(lifetime_seconds))
        self._lock = asyncio.Lock()
        self._pending: dict[str, tuple[float, SubmittedInput]] = {}

    async def submit(self, challenge: InputChallenge, method, values: Mapping[str, object]) -> None:
        submitted = validate_submission(challenge, method, values)
        async with self._lock:
            self._purge_locked()
            if challenge.id in self._pending:
                submitted.discard()
                raise InputSubmissionRejected("Input is already pending for this challenge")
            self._pending[challenge.id] = (self.clock() + self.lifetime_seconds, submitted)

    async def has(self, challenge: InputChallenge) -> bool:
        async with self._lock:
            self._purge_locked()
            return challenge.id in self._pending

    async def take(self, challenge: InputChallenge) -> SubmittedInput | None:
        async with self._lock:
            self._purge_locked()
            entry = self._pending.pop(challenge.id, None)
            if entry is None:
                return None
            submitted = entry[1]
            if submitted.generation != challenge.generation:
                submitted.discard()
                return None
            return submitted

    async def clear(self, challenge_id: str) -> None:
        async with self._lock:
            entry = self._pending.pop(challenge_id, None)
            if entry:
                entry[1].discard()

    def _purge_locked(self):
        now = self.clock()
        for key, (expires, submitted) in tuple(self._pending.items()):
            if expires <= now:
                submitted.discard()
                self._pending.pop(key, None)


class InputChallengeStore:
    """The single durable owner of non-secret transfer challenge metadata."""

    def __init__(self, *, clock=time.time):
        self.clock = clock

    async def initialize(self):
        # Canonical DB initialization owns schema creation. This store owns only
        # challenge lifecycle rows.
        return None

    async def current(self, transfer_id: int) -> InputChallenge | None:
        async with get_db() as db:
            row = await db.fetchone("""SELECT c.*, t.status AS transfer_status,
                (SELECT state FROM resolution_attempts WHERE id=c.operation_id) AS resolution_state,
                (SELECT state FROM transfer_requests WHERE id=c.request_id) AS request_state,
                (SELECT status FROM download_files WHERE id=c.artifact_id) AS artifact_state,
                (SELECT execution_attempt_id FROM download_files WHERE id=c.artifact_id) AS execution_attempt_id
                FROM transfer_input_challenges c JOIN torrents t ON t.id=c.transfer_id WHERE c.transfer_id=?""", (transfer_id,))
            if not row:
                return None
            stale = row["transfer_status"] in _TERMINAL_FOR_INPUT
            if row["origin"] == InputOrigin.PROVIDER.value:
                stale = stale or row["resolution_state"] != "input_required" or row["request_state"] != "input_required"
            else:
                stale = stale or row["artifact_state"] != "input_required" or row["execution_attempt_id"] is not None
            if stale:
                await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
                await db.commit()
                return None
            return _challenge(row)

    async def _next(self, db, transfer_id: int) -> tuple[str, int]:
        row = await db.fetchone("SELECT generation FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
        return new_identity(), (int(row["generation"]) + 1 if row else 1)

    async def wait_provider(self, attempt: ResolutionAttempt, requirement: InputRequirement, integration_id: str) -> InputChallenge:
        now = float(self.clock())
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.transfer_id,r.attempts,t.status,a.provider_id FROM transfer_requests r
                JOIN torrents t ON t.id=r.transfer_id JOIN resolution_attempts a ON a.id=? AND a.request_id=r.id
                WHERE r.id=?""", (attempt.id, attempt.request_id))
            if not row or row["status"] in _TERMINAL_FOR_INPUT or row["provider_id"] != integration_id:
                raise InputSubmissionRejected("Input challenge is no longer applicable")
            identity, generation = await self._next(db, row["transfer_id"])
            challenge = InputChallenge(identity, row["transfer_id"], generation, requirement.reason, InputOrigin.PROVIDER,
                integration_id, attempt.id, requirement.methods, request_id=attempt.request_id)
            await db.execute("""INSERT INTO transfer_input_challenges(transfer_id,challenge_id,generation,reason,origin,integration_id,
                operation_id,request_id,artifact_id,methods,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transfer_id) DO UPDATE SET challenge_id=excluded.challenge_id,generation=excluded.generation,
                reason=excluded.reason,origin=excluded.origin,integration_id=excluded.integration_id,operation_id=excluded.operation_id,
                request_id=excluded.request_id,artifact_id=NULL,methods=excluded.methods,updated_at=excluded.updated_at""",
                (challenge.transfer_id, challenge.id, challenge.generation, challenge.reason.value, challenge.origin.value,
                 challenge.integration_id, challenge.operation_id, challenge.request_id, None, _methods_payload(challenge.methods), now, now))
            await db.execute("UPDATE resolution_attempts SET state='input_required',error=NULL,result=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (attempt.id,))
            await db.execute("UPDATE transfer_requests SET state='input_required',retry_at=0,error=NULL,attempts=MAX(0,attempts-1) WHERE id=?", (attempt.request_id,))
            await db.execute("UPDATE torrents SET status='input_required',normalized_error=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (challenge.transfer_id,))
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer requires authentication input')", (challenge.transfer_id,))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required','auth_required')", (challenge.transfer_id,))
            await db.commit()
            return challenge

    async def wait_executor(self, artifact: Artifact, integration_id: str, operation_id: str, requirement: InputRequirement) -> InputChallenge:
        now = float(self.clock())
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.torrent_id,t.status,f.status AS artifact_state,f.execution_attempt_id FROM download_files f
                JOIN torrents t ON t.id=f.torrent_id WHERE f.id=?""", (artifact.id,))
            if not row or row["status"] in _TERMINAL_FOR_INPUT or row["artifact_state"] != "queued" or row["execution_attempt_id"] is not None:
                raise InputSubmissionRejected("Input challenge is no longer applicable")
            identity, generation = await self._next(db, artifact.transfer_id)
            challenge = InputChallenge(identity, artifact.transfer_id, generation, requirement.reason, InputOrigin.EXECUTOR,
                integration_id, operation_id, requirement.methods, request_id=artifact.request_id, artifact_id=artifact.id)
            await db.execute("""INSERT INTO transfer_input_challenges(transfer_id,challenge_id,generation,reason,origin,integration_id,
                operation_id,request_id,artifact_id,methods,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transfer_id) DO UPDATE SET challenge_id=excluded.challenge_id,generation=excluded.generation,
                reason=excluded.reason,origin=excluded.origin,integration_id=excluded.integration_id,operation_id=excluded.operation_id,
                request_id=excluded.request_id,artifact_id=excluded.artifact_id,methods=excluded.methods,updated_at=excluded.updated_at""",
                (challenge.transfer_id, challenge.id, challenge.generation, challenge.reason.value, challenge.origin.value,
                 challenge.integration_id, challenge.operation_id, challenge.request_id, challenge.artifact_id,
                 _methods_payload(challenge.methods), now, now))
            await db.execute("UPDATE download_files SET status='input_required',normalized_error=NULL WHERE id=?", (artifact.id,))
            await db.execute("UPDATE torrents SET status='input_required',normalized_error=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (artifact.transfer_id,))
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer requires authentication input')", (artifact.transfer_id,))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required','auth_required')", (artifact.transfer_id,))
            await db.commit()
            return challenge

    async def replace(self, challenge: InputChallenge, requirement: InputRequirement) -> InputChallenge:
        now = float(self.clock())
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=? AND challenge_id=? AND generation=?",
                                        (challenge.transfer_id, challenge.id, challenge.generation))
            if not current:
                raise InputSubmissionRejected("Input challenge is stale")
            identity = new_identity()
            generation = challenge.generation + 1
            replacement = InputChallenge(identity, challenge.transfer_id, generation, requirement.reason, challenge.origin,
                challenge.integration_id, challenge.operation_id, requirement.methods, challenge.request_id, challenge.artifact_id)
            await db.execute("""UPDATE transfer_input_challenges SET challenge_id=?,generation=?,reason=?,methods=?,updated_at=?
                WHERE transfer_id=? AND challenge_id=? AND generation=?""",
                (replacement.id, replacement.generation, replacement.reason.value, _methods_payload(replacement.methods), now,
                 challenge.transfer_id, challenge.id, challenge.generation))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required','auth_required')", (challenge.transfer_id,))
            await db.commit()
            return replacement

    async def clear(self, challenge: InputChallenge) -> None:
        async with get_db() as db:
            await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=? AND challenge_id=? AND generation=?",
                             (challenge.transfer_id, challenge.id, challenge.generation))
            await db.commit()

    async def clear_transfer(self, transfer_id: int) -> None:
        async with get_db() as db:
            await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
            await db.commit()
