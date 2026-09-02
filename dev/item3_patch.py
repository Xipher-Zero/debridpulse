from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_one(path, old, new):
    p = ROOT / path
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"Expected exactly one match in {path}: {old[:80]!r}; found {text.count(old)}")
    p.write_text(text.replace(old, new, 1))


def append_before(path, marker, content):
    replace_one(path, marker, content + marker)


# ---------------------------------------------------------------------------
# Canonical value model
# ---------------------------------------------------------------------------
replace_one(
    "backend/transfers/models.py",
    'class ResourceState(StrEnum):\n',
    '''class InputReason(StrEnum):\n    AUTH_REQUIRED = "auth_required"\n\n\nclass InputOrigin(StrEnum):\n    PROVIDER = "provider"\n    EXECUTOR = "executor"\n\n\nclass InputMethod(StrEnum):\n    USERNAME_PASSWORD = "username_password"\n    USERNAME_PRIVATE_KEY = "username_private_key"\n\n\nclass InputField(StrEnum):\n    USERNAME = "username"\n    PASSWORD = "password"\n    PRIVATE_KEY = "private_key"\n    PASSPHRASE = "passphrase"\n\n\n@dataclass(frozen=True)\nclass InputFieldDescriptor:\n    name: InputField\n    required: bool\n\n\n@dataclass(frozen=True)\nclass InputMethodDescriptor:\n    method: InputMethod\n    fields: tuple[InputFieldDescriptor, ...]\n\n    def __post_init__(self):\n        actual = {item.name: item.required for item in self.fields}\n        if len(actual) != len(self.fields):\n            raise ValueError("Authentication field descriptors must be unique")\n        if self.method == InputMethod.USERNAME_PASSWORD:\n            expected = {InputField.USERNAME: True, InputField.PASSWORD: True}\n        elif self.method == InputMethod.USERNAME_PRIVATE_KEY:\n            expected = {InputField.USERNAME: True, InputField.PRIVATE_KEY: True, InputField.PASSPHRASE: False}\n        else:\n            raise ValueError("Unsupported authentication method")\n        if actual != expected:\n            raise ValueError("Authentication method fields do not match the canonical contract")\n\n\n@dataclass(frozen=True)\nclass InputRequirement:\n    reason: InputReason\n    methods: tuple[InputMethodDescriptor, ...]\n\n    def __post_init__(self):\n        if self.reason != InputReason.AUTH_REQUIRED:\n            raise ValueError("Unsupported input-required reason")\n        if not self.methods or len({item.method for item in self.methods}) != len(self.methods):\n            raise ValueError("Authentication challenges require unique accepted methods")\n\n\n@dataclass(frozen=True)\nclass InputChallenge:\n    id: str\n    transfer_id: int\n    generation: int\n    reason: InputReason\n    origin: InputOrigin\n    integration_id: str\n    operation_id: str\n    methods: tuple[InputMethodDescriptor, ...]\n    request_id: str | None = None\n    artifact_id: int | None = None\n\n    @property\n    def requirement(self) -> InputRequirement:\n        return InputRequirement(self.reason, self.methods)\n\n\nclass ResourceState(StrEnum):\n''',
)
replace_one(
    "backend/transfers/models.py",
    '    RESOLVING = "processing"\n    READY = "ready"\n',
    '    RESOLVING = "processing"\n    INPUT_REQUIRED = "input_required"\n    READY = "ready"\n',
)
replace_one(
    "backend/transfers/models.py",
    'class ResolutionResult:\n    """Candidates are alternatives for one request; manifests describe members."""\n    state: ResourceState\n    candidates: tuple[TransferCandidate, ...] = ()\n    observation: ProviderObservation | None = None\n    error: NormalizedError | None = None\n',
    'class ResolutionResult:\n    """Candidates are alternatives for one request; manifests describe members."""\n    state: ResourceState\n    candidates: tuple[TransferCandidate, ...] = ()\n    observation: ProviderObservation | None = None\n    error: NormalizedError | None = None\n    input_required: InputRequirement | None = None\n',
)

# ---------------------------------------------------------------------------
# Transient input + durable non-secret challenge store
# ---------------------------------------------------------------------------
(ROOT / "backend/transfers/input_required.py").write_text(r'''"""Neutral INPUT_REQUIRED challenge persistence and transient secret delivery.

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


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS transfer_input_challenges (
        transfer_id INTEGER PRIMARY KEY REFERENCES torrents(id),
        challenge_id TEXT NOT NULL UNIQUE,
        generation INTEGER NOT NULL CHECK(generation > 0),
        reason TEXT NOT NULL,
        origin TEXT NOT NULL,
        integration_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        request_id TEXT,
        artifact_id INTEGER,
        methods TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transfer_input_challenge_id ON transfer_input_challenges(challenge_id)",
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
        async with get_db() as db:
            for statement in _SCHEMA:
                await db.execute(statement)
            await db.commit()

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
''')

# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
replace_one(
    "backend/transfers/contracts.py",
    'from transfers.models import (\n    CleanupDirective, ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionSnapshot,\n    HealthObservation, IntegrationDescriptor, ProviderObservation,\n    ProviderResource, ResolutionResult, ResourceSnapshot, TransferCandidate,\n    TransferOutcome, TransferRequest, SourceEntry, ArtifactFingerprint,\n)\n',
    'from transfers.input_required import SubmittedInput\nfrom transfers.models import (\n    CleanupDirective, ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionSnapshot,\n    HealthObservation, InputRequirement, IntegrationDescriptor, ProviderObservation,\n    ProviderResource, ResolutionResult, ResourceSnapshot, TransferCandidate,\n    TransferOutcome, TransferRequest, SourceEntry, ArtifactFingerprint,\n)\n',
)
replace_one(
    "backend/transfers/contracts.py",
    '    def prepare(self, request: ExecutionRequest) -> ExecutionHandle:\n        """Allocate a handle without remote contact; core persists it before start."""\n        ...\n',
    '    def prepare(self, request: ExecutionRequest) -> ExecutionHandle | InputRequirement:\n        """Allocate a handle or request transient input without remote mutation."""\n        ...\n',
)
append_before(
    "backend/transfers/contracts.py",
    '\n\n@runtime_checkable\nclass ResourceLookup(Protocol):',
    '''\n\n@runtime_checkable\nclass ProviderInputContinuation(Protocol):\n    async def resolve_with_input(self, request: TransferRequest, submitted: SubmittedInput) -> ResolutionResult: ...\n''',
)
append_before(
    "backend/transfers/contracts.py",
    '\n\n@runtime_checkable\nclass PauseResume(Protocol):',
    '''\n\n@runtime_checkable\nclass ExecutorInputContinuation(Protocol):\n    def prepare_with_input(self, request: ExecutionRequest, submitted: SubmittedInput) -> ExecutionHandle | InputRequirement: ...\n''',
)

# ---------------------------------------------------------------------------
# Persistence codec must reject transient credentials structurally.
# ---------------------------------------------------------------------------
replace_one(
    "backend/transfers/codec.py",
    'from transfers.errors import NormalizedError\n',
    'from transfers.errors import NormalizedError\nfrom transfers.input_required import SubmittedInput\n',
)
replace_one(
    "backend/transfers/codec.py",
    'def _value(value):\n    if isinstance(value, NormalizedError):\n',
    'def _value(value):\n    if isinstance(value, SubmittedInput):\n        raise TypeError("Transient input cannot be persisted")\n    if isinstance(value, NormalizedError):\n',
)

# ---------------------------------------------------------------------------
# Registry: expose neutral eligible executor set so challenge continuation stays
# pinned to its issuing executor without consulting private registry internals.
# ---------------------------------------------------------------------------
replace_one(
    "backend/transfers/registry.py",
    '    def executor_for(self, candidate: TransferCandidate) -> Executor:\n        schemes = {endpoint.scheme for endpoint in candidate.endpoints}\n        matches = [executor for executor in self.executors.values()\n                   if executor.descriptor.enabled and executor.descriptor.id not in self._unhealthy\n                   and schemes & executor.descriptor.schemes]\n        if not matches:\n',
    '    def eligible_executors(self, candidate: TransferCandidate) -> tuple[Executor, ...]:\n        schemes = {endpoint.scheme for endpoint in candidate.endpoints}\n        matches = [executor for executor in self.executors.values()\n                   if executor.descriptor.enabled and executor.descriptor.id not in self._unhealthy\n                   and schemes & executor.descriptor.schemes]\n        return tuple(sorted(matches, key=lambda item: (-item.descriptor.priority, item.descriptor.id)))\n\n    def executor_for(self, candidate: TransferCandidate) -> Executor:\n        matches = self.eligible_executors(candidate)\n        if not matches:\n',
)
replace_one(
    "backend/transfers/registry.py",
    '        return sorted(matches, key=lambda item: (-item.descriptor.priority, item.descriptor.id))[0]\n',
    '        return matches[0]\n',
)

# ---------------------------------------------------------------------------
# Repository integration: safe presentation and input-backed execution prepare.
# ---------------------------------------------------------------------------
replace_one(
    "backend/transfers/repository.py",
    'from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError\n',
    'from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError\nfrom transfers.input_required import public_challenge\n',
)
replace_one(
    "backend/transfers/repository.py",
    '            events = await db.fetchall("SELECT id,torrent_id,level,message,created_at FROM events WHERE torrent_id=? ORDER BY id DESC LIMIT 50", (transfer_id,)) if details else []\n',
    '            events = await db.fetchall("SELECT id,torrent_id,level,message,created_at FROM events WHERE torrent_id=? ORDER BY id DESC LIMIT 50", (transfer_id,)) if details else []\n            input_challenge = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))\n',
)
replace_one(
    "backend/transfers/repository.py",
    '        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})\n',
    '        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})\n        result["input_required"] = public_challenge(input_challenge)\n',
)
replace_one(
    "backend/transfers/repository.py",
    '    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle) -> bool:\n',
    '    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle, *, from_input_required: bool = False) -> bool:\n',
)
replace_one(
    "backend/transfers/repository.py",
    '                WHERE f.id=? AND f.status=\'queued\' AND f.execution_attempt_id IS NULL\n',
    '                WHERE f.id=? AND f.status=? AND f.execution_attempt_id IS NULL\n',
)
replace_one(
    "backend/transfers/repository.py",
    '                AND t.status NOT IN (\'deleted\',\'completed\') AND COALESCE(p.paused,0)=0""", (artifact.id,))\n',
    '                AND t.status NOT IN (\'deleted\',\'completed\',\'cancelled\') AND COALESCE(p.paused,0)=0""",\n                (artifact.id, "input_required" if from_input_required else "queued"))\n',
)
replace_one(
    "backend/transfers/repository.py",
    '            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,\n                normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (handle.attempt_id, handle.executor_id, artifact.id))\n',
    '            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,\n                status=CASE WHEN ? THEN \'queued\' ELSE status END,normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",\n                (handle.attempt_id, handle.executor_id, int(from_input_required), artifact.id))\n',
)

# ---------------------------------------------------------------------------
# Engine: canonical lifecycle owner.
# ---------------------------------------------------------------------------
replace_one(
    "backend/transfers/engine.py",
    'from transfers.contracts import BatchObservation, CandidateRefresh, Cleanup, Inventory, Manifest, PauseResume, ResourceLookup\n',
    'from transfers.contracts import (BatchObservation, CandidateRefresh, Cleanup, ExecutorInputContinuation, Inventory, Manifest,\n    PauseResume, ProviderInputContinuation, ResourceLookup)\n',
)
replace_one(
    "backend/transfers/engine.py",
    'from transfers.filesystem import destination, payload_matches, retire_partial, safe_name, stable_payload, validate_target\n',
    'from transfers.filesystem import destination, payload_matches, retire_partial, safe_name, stable_payload, validate_target\nfrom transfers.input_required import EphemeralInputBroker, InputChallengeStore, InputSubmissionRejected\n',
)
replace_one(
    "backend/transfers/engine.py",
    '    Artifact, CancellationInitiator, CleanupAuthority, CleanupDirective,\n    ExecutionObservation, ExecutionRequest, ExecutionState, OutcomeKind, Ownership,\n    RequestRecord, ResolutionResult, ResourceState, TransferOutcome, TransferRequest,\n    TransferState, new_identity,\n',
    '    Artifact, CancellationInitiator, CleanupAuthority, CleanupDirective,\n    ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionState, InputChallenge, InputOrigin, InputRequirement,\n    OutcomeKind, Ownership, RequestRecord, ResolutionAttempt, ResolutionResult, ResourceState, TransferOutcome, TransferRequest,\n    TransferState, new_identity,\n',
)
replace_one(
    "backend/transfers/engine.py",
    '        self.registry = registry\n        self.root = str(Path(download_root).resolve())\n',
    '        self.registry = registry\n        self.challenges = InputChallengeStore(clock=clock)\n        self.inputs = EphemeralInputBroker(clock=clock)\n        self.root = str(Path(download_root).resolve())\n',
)
replace_one(
    "backend/transfers/engine.py",
    '    async def initialize(self):\n        await self.repository.initialize()\n',
    '    async def initialize(self):\n        await self.repository.initialize()\n        await self.challenges.initialize()\n',
)
replace_one(
    "backend/transfers/engine.py",
    '        if not transfer or transfer.state in {TransferState.DELETED, TransferState.COMPLETED}:\n',
    '        if not transfer or transfer.state in {TransferState.DELETED, TransferState.COMPLETED, TransferState.CANCELLED}:\n',
)
replace_one(
    "backend/transfers/engine.py",
    '                    if not await self._live(transfer.id, admission=True):\n                        return\n                    records = await self.repository.requests(transfer.id)\n',
    '                    if not await self._live(transfer.id, admission=True):\n                        return\n                    challenge = await self.challenges.current(transfer.id)\n                    if challenge:\n                        if challenge.origin == InputOrigin.PROVIDER:\n                            await self._continue_provider_input(challenge)\n                        return\n                    records = await self.repository.requests(transfer.id)\n',
)
replace_one(
    "backend/transfers/engine.py",
    '            transfers = await self.repository.active()\n            artifacts_by_transfer = {transfer.id: await self.repository.artifacts(transfer.id) for transfer in transfers}\n            grouped = {}\n            for artifacts in artifacts_by_transfer.values():\n                for artifact in artifacts:\n',
    '            transfers = await self.repository.active()\n            artifacts_by_transfer = {transfer.id: await self.repository.artifacts(transfer.id) for transfer in transfers}\n            challenges = {transfer.id: await self.challenges.current(transfer.id) for transfer in transfers}\n            grouped = {}\n            for transfer in transfers:\n                if challenges[transfer.id]:\n                    continue\n                for artifact in artifacts_by_transfer[transfer.id]:\n',
)
replace_one(
    "backend/transfers/engine.py",
    '            for transfer in transfers:\n                await self._process_executions(transfer.id, artifacts_by_transfer[transfer.id], observations)\n',
    '            for transfer in transfers:\n                challenge = challenges[transfer.id]\n                if challenge:\n                    if challenge.origin == InputOrigin.EXECUTOR and await self._live(transfer.id, admission=True):\n                        await self._continue_executor_input(challenge, artifacts_by_transfer[transfer.id])\n                    continue\n                await self._process_executions(transfer.id, artifacts_by_transfer[transfer.id], observations)\n',
)
replace_one(
    "backend/transfers/engine.py",
    '            if not isinstance(result, ResolutionResult):\n                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))\n            live = await self.repository.resolution(attempt, result)\n            if not live:\n                if await self.repository.delete_remote_requested(record.transfer_id):\n                    await self._cleanup_resources(record.transfer_id, explicit=True)\n                return\n            if result.error:\n                await self._request_failure(record, result.error, attempts=record.attempts + 1)\n            elif result.candidates:\n                await self._materialize(record, result.candidates)\n            elif result.observation:\n                if result.observation.name and record.parent_id is None:\n                    await self.repository.rename(record.transfer_id, safe_name(result.observation.name))\n                if result.observation.state == ResourceState.AVAILABLE:\n                    await self._observe_resource(replace(record, resource=result.observation.resource, state="waiting", attempts=record.attempts + 1))\n            else:\n                raise TransferError(self._error(Category.NO_TRANSFER_CANDIDATE, Stage.RESOLUTION, domain=Domain.RESOLUTION))\n',
    '            await self._apply_resolution(record, attempt, provider, result)\n',
)
append_before(
    "backend/transfers/engine.py",
    '\n    async def _observe_resource(self, record: RequestRecord):',
    r'''

    async def _apply_resolution(self, record: RequestRecord, attempt: ResolutionAttempt, provider, result: ResolutionResult,
                                *, challenge: InputChallenge | None = None):
        if not isinstance(result, ResolutionResult):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))
        if result.input_required:
            if result.error or result.candidates or result.observation or not isinstance(result.input_required, InputRequirement):
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))
            if not isinstance(provider, ProviderInputContinuation):
                raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST,
                                                retryability=Retryability.NEVER))
            if challenge:
                await self.challenges.replace(challenge, result.input_required)
            else:
                await self.challenges.wait_provider(attempt, result.input_required, provider.descriptor.id)
            return
        live = await self.repository.resolution(attempt, result)
        if challenge:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
        if not live:
            if await self.repository.delete_remote_requested(record.transfer_id):
                await self._cleanup_resources(record.transfer_id, explicit=True)
            return
        if result.error:
            await self._request_failure(record, result.error, attempts=record.attempts + 1)
        elif result.candidates:
            await self._materialize(record, result.candidates)
        elif result.observation:
            if result.observation.name and record.parent_id is None:
                await self.repository.rename(record.transfer_id, safe_name(result.observation.name))
            if result.observation.state == ResourceState.AVAILABLE:
                await self._observe_resource(replace(record, resource=result.observation.resource, state="waiting", attempts=record.attempts + 1))
        else:
            raise TransferError(self._error(Category.NO_TRANSFER_CANDIDATE, Stage.RESOLUTION, domain=Domain.RESOLUTION))

    async def _continue_provider_input(self, challenge: InputChallenge):
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        records = await self.repository.requests(challenge.transfer_id)
        record = next((item for item in records if item.id == challenge.request_id), None)
        if record is None or record.state != "input_required":
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        eligible = {item.descriptor.id: item for item in self.registry.eligible_providers(record.request)}
        provider = eligible.get(challenge.integration_id)
        if not isinstance(provider, ProviderInputContinuation):
            return
        submitted = await self.inputs.take(challenge)
        if submitted is None:
            return
        try:
            result = await provider.resolve_with_input(record.request, submitted)
            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")
            await self._apply_resolution(record, attempt, provider, result, challenge=challenge)
        except Exception as exc:
            secrets = submitted.secret_values()
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)
            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")
            await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self.challenges.clear(challenge)
            await self._request_failure(record, error, attempts=record.attempts + 1)
        finally:
            submitted.discard()
''',
)
replace_one(
    "backend/transfers/engine.py",
    '''    async def _dispatch(self, artifact: Artifact):\n        try:\n            validate_target(self.root, artifact.target)\n            candidate = artifact.candidates[artifact.selected]\n            executor = self.registry.executor_for(candidate)\n            sidecars = executor.resumable_paths(artifact.target)\n            if await stable_payload(artifact.target, artifact.expected_bytes, sidecars=sidecars, integrity=candidate.integrity,\n                                    delay=self.policy.adoption_stability_seconds):\n                await self.repository.artifact_state(artifact.id, "completed")\n                return\n            if candidate.expires_at is not None and candidate.expires_at <= self.clock():\n                await self._refresh(artifact)\n                return\n            request = ExecutionRequest(candidate, artifact.target, new_identity())\n            handle = executor.prepare(request)\n            async with self._dispatch_lock:\n                if not self.dispatch_permitted or not await self._live(artifact.transfer_id, admission=True):\n                    return\n                attempts = await self.repository.live_executions()\n                occupied = sum(attempt.state in {"prepared", "queued", "transferring", "unknown"} for attempt in attempts)\n                if occupied >= max(1, self.policy.max_active_executions):\n                    return\n                if not await self.repository.prepare_execution(artifact, handle):\n                    return\n            try:\n                observed = await executor.start(request, handle)\n            except Exception as exc:\n                observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,\n                    error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.QUEUE))\n            current = next(item for item in await self.repository.artifacts(artifact.transfer_id) if item.id == artifact.id)\n            await self._execution_result(current, executor, observed)\n        except Exception as exc:\n            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id="", domain=Domain.INTERNAL, stage=Stage.QUEUE)\n            await self.repository.artifact_state(artifact.id, "error", error=error)\n''',
    '''    async def _dispatch(self, artifact: Artifact):\n        try:\n            validate_target(self.root, artifact.target)\n            candidate = artifact.candidates[artifact.selected]\n            executor = self.registry.executor_for(candidate)\n            sidecars = executor.resumable_paths(artifact.target)\n            if await stable_payload(artifact.target, artifact.expected_bytes, sidecars=sidecars, integrity=candidate.integrity,\n                                    delay=self.policy.adoption_stability_seconds):\n                await self.repository.artifact_state(artifact.id, "completed")\n                return\n            if candidate.expires_at is not None and candidate.expires_at <= self.clock():\n                await self._refresh(artifact)\n                return\n            request = ExecutionRequest(candidate, artifact.target, new_identity())\n            prepared = executor.prepare(request)\n            if isinstance(prepared, InputRequirement):\n                if not isinstance(executor, ExecutorInputContinuation):\n                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE, domain=Domain.REQUEST, retryability=Retryability.NEVER))\n                await self.challenges.wait_executor(artifact, executor.descriptor.id, request.attempt_id, prepared)\n                return\n            if not isinstance(prepared, ExecutionHandle) or prepared.executor_id != executor.descriptor.id or prepared.attempt_id != request.attempt_id:\n                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.QUEUE))\n            handle = prepared\n            async with self._dispatch_lock:\n                if not self.dispatch_permitted or not await self._live(artifact.transfer_id, admission=True):\n                    return\n                attempts = await self.repository.live_executions()\n                occupied = sum(attempt.state in {"prepared", "queued", "transferring", "unknown"} for attempt in attempts)\n                if occupied >= max(1, self.policy.max_active_executions):\n                    return\n                if not await self.repository.prepare_execution(artifact, handle):\n                    return\n            try:\n                observed = await executor.start(request, handle)\n            except Exception as exc:\n                observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,\n                    error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.QUEUE))\n            current = next(item for item in await self.repository.artifacts(artifact.transfer_id) if item.id == artifact.id)\n            await self._execution_result(current, executor, observed)\n        except Exception as exc:\n            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id="", domain=Domain.INTERNAL, stage=Stage.QUEUE)\n            await self.repository.artifact_state(artifact.id, "error", error=error)\n''',
)
append_before(
    "backend/transfers/engine.py",
    '\n    async def _execution_result(self, artifact, executor, observed):',
    r'''

    async def _continue_executor_input(self, challenge: InputChallenge, artifacts):
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        artifact = next((item for item in artifacts if item.id == challenge.artifact_id), None)
        if artifact is None or artifact.state != "input_required" or not artifact.candidates:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        candidate = artifact.candidates[artifact.selected]
        eligible = {item.descriptor.id: item for item in self.registry.eligible_executors(candidate)}
        executor = eligible.get(challenge.integration_id)
        if not isinstance(executor, ExecutorInputContinuation):
            return
        request = ExecutionRequest(candidate, artifact.target, challenge.operation_id)
        submitted = None
        try:
            async with self._dispatch_lock:
                if not self.dispatch_permitted or not await self._live(challenge.transfer_id, admission=True):
                    return
                attempts = await self.repository.live_executions()
                occupied = sum(item.state in {"prepared", "queued", "transferring", "unknown"} for item in attempts)
                if occupied >= max(1, self.policy.max_active_executions):
                    return
                submitted = await self.inputs.take(challenge)
                if submitted is None:
                    return
                prepared = executor.prepare_with_input(request, submitted)
                if isinstance(prepared, InputRequirement):
                    await self.challenges.replace(challenge, prepared)
                    return
                if not isinstance(prepared, ExecutionHandle) or prepared.executor_id != challenge.integration_id or prepared.attempt_id != challenge.operation_id:
                    raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.QUEUE))
                if not await self.repository.prepare_execution(artifact, prepared, from_input_required=True):
                    return
                handle = prepared
            await self.challenges.clear(challenge)
            try:
                observed = await executor.start(request, handle)
            except Exception as exc:
                observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                    error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR,
                                          stage=Stage.QUEUE, secrets=submitted.secret_values()))
            current = next(item for item in await self.repository.artifacts(challenge.transfer_id) if item.id == artifact.id)
            await self._execution_result(current, executor, observed)
        except Exception as exc:
            secrets = submitted.secret_values() if submitted else ()
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=challenge.integration_id, domain=Domain.EXECUTOR, stage=Stage.QUEUE, secrets=secrets)
            await self.challenges.clear(challenge)
            await self.repository.artifact_state(artifact.id, "error", error=error)
            await self.repository.outcome(challenge.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
        finally:
            if submitted:
                submitted.discard()
''',
)
replace_one(
    "backend/transfers/engine.py",
    '        transfer = await self.repository.get(transfer_id)\n        requests = await self.repository.requests(transfer_id)\n',
    '        transfer = await self.repository.get(transfer_id)\n        challenge = await self.challenges.current(transfer_id)\n        if challenge:\n            if transfer.state != TransferState.INPUT_REQUIRED:\n                await self.repository.state(transfer_id, TransferState.INPUT_REQUIRED)\n            return\n        requests = await self.repository.requests(transfer_id)\n',
)
replace_one(
    "backend/transfers/engine.py",
    '        if not results:\n            await self.repository.state(transfer_id, TransferState.QUEUED if resume else TransferState.PAUSED)\n',
    '        if not results and not await self.challenges.current(transfer_id):\n            await self.repository.state(transfer_id, TransferState.QUEUED if resume else TransferState.PAUSED)\n',
)
replace_one(
    "backend/transfers/engine.py",
    '            if transfer is None:\n                raise KeyError(transfer_id)\n            if transfer.state == TransferState.DELETED and not reacquire:\n',
    '            if transfer is None:\n                raise KeyError(transfer_id)\n            if await self.challenges.current(transfer_id):\n                return False\n            if transfer.state == TransferState.DELETED and not reacquire:\n',
)
append_before(
    "backend/transfers/engine.py",
    '\n    async def delete(self, transfer_id: int, *, remote=True):',
    r'''

    async def submit_input(self, transfer_id: int, challenge_id: str, method: str, values):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        challenge = await self.challenges.current(transfer_id)
        if transfer.state != TransferState.INPUT_REQUIRED or challenge is None or challenge.id != challenge_id:
            raise InputSubmissionRejected("Input challenge is stale")
        await self.inputs.submit(challenge, method, values)
        return challenge

    async def cancel(self, transfer_id: int):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        challenge = await self.challenges.current(transfer_id)
        if challenge:
            await self.inputs.clear(challenge.id)
            await self.challenges.clear_transfer(transfer_id)
        for artifact in await self.repository.artifacts(transfer_id):
            if artifact.execution:
                executor = self.registry.executors.get(artifact.execution.executor_id)
                if executor:
                    outcome = await executor.cancel(artifact.execution)
                    await self.repository.outcome(transfer_id, outcome, attempt_id=artifact.execution.attempt_id)
            if artifact.state != "completed":
                await self.repository.artifact_state(artifact.id, "cancelled")
        await self.repository.state(transfer_id, TransferState.CANCELLED)
        return True
''',
)
replace_one(
    "backend/transfers/engine.py",
    '    async def delete(self, transfer_id: int, *, remote=True):\n        # Persist the tombstone before waiting on any integration. Every async\n',
    '    async def delete(self, transfer_id: int, *, remote=True):\n        challenge = await self.challenges.current(transfer_id)\n        if challenge:\n            await self.inputs.clear(challenge.id)\n        await self.challenges.clear_transfer(transfer_id)\n        # Persist the tombstone before waiting on any integration. Every async\n',
)

# ---------------------------------------------------------------------------
# Application/API command surface: no secret echo and no saved credentials.
# ---------------------------------------------------------------------------
append_before(
    "backend/application/service.py",
    '\n    async def pause(self, transfer_id):',
    r'''

    async def submit_input(self, transfer_id, *, challenge_id, method, values):
        async with self.application_operation():
            challenge = await self.engine.submit_input(transfer_id, challenge_id, method, values)
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            await self._publish(transfer_id)
            return {"ok": True, "accepted": True, "id": transfer_id, "challenge_id": challenge.id}

    async def cancel(self, transfer_id):
        async with self.application_operation():
            await self.require(transfer_id)
            await self.engine.cancel(transfer_id)
            await self._publish(transfer_id)
            return {"ok": True}
''',
)
append_before(
    "backend/api/routes.py",
    '\n\n@router.post("/torrents/{torrent_id}/retry")',
    r'''

@router.post("/torrents/{torrent_id}/input")
async def submit_transfer_input(torrent_id: int, request: Request, application: ApplicationService = Depends(get_application)):
    raw = await request.body()
    if len(raw) > 512 * 1024:
        raise HTTPException(413, "Authentication input is too large")
    try:
        body = _json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "Authentication input must be a JSON object") from None
    finally:
        raw = b""
    if not isinstance(body, dict):
        raise HTTPException(400, "Authentication input must be a JSON object")
    challenge_id = body.get("challenge_id")
    method = body.get("method")
    if not isinstance(challenge_id, str) or not challenge_id or not isinstance(method, str) or not method:
        raise HTTPException(400, "challenge_id and method are required")
    values = {name: body[name] for name in ("username", "password", "private_key", "passphrase") if name in body}
    try:
        return await application.submit_input(torrent_id, challenge_id=challenge_id, method=method, values=values)
    except KeyError:
        raise HTTPException(404, "Transfer not found") from None
    except ValueError:
        raise HTTPException(409, "Authentication input was not accepted") from None


@router.post("/torrents/{torrent_id}/cancel")
async def cancel_torrent(torrent_id: int, application: ApplicationService = Depends(get_application)):
    try:
        return await application.cancel(torrent_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found") from None
''',
)

# ---------------------------------------------------------------------------
# Explicit DB backup/wipe awareness for the new non-secret challenge table.
# ---------------------------------------------------------------------------
replace_one(
    "backend/services/db_maintenance.py",
    '    "integration_runtime_state",\n    "schema_migrations",\n',
    '    "integration_runtime_state",\n    "transfer_input_challenges",\n    "schema_migrations",\n',
)
replace_one(
    "backend/services/db_maintenance.py",
    '    "integration_runtime_state": "integration_id,state_key",\n    "schema_migrations": "version",\n',
    '    "integration_runtime_state": "integration_id,state_key",\n    "transfer_input_challenges": "transfer_id",\n    "schema_migrations": "version",\n',
)
replace_one(
    "backend/services/db_maintenance.py",
    '        await db.execute("DELETE FROM integration_runtime_state")\n        for table in ("application_events", "postprocess_attempts", "transfer_outcomes", "execution_attempts", "resolution_attempts", "provider_resources"):\n',
    '        await db.execute("DELETE FROM integration_runtime_state")\n        await db.execute("DELETE FROM transfer_input_challenges")\n        for table in ("application_events", "postprocess_attempts", "transfer_outcomes", "execution_attempts", "resolution_attempts", "provider_resources"):\n',
)

# ---------------------------------------------------------------------------
# Deterministic unrelated provider/executor proofs.
# ---------------------------------------------------------------------------
(ROOT / "backend/tests/test_input_required_lifecycle.py").write_text(r'''"""Neutral INPUT_REQUIRED/AUTH_REQUIRED lifecycle proof with unrelated integrations."""
import json
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor
from transfers import codec
from transfers.engine import TransferEngine
from transfers.input_required import auth_required, username_password, username_private_key
from transfers.models import (
    Capability, Endpoint, ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionState,
    InputField, InputMethod, IntegrationDescriptor, ResourceState, ResolutionResult,
    TransferCandidate, TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class AuthParcelProvider:
    def __init__(self):
        self.descriptor = IntegrationDescriptor("auth-parcel", "Auth parcel", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"auth-parcel"}))
        self.resolve_calls = 0
        self.continuation_calls = 0

    async def resolve(self, request):
        self.resolve_calls += 1
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))

    async def resolve_with_input(self, request, submitted):
        self.continuation_calls += 1
        if (submitted.method == InputMethod.USERNAME_PASSWORD
                and submitted.value(InputField.USERNAME) == "provider-user-sentinel"
                and submitted.value(InputField.PASSWORD) == "provider-password-sentinel"):
            candidate = TransferCandidate("parcel.bin", (Endpoint("memory", "memory:parcel"),), expected_bytes=4,
                                          provider_id=self.descriptor.id)
            return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))


class StaticProvider:
    def __init__(self, scheme="keymem"):
        self.scheme = scheme
        self.descriptor = IntegrationDescriptor("static-parcel", "Static parcel", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"key-parcel"}))

    async def resolve(self, request):
        candidate = TransferCandidate("key.bin", (Endpoint(self.scheme, self.scheme + ":payload"),), expected_bytes=4,
                                      provider_id=self.descriptor.id)
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))


class KeyExecutor(MemoryExecutor):
    def __init__(self, authorize, *, encrypted):
        super().__init__(authorize)
        self.encrypted = encrypted
        self.prepare_calls = 0
        self.input_calls = 0
        self.descriptor = IntegrationDescriptor("key-copy", "Key copy", frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}),
                                                schemes=frozenset({"keymem"}))

    def prepare(self, request):
        self.prepare_calls += 1
        return auth_required(username_private_key())

    def prepare_with_input(self, request, submitted):
        self.input_calls += 1
        accepted = (submitted.method == InputMethod.USERNAME_PRIVATE_KEY
                    and submitted.value(InputField.USERNAME) == "executor-user-sentinel"
                    and submitted.value(InputField.PRIVATE_KEY) == "executor-private-key-sentinel")
        if self.encrypted:
            accepted = accepted and submitted.value(InputField.PASSPHRASE) == "executor-passphrase-sentinel"
        if not accepted:
            return auth_required(username_private_key())
        return ExecutionHandle(self.descriptor.id, {"copy_ticket": request.attempt_id, "destination": request.target}, request.attempt_id)


@pytest_asyncio.fixture
async def base(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    now = [1000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=1)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return repository, registry, engine, now


async def db_text():
    async with database.get_db() as db:
        names = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'") if not row["name"].startswith("sqlite_")]
        payload = {}
        for name in names:
            payload[name] = await db.fetchall(f"SELECT * FROM {name}")
    return json.dumps(payload, sort_keys=True, default=str)


@pytest.mark.asyncio
async def test_provider_auth_wait_is_nonterminal_budget_neutral_and_same_transfer_continues(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source", name="parcel.bin"),))
    await engine.tick()
    waiting = await repository.get(transfer.id)
    challenge = await engine.challenges.current(transfer.id)
    assert waiting.state == TransferState.INPUT_REQUIRED
    assert challenge.reason.value == "auth_required" and challenge.origin.value == "provider"
    assert (await repository.requests(transfer.id))[0].attempts == 0
    for _ in range(3):
        await engine.tick()
    assert provider.resolve_calls == 1
    assert not await repository.live_executions()
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    current = await repository.get(transfer.id)
    assert current.id == transfer.id and current.state == TransferState.TRANSFERRING
    artifact = (await repository.artifacts(transfer.id))[0]
    executor.finish(artifact.execution)
    await engine.tick()
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await engine.challenges.current(transfer.id) is None


@pytest.mark.asyncio
async def test_rejected_provider_auth_supersedes_generation_and_stale_submission_is_rejected(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    first = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, first.id, "username_password", {"username": "wrong", "password": "wrong"})
    await engine.tick()
    second = await engine.challenges.current(transfer.id)
    assert second.id != first.id and second.generation == first.generation + 1
    assert (await repository.requests(transfer.id))[0].attempts == 0
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, first.id, "username_password", {"username": "stale", "password": "stale"})


@pytest.mark.asyncio
async def test_private_key_passphrase_is_optional_in_challenge_and_unencrypted_key_continues_without_it(base):
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = KeyExecutor(repository.authorize_execution, encrypted=False)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    descriptor = challenge.methods[0]
    fields = {field.name: field.required for field in descriptor.fields}
    assert descriptor.method == InputMethod.USERNAME_PRIVATE_KEY
    assert fields[InputField.USERNAME] is True and fields[InputField.PRIVATE_KEY] is True
    assert fields[InputField.PASSPHRASE] is False
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.retries == 0 and not await repository.live_executions()
    await engine.submit_input(transfer.id, challenge.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None and artifact.retries == 1
    executor.finish(artifact.execution)
    await engine.tick()
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED


@pytest.mark.asyncio
async def test_encrypted_key_missing_passphrase_rechallenges_then_accepts_optional_passphrase(base):
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = KeyExecutor(repository.authorize_execution, encrypted=True)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    await engine.tick()
    first = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, first.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})
    await engine.tick()
    second = await engine.challenges.current(transfer.id)
    assert second.generation == first.generation + 1
    assert next(field for field in second.methods[0].fields if field.name == InputField.PASSPHRASE).required is False
    await engine.submit_input(transfer.id, second.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel",
        "passphrase": "executor-passphrase-sentinel"})
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None


@pytest.mark.asyncio
async def test_pause_preserves_input_required_and_delays_transient_continuation(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.pause(transfer.id)
    paused = await repository.get(transfer.id)
    assert paused.paused and paused.state == TransferState.INPUT_REQUIRED
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    assert provider.continuation_calls == 0
    await engine.resume(transfer.id)
    await engine.tick()
    assert provider.continuation_calls == 1


@pytest.mark.asyncio
async def test_restart_restores_challenge_but_not_submitted_values(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    restarted = TransferEngine(TransferRepository(), registry, download_root=engine.root, policy=engine.policy, clock=engine.clock)
    await restarted.initialize()
    restored = await restarted.challenges.current(transfer.id)
    assert restored.id == challenge.id and (await restarted.repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
    await restarted.tick()
    assert provider.continuation_calls == 0
    await restarted.submit_input(transfer.id, restored.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await restarted.tick()
    assert provider.continuation_calls == 1


@pytest.mark.asyncio
async def test_delete_and_cancel_invalidate_waiting_challenge(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    deleted = await engine.submit((TransferRequest("auth-parcel", "delete-source"),), deduplicate=False)
    await engine.tick()
    old = await engine.challenges.current(deleted.id)
    await engine.delete(deleted.id, remote=False)
    assert await engine.challenges.current(deleted.id) is None
    with pytest.raises(ValueError):
        await engine.submit_input(deleted.id, old.id, "username_password", {"username": "x", "password": "y"})
    cancelled = await engine.submit((TransferRequest("auth-parcel", "cancel-source"),), deduplicate=False)
    await engine.tick()
    old2 = await engine.challenges.current(cancelled.id)
    await engine.cancel(cancelled.id)
    assert (await repository.get(cancelled.id)).state == TransferState.CANCELLED
    with pytest.raises(ValueError):
        await engine.submit_input(cancelled.id, old2.id, "username_password", {"username": "x", "password": "y"})


@pytest.mark.asyncio
async def test_duplicate_submission_rejected_and_transient_values_never_reach_persistence(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    values = {"username": "provider-user-sentinel", "password": "provider-password-sentinel"}
    await engine.submit_input(transfer.id, challenge.id, "username_password", values)
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, challenge.id, "username_password", values)
    encoded = await db_text()
    assert "provider-user-sentinel" not in encoded and "provider-password-sentinel" not in encoded
    submitted = await engine.inputs.take(challenge)
    assert "sentinel" not in repr(submitted)
    with pytest.raises(TypeError):
        codec.dump(submitted)
    submitted.discard()
''')

print("Item 3 canonical development patch applied")
