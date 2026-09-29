"""Neutral INPUT_REQUIRED challenge persistence and transient secret delivery.

Only non-secret challenge metadata is durable: the reason, the accepted methods
and any non-secret facts the operator needs to decide (for example an observed
server identity). Submitted values are process-local, bounded, redacted from
repr, and structurally rejected by the persistence codec.

``EphemeralInputBroker`` is also the one Authentication Input Context owner:
USER_SUPPLIED material enters it either split out of a credential-bearing
resource at an admission boundary or as an answer to a challenge, and no
consumer can tell which. Consumers only emit ordinary requirements; the broker
alone matches, validates (single-flight), reuses within a bounded request
lineage and target scope, rejects and destroys that material. Confirmed server
identities are held beside it as a separate fact, never merged into it.

Transport authentication is a verdict of its own: a transport reports the
moment it definitively ACCEPTED submitted material
(``SubmittedInput.transport_accepted``), independently of whether the listing,
sample or transfer it authenticated for ever completes, and its consumer
settles the material at that moment (``authenticated``). A refusal is reported
as the transport's ordinary requirement; anything else (a timeout, a route or
capacity failure) is no verdict at all. Material that proved access to one
exact candidate is remembered with that candidate (``AccessProof``) so the
canonical owner that later adopts the candidate reuses exactly it
(``adopted_input``) -- never a host-wide credential, never persisted.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
import itertools
import json
from types import MappingProxyType
import time
from typing import Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from db.database import get_db
from transfers.models import (
    Artifact, InputChallenge, InputFact, InputFactName, InputField, InputFieldDescriptor, InputMethod,
    InputMethodDescriptor, InputOrigin, InputReason, InputRequirement,
    ResolutionAttempt, new_identity,
)
from transfers.policy import SIDE_STATE_RETIRING_TRANSFER_STATES
from transfers.requests import AuthScope


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


# The transport input methods a candidate may declare, as the descriptors
# material is matched against.
_METHOD_DESCRIPTORS = {
    InputMethod.USERNAME_PASSWORD: username_password,
    InputMethod.USERNAME_PRIVATE_KEY: username_private_key,
}


def server_identity_confirmation() -> InputMethodDescriptor:
    """Confirm the observed identity only; credentials are already held."""
    return InputMethodDescriptor(InputMethod.SERVER_IDENTITY, ())


def auth_required(*methods: InputMethodDescriptor) -> InputRequirement:
    return InputRequirement(InputReason.AUTH_REQUIRED, tuple(methods))


def server_identity_required(*methods: InputMethodDescriptor, host: str, algorithm: str,
                             fingerprint: str) -> InputRequirement:
    """One challenge: confirm the observed server identity and supply ``methods``."""
    return InputRequirement(InputReason.SERVER_IDENTITY_REQUIRED, tuple(methods), (
        InputFact(InputFactName.SERVER_HOST, host),
        InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, algorithm),
        InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, fingerprint),
    ))


_EVENT_MESSAGES = {
    InputReason.AUTH_REQUIRED: "Transfer requires authentication input",
    InputReason.SERVER_IDENTITY_REQUIRED: "Transfer requires server identity confirmation",
}


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


def _facts_payload(facts) -> str:
    return json.dumps([{"name": fact.name.value, "value": fact.value} for fact in facts],
                      separators=(",", ":"), sort_keys=True)


def _facts(value) -> tuple[InputFact, ...]:
    return tuple(InputFact(InputFactName(item["name"]), item["value"]) for item in json.loads(value or "[]"))


def _challenge(row) -> InputChallenge:
    return InputChallenge(
        id=row["challenge_id"], transfer_id=int(row["transfer_id"]), generation=int(row["generation"]),
        reason=InputReason(row["reason"]), origin=InputOrigin(row["origin"]),
        integration_id=row["integration_id"], operation_id=row["operation_id"],
        methods=_methods(row["methods"]), request_id=row.get("request_id"), artifact_id=row.get("artifact_id"),
        facts=_facts(row.get("facts")),
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
        "facts": [{"name": fact.name.value, "value": fact.value} for fact in challenge.facts],
    }


class SubmittedInput:
    """Process-local credential bundle. No serialization or public projection exists.

    ``facts`` are the answered challenge's non-secret facts, carried so the
    continuation acts on exactly what the operator saw; they are not secrets.
    """

    __slots__ = ("challenge_id", "generation", "method", "_values", "facts", "token", "_accepted", "_listener")

    def __init__(self, challenge_id: str, generation: int, method: InputMethod, values: Mapping[InputField, str],
                 facts: tuple[InputFact, ...] = (), *, token: int | None = None):
        self.challenge_id = challenge_id
        self.generation = generation
        self.method = method
        self._values = MappingProxyType(dict(values))
        self.facts = tuple(facts)
        # Opaque settlement handle into the authentication-input owner; carries
        # no secret and means nothing outside it.
        self.token = token
        self._accepted = False
        self._listener: Callable[[], None] | None = None

    def transport_accepted(self) -> None:
        """Called by a transport at the moment it definitively ACCEPTED this
        material (e.g. SSH user authentication succeeded, an FTP login was
        answered 230) -- before and independently of the operation it
        authenticated for. Idempotent; never called for a refusal or for an
        outcome that is no verdict (timeout, route or capacity failure)."""
        if self._accepted:
            return
        self._accepted = True
        if self._listener is not None:
            self._listener()

    @property
    def accepted_by_transport(self) -> bool:
        return self._accepted

    def on_transport_acceptance(self, listener: Callable[[], None]) -> None:
        """The one consumer's notice of the transport's acceptance (at once,
        when it was already reported)."""
        self._listener = listener
        if self._accepted:
            listener()

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
    return SubmittedInput(challenge.id, challenge.generation, selected, converted, challenge.facts)


# ── Authentication Input Context ────────────────────────────────────────────

def split_user_supplied(payload) -> tuple[object, dict[InputField, str]]:
    """``(sanitized resource, USER_SUPPLIED values)`` for one admitted resource.

    Only what URI semantics define as user credentials -- the authority's
    userinfo -- is extracted; the query, fragment and everything else stay part
    of the resource (a signed or capability URL keeps its authorization). A
    resource without userinfo, or one that is not URL-shaped, is returned
    unchanged with no values."""
    if not isinstance(payload, str) or "://" not in payload:
        return payload, {}
    try:
        parts = urlsplit(payload)
    except ValueError:
        return payload, {}
    if parts.username is None and parts.password is None:
        return payload, {}
    values = {}
    if parts.username:
        values[InputField.USERNAME] = unquote(parts.username)
    if parts.password:
        values[InputField.PASSWORD] = unquote(parts.password)
    sanitized = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, parts.query, parts.fragment))
    return sanitized, values


async def authenticated(operation: Awaitable, submitted: SubmittedInput | None,
                        on_accepted: Callable[[], Awaitable[object]]):
    """Run one transport ``operation`` that uses ``submitted`` and settle the
    material at the transport's definitive acceptance -- not at the end of
    the operation. ``on_accepted`` is awaited at most once, as soon as the
    transport reports acceptance (``SubmittedInput.transport_accepted``),
    while the operation (listing, sampling) keeps running; its result is
    returned unchanged. Without a report nothing is settled here: the caller
    judges the operation's own outcome exactly as before."""
    if submitted is None:
        return await operation
    reported = asyncio.Event()
    submitted.on_transport_acceptance(reported.set)
    work = asyncio.ensure_future(operation)
    notice = asyncio.ensure_future(reported.wait())
    try:
        await asyncio.wait({work, notice}, return_when=asyncio.FIRST_COMPLETED)
        if reported.is_set():
            await on_accepted()
        return await work
    finally:
        notice.cancel()
        if not work.done():
            work.cancel()


@dataclass(frozen=True)
class AccessProof:
    """What an ACCEPTED settlement proved: access to exactly ``candidate_id``,
    by the request ``request_id`` of the settling transfer that owns it."""
    candidate_id: str
    request_id: str


class AuthOutcome(StrEnum):
    SATISFIED = "satisfied"                  # continue now with ``submitted``
    PENDING = "pending"                      # another consumer is validating the same material
    CHALLENGE = "challenge"                  # ask through INPUT_REQUIRED with ``requirement``
    IDENTITY_CHANGED = "identity_changed"    # differs from the identity confirmed in this lineage


@dataclass(frozen=True)
class AuthResolution:
    outcome: AuthOutcome
    submitted: SubmittedInput | None = None
    requirement: InputRequirement | None = None


class _MaterialState(StrEnum):
    UNTESTED = "untested"
    VALID = "valid"
    REJECTED = "rejected"


class _Material:
    """One USER_SUPPLIED answer for one scope. Never serialized; ``origin`` is
    provenance only (admission or operator answer) and never changes matching."""

    __slots__ = ("values", "origin", "state", "lease", "lease_expires")

    def __init__(self, values: Mapping[InputField, str], origin: str):
        self.values = dict(values)
        self.origin = origin
        self.state = _MaterialState.UNTESTED
        self.lease: int | None = None
        self.lease_expires = 0.0

    def __repr__(self) -> str:
        return f"_Material(origin={self.origin!r}, state={self.state.value!r}, values=<redacted>)"


class _Context:
    __slots__ = ("materials", "identity", "established")

    def __init__(self, established: float):
        self.materials: list[_Material] = []
        self.identity: tuple[str, str] | None = None
        self.established = established


class _ProvenAccess:
    """Accepted material that proved access to ONE candidate: the material
    itself (the same object its lineage context holds -- never a copy), the
    method and server-identity facts the transport accepted, the proving
    transfer and request (the candidate's provenance) and the transfers whose
    lifecycle currently holds the candidate (its origin, then the canonical
    owner that adopted it)."""

    __slots__ = ("key", "material", "method", "facts", "origin", "holders", "established")

    def __init__(self, key, material: _Material, method: InputMethod, facts, origin: tuple[int, str],
                 established: float):
        self.key = key
        self.material = material
        self.method = method
        self.facts = tuple(facts)
        self.origin = origin
        self.holders = {origin[0]}
        self.established = established


def _identity_fact(facts) -> tuple[str, str] | None:
    named = {fact.name: fact.value for fact in facts}
    algorithm = named.get(InputFactName.SERVER_IDENTITY_ALGORITHM)
    fingerprint = named.get(InputFactName.SERVER_IDENTITY_FINGERPRINT)
    return (algorithm, fingerprint) if algorithm and fingerprint else None


def _compatible(values: Mapping[InputField, str], methods) -> InputMethodDescriptor | None:
    """The requested method this material answers completely, if any. Material
    never satisfies a mechanism it was not supplied for."""
    for descriptor in methods:
        if descriptor.method == InputMethod.SERVER_IDENTITY:
            continue
        names = {field.name for field in descriptor.fields}
        if set(values) <= names and all(values.get(field.name) for field in descriptor.fields if field.required):
            return descriptor
    return None


class EphemeralInputBroker:
    """The one process-local owner of submitted transient input and of every
    Authentication Input Context.

    Slots, all under one lock and never serialized:

    * pending answers to a current challenge (by challenge id);
    * a one-shot handoff of input that already proved one resolved
      candidate, kept for the writer admitted for that same candidate;
    * lineage contexts ``(transfer, request, scope)`` holding USER_SUPPLIED
      material and, separately, a confirmed server identity. Material supplied
      with a resource belongs to the request that carried it; an interactive
      answer belongs to the lineage root, so every descendant in the same
      target scope can reuse it -- an unrelated root never can.

    Validation is single-flight: untested material is leased to one consumer
    at a time and others wait (``PENDING``) for its settlement. Rejected
    material is never offered again. Semantic lifetime is primary -- a
    transfer's contexts are destroyed when it is cancelled, deleted or reaches
    a terminal state; ``context_ceiling_seconds`` is only a safety ceiling.

    An ACCEPTED settlement that names the candidate it proved (``AccessProof``)
    also keeps that candidate's proven access: the same material object, the
    accepted method and server identity, and the candidate's provenance. A
    canonical owner that adopted exactly that candidate reuses exactly that
    access (``adopted_input``); a transfer ending hands it to the canonical
    owner that adopted the candidate, or destroys it. Nothing is keyed by a
    hostname, and nothing outlives every transfer holding the candidate.
    """

    def __init__(self, *, clock=time.time, lifetime_seconds: float = 120.0,
                 context_ceiling_seconds: float = 24 * 3600.0):
        self.clock = clock
        self.lifetime_seconds = max(1.0, float(lifetime_seconds))
        self.context_ceiling_seconds = max(self.lifetime_seconds, float(context_ceiling_seconds))
        self._lock = asyncio.Lock()
        self._pending: dict[str, tuple[float, SubmittedInput, int]] = {}
        self._handoffs: dict[tuple[int, str, str, str], tuple[float, SubmittedInput]] = {}
        self._contexts: dict[tuple[int, str, AuthScope], _Context] = {}
        # token -> (context key, material, leased method, leased facts)
        self._tokens: dict[int, tuple[tuple[int, str, AuthScope], _Material, InputMethod, tuple]] = {}
        self._in_use: dict[tuple[int, str, str], int] = {}
        # candidate id -> the access that candidate's transport accepted
        self._proven: dict[str, _ProvenAccess] = {}
        self._token_sequence = itertools.count(1)

    # ── answers to a current challenge ──────────────────────────────────────

    async def submit(self, challenge: InputChallenge, method, values: Mapping[str, object]) -> None:
        submitted = validate_submission(challenge, method, values)
        async with self._lock:
            self._purge_locked()
            if challenge.id in self._pending:
                submitted.discard()
                raise InputSubmissionRejected("Input is already pending for this challenge")
            self._pending[challenge.id] = (self.clock() + self.lifetime_seconds, submitted, challenge.transfer_id)

    async def has(self, challenge: InputChallenge) -> bool:
        async with self._lock:
            self._purge_locked()
            return challenge.id in self._pending

    async def take(self, challenge: InputChallenge, *, chain: tuple[str, ...] = (),
                   scope: AuthScope | None = None) -> SubmittedInput | None:
        """Consume the answer to ``challenge`` for ``chain`` (the challenged
        request first, its lineage root last) in ``scope``.

        The answer becomes USER_SUPPLIED material of the lineage root, so every
        descendant in the same scope reuses it; a confirmed identity is recorded
        separately. An identity-only answer is completed from material this
        lineage already holds. The returned input is leased for validation:
        its consumer settles it."""
        async with self._lock:
            self._purge_locked()
            entry = self._pending.pop(challenge.id, None)
            if entry is None:
                return None
            submitted = entry[1]
            if submitted.generation != challenge.generation:
                submitted.discard()
                return None
            if scope is None:
                return submitted
            root = chain[-1] if chain else str(challenge.request_id or "")
            context = self._context_locked(challenge.transfer_id, root, scope)
            identity = _identity_fact(submitted.facts)
            if challenge.reason == InputReason.SERVER_IDENTITY_REQUIRED and identity is not None:
                context.identity = identity
            if submitted.method != InputMethod.SERVER_IDENTITY:
                material = _Material({field: submitted.value(field) for field in InputField if submitted.value(field)},
                                     "operator")
                context.materials.append(material)
                # An answer to a question that carried no server identity (a
                # credential asked again) still travels with the identity this
                # lineage already confirmed for the same scope -- never re-asked.
                facts = submitted.facts if identity is not None else (
                    self._confirmed_facts_locked(challenge.transfer_id, chain or (root,), scope) or submitted.facts)
                return self._leased_locked((challenge.transfer_id, root, scope), material, submitted.method,
                                           facts, challenge_id=challenge.id,
                                           generation=challenge.generation, discard=submitted)
            chosen = self._material_locked(challenge.transfer_id, chain or (root,), scope, (username_password(),))
            if chosen is None:
                return submitted
            key, material, descriptor = chosen
            return self._leased_locked(key, material, descriptor.method, submitted.facts,
                                       challenge_id=challenge.id, generation=challenge.generation, discard=submitted)

    async def clear(self, challenge_id: str) -> None:
        async with self._lock:
            entry = self._pending.pop(challenge_id, None)
            if entry:
                entry[1].discard()

    # ── USER_SUPPLIED material and matching ─────────────────────────────────

    async def supply(self, transfer_id: int, request_id: str, scope: AuthScope | None,
                     values: Mapping[InputField, str], *, origin: str) -> bool:
        """Admit USER_SUPPLIED material carried by ``request_id``'s resource."""
        if scope is None or not values:
            return False
        async with self._lock:
            self._purge_locked()
            self._context_locked(int(transfer_id), str(request_id), scope).materials.append(_Material(values, origin))
            return True

    async def resolve(self, transfer_id: int, chain: tuple[str, ...], scope: AuthScope | None,
                      requirement: InputRequirement) -> AuthResolution:
        """Match one ordinary requirement against this lineage's context.

        ``chain`` is the consuming request first and its lineage root last."""
        async with self._lock:
            self._purge_locked()
            if scope is None or not chain:
                return AuthResolution(AuthOutcome.CHALLENGE, requirement=requirement)
            transfer_id = int(transfer_id)
            observed = _identity_fact(requirement.facts)
            identity_needed = requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
            confirmed = next((context.identity for context in self._chain_locked(transfer_id, chain, scope)
                              if context.identity is not None), None) if identity_needed else None
            if identity_needed and confirmed is not None and confirmed != observed:
                return AuthResolution(AuthOutcome.IDENTITY_CHANGED)
            chosen = self._material_locked(transfer_id, chain, scope, requirement.methods)
            if identity_needed and confirmed is None:
                if chosen is None:
                    return AuthResolution(AuthOutcome.CHALLENGE, requirement=requirement)
                # Credentials are held: only the identity is asked for.
                return AuthResolution(AuthOutcome.CHALLENGE, requirement=InputRequirement(
                    requirement.reason, (server_identity_confirmation(),), requirement.facts))
            if chosen is None:
                return AuthResolution(AuthOutcome.CHALLENGE, requirement=requirement)
            key, material, descriptor = chosen
            if (material.state == _MaterialState.UNTESTED and material.lease is not None
                    and material.lease_expires > self.clock()):
                return AuthResolution(AuthOutcome.PENDING)
            return AuthResolution(AuthOutcome.SATISFIED, submitted=self._leased_locked(
                key, material, descriptor.method, requirement.facts))

    async def settle(self, token: int | None, *, accepted: bool,
                     proof: AccessProof | None = None) -> tuple[str, str] | None:
        """Record what the consumer observed with leased material.

        Returns ``("auth_accepted" | "auth_rejected", origin)`` only when this
        settlement changed the material's state, so each material produces one
        coalesced indication however many siblings used it; else ``None``.
        Rejected material is destroyed and never offered again. An accepted
        settlement with a ``proof`` also keeps the proven candidate's access:
        exactly this material, with the method and facts it was leased under."""
        if token is None:
            return None
        async with self._lock:
            entry = self._tokens.pop(int(token), None)
            if entry is None:
                return None
            key, material, method, facts = entry
            if material.lease == token:
                material.lease = None
            if accepted and proof is not None and material.values:
                known = self._proven.get(str(proof.candidate_id))
                # Access re-proven with the very material an earlier proof
                # recorded (e.g. by the adopting owner's writer) keeps that
                # proof's provenance; new material records its own.
                if known is None or known.material is not material:
                    self._proven[str(proof.candidate_id)] = _ProvenAccess(
                        key, material, method, self._identity_facts_locked(key, facts),
                        (int(key[0]), str(proof.request_id)), self.clock())
            if accepted:
                if material.state == _MaterialState.UNTESTED:
                    material.state = _MaterialState.VALID
                    return "auth_accepted", material.origin
                return None
            if material.state == _MaterialState.REJECTED:
                return None
            material.state = _MaterialState.REJECTED
            material.values = {}
            return "auth_rejected", material.origin

    async def valid_for(self, transfer_id: int, chain: tuple[str, ...], scope: AuthScope | None) -> bool:
        """Whether accepted material already serves this lineage and scope."""
        async with self._lock:
            self._purge_locked()
            if scope is None:
                return False
            return any(material.state == _MaterialState.VALID
                       for context in self._chain_locked(int(transfer_id), chain, scope)
                       for material in context.materials)

    async def writer_input(self, transfer_id: int, request_id: str, candidate_id: str, chain: tuple[str, ...],
                           scope: AuthScope | None, methods) -> SubmittedInput | None:
        """Already-VALID material of this lineage and scope for a writer
        admitted without an exact candidate handoff, or ``None``.

        Only material a consumer already accepted (e.g. the core-run discovery
        that classified this source) is eligible -- never untested or rejected
        material -- and only for a method the candidate declares (``methods``).
        The server identity this lineage confirmed for the scope travels as the
        canonical facts; none is invented when there is none. The lease is
        held in use by ``candidate_id``, so its execution settles it exactly
        like a handed-off input."""
        descriptors = tuple(_METHOD_DESCRIPTORS[method]() for method in methods if method in _METHOD_DESCRIPTORS)
        async with self._lock:
            self._purge_locked()
            if scope is None or not chain or not descriptors:
                return None
            transfer_id = int(transfer_id)
            chosen = self._material_locked(transfer_id, chain, scope, descriptors, usable=(_MaterialState.VALID,))
            if chosen is None:
                return None
            key, material, descriptor = chosen
            identity = next((context.identity for context in self._chain_locked(transfer_id, chain, scope)
                             if context.identity is not None), None)
            facts = () if identity is None else (
                InputFact(InputFactName.SERVER_HOST, scope.host),
                InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, identity[0]),
                InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, identity[1]),
            )
            submitted = self._leased_locked(key, material, descriptor.method, facts)
            self._in_use[(transfer_id, str(request_id), str(candidate_id))] = submitted.token
            return submitted

    async def adopted_input(self, transfer_id: int, request_id: str, candidate_id: str, scope: AuthScope | None,
                            methods, *, origin: tuple[int, str] | None) -> SubmittedInput | None:
        """The proven access of exactly ``candidate_id`` for a writer of a
        canonical owner that adopted it, or ``None``.

        Every fence must hold: the candidate's own provenance (``origin``:
        the contributing transfer and request, from canonical ownership) is
        the one that proved it; the dispatch scope -- family, host and port --
        is the proven scope; the proven method is one the candidate declares
        (``methods``); the material is still VALID. The confirmed server
        identity travels as the canonical facts, so the transport pins exactly
        it (a changed identity fails closed there). The lease is held in use by
        ``candidate_id`` for this writer and settles like any other."""
        async with self._lock:
            self._purge_locked()
            entry = self._proven.get(str(candidate_id))
            if (entry is None or origin is None or scope is None
                    or (int(origin[0]), str(origin[1])) != entry.origin or entry.key[2] != scope
                    or entry.method not in set(methods)
                    or entry.material.state != _MaterialState.VALID or not entry.material.values
                    or _compatible(entry.material.values, (_METHOD_DESCRIPTORS[entry.method](),)) is None):
                return None
            submitted = self._leased_locked(entry.key, entry.material, entry.method, entry.facts)
            self._in_use[(int(transfer_id), str(request_id), str(candidate_id))] = submitted.token
            return submitted

    async def lease_for_proof(self, scope: AuthScope | None, methods,
                              requirement: InputRequirement) -> SubmittedInput | None:
        """Lend already-VALID material of exactly ``scope`` to ONE equivalence
        fingerprint of a candidate the deciding request does not own, or ``None``.

        Proof only, never inheritance: the lease enters no lineage, no use
        record and no writer handoff; its consumer ends it
        (``end_proof_lease``). Any lineage's VALID material of the exact
        authenticated scope (family, host, port) may be lent, for a method both
        the candidate declares (``methods``) and the sampler requested. When
        the sampler requires a server identity, the lending context must have
        confirmed exactly the identity observed -- no identity is probed or
        invented -- and the lease carries it. Untested and rejected material
        is never lent."""
        requested = {descriptor.method for descriptor in getattr(requirement, "methods", ())}
        descriptors = tuple(_METHOD_DESCRIPTORS[method]() for method in methods
                            if method in _METHOD_DESCRIPTORS and method in requested)
        if scope is None or not descriptors:
            return None
        identity_needed = requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
        observed = _identity_fact(requirement.facts)
        if identity_needed and observed is None:
            return None
        async with self._lock:
            self._purge_locked()
            for key, context in self._contexts.items():
                if key[2] != scope or (identity_needed and context.identity != observed):
                    continue
                for material in reversed(context.materials):
                    if material.state != _MaterialState.VALID:
                        continue
                    descriptor = _compatible(material.values, descriptors)
                    if descriptor is None:
                        continue
                    facts = requirement.facts if identity_needed else () if context.identity is None else (
                        InputFact(InputFactName.SERVER_HOST, scope.host),
                        InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, context.identity[0]),
                        InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, context.identity[1]),
                    )
                    return self._leased_locked(key, material, descriptor.method, facts)
            return None

    async def end_proof_lease(self, submitted: SubmittedInput | None, *, rejected: bool) -> tuple[str, str] | None:
        """End a proof lease. Only a definitive credential refusal (``rejected``)
        invalidates the lent material, through the one settlement; any other
        outcome -- a proof, a transport failure, a fail-closed identity --
        leaves it exactly as valid as it was."""
        if submitted is None or submitted.token is None:
            return None
        if rejected:
            return await self.settle(submitted.token, accepted=False)
        async with self._lock:
            self._tokens.pop(int(submitted.token), None)
        return None

    # ── handoff to the admitted writer ──────────────────────────────────────

    @staticmethod
    def _handoff_key(transfer_id: int, request_id: str, candidate_id: str, integration_id: str):
        return int(transfer_id), str(request_id), str(candidate_id), str(integration_id)

    async def hand_off(self, transfer_id: int, request_id: str, candidate_id: str, integration_id: str,
                       submitted: SubmittedInput) -> None:
        """Keep input that proved one candidate's evidence for that candidate's writer."""
        key = self._handoff_key(transfer_id, request_id, candidate_id, integration_id)
        async with self._lock:
            self._purge_locked()
            previous = self._handoffs.pop(key, None)
            if previous:
                previous[1].discard()
            self._handoffs[key] = (self.clock() + self.lifetime_seconds, submitted)

    async def take_handoff(self, transfer_id: int, request_id: str, candidate_id: str,
                           integration_id: str) -> SubmittedInput | None:
        """Consume the handoff for exactly these identities, at most once.

        Admission of a writer for this request is also the point at which any
        handoff it holds for a different candidate or integration became
        stale (the selected candidate was replaced): those are discarded. A
        consumed handoff that still has a settlement pending is remembered as
        in use by this candidate until its execution settles it."""
        key = self._handoff_key(transfer_id, request_id, candidate_id, integration_id)
        async with self._lock:
            self._purge_locked()
            entry = self._handoffs.pop(key, None)
            for stale in [item for item in self._handoffs if item[:2] == key[:2]]:
                self._handoffs.pop(stale)[1].discard()
            if entry is None:
                return None
            submitted = entry[1]
            if submitted.token is not None:
                self._in_use[key[:3]] = submitted.token
            return submitted

    async def mark_use(self, transfer_id: int, request_id: str, candidate_id: str, token: int | None) -> None:
        if token is None:
            return
        async with self._lock:
            self._in_use[(int(transfer_id), str(request_id), str(candidate_id))] = int(token)

    async def release_use(self, transfer_id: int, request_id: str, candidate_id: str) -> int | None:
        async with self._lock:
            return self._in_use.pop((int(transfer_id), str(request_id), str(candidate_id)), None)

    # ── lifetime ────────────────────────────────────────────────────────────

    async def holds(self, transfer_id: int) -> bool:
        """Whether any secret-bearing material of ``transfer_id`` is resident."""
        transfer_id = int(transfer_id)
        async with self._lock:
            self._purge_locked()
            return (any(key[0] == transfer_id for key in self._contexts)
                    or any(key[0] == transfer_id for key in self._handoffs)
                    or any(entry[2] == transfer_id for entry in self._pending.values())
                    or any(transfer_id in entry.holders for entry in self._proven.values()))

    async def discard_transfer(self, transfer_id: int, *, adopted: Mapping[str, int] | None = None) -> None:
        """Destroy everything this transfer's lineages hold, immediately --
        except the proven access of a candidate another transfer's canonical
        artifact adopted (``adopted``: candidate id -> that canonical
        transfer), which from now on lives exactly as long as the adopter."""
        transfer_id = int(transfer_id)
        adopted = {str(candidate): int(owner) for candidate, owner in dict(adopted or {}).items()}
        async with self._lock:
            for candidate_id, entry in tuple(self._proven.items()):
                if transfer_id not in entry.holders:
                    continue
                entry.holders.discard(transfer_id)
                if candidate_id in adopted and adopted[candidate_id] != transfer_id:
                    entry.holders.add(adopted[candidate_id])
                if not entry.holders:
                    self._proven.pop(candidate_id)
            for key in [key for key in self._handoffs if key[0] == transfer_id]:
                self._handoffs.pop(key)[1].discard()
            for challenge_id in [cid for cid, entry in self._pending.items() if entry[2] == transfer_id]:
                self._pending.pop(challenge_id)[1].discard()
            for key in [key for key in self._contexts if key[0] == transfer_id]:
                self._drop_context_locked(key)
            for key in [key for key in self._in_use if key[0] == transfer_id]:
                self._in_use.pop(key)
            self._forget_unreferenced_locked()

    # ── internals ───────────────────────────────────────────────────────────

    def _context_locked(self, transfer_id: int, request_id: str, scope: AuthScope) -> _Context:
        key = (int(transfer_id), str(request_id), scope)
        context = self._contexts.get(key)
        if context is None:
            context = self._contexts[key] = _Context(self.clock())
        return context

    def _identity_facts_locked(self, key, facts) -> tuple[InputFact, ...]:
        """The server identity a proven access pins: the one its lease carried,
        else the one its lineage confirmed for the scope; none is invented."""
        identity = _identity_fact(facts)
        if identity is None:
            context = self._contexts.get(key)
            identity = context.identity if context is not None else None
        scope = key[2]
        if identity is None or not scope.host:
            return ()
        return (InputFact(InputFactName.SERVER_HOST, scope.host),
                InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, identity[0]),
                InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, identity[1]))

    def _confirmed_facts_locked(self, transfer_id: int, chain, scope) -> tuple[InputFact, ...]:
        """The server identity this lineage confirmed for ``scope``, as the
        facts an identity-bound consumer reads; empty when none was."""
        confirmed = next((context.identity for context in self._chain_locked(transfer_id, chain, scope)
                          if context.identity is not None), None)
        if confirmed is None or not scope.host:
            return ()
        algorithm, fingerprint = confirmed
        return (InputFact(InputFactName.SERVER_HOST, scope.host),
                InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, algorithm),
                InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, fingerprint))

    def _chain_locked(self, transfer_id: int, chain, scope):
        for request_id in chain:
            context = self._contexts.get((transfer_id, str(request_id), scope))
            if context is not None:
                yield context

    def _material_locked(self, transfer_id: int, chain, scope, methods,
                         usable=(_MaterialState.UNTESTED, _MaterialState.VALID)):
        """Nearest-in-lineage, newest-first usable material for ``methods``."""
        for request_id in chain:
            key = (transfer_id, str(request_id), scope)
            context = self._contexts.get(key)
            if context is None:
                continue
            for material in reversed(context.materials):
                if material.state not in usable:
                    continue
                descriptor = _compatible(material.values, methods)
                if descriptor is not None:
                    return key, material, descriptor
        return None

    def _leased_locked(self, key, material: _Material, method: InputMethod, facts, *, challenge_id: str = "",
                       generation: int = 0, discard: SubmittedInput | None = None) -> SubmittedInput:
        token = next(self._token_sequence)
        self._tokens[token] = (key, material, method, tuple(facts))
        if material.state == _MaterialState.UNTESTED:
            material.lease = token
            material.lease_expires = self.clock() + self.lifetime_seconds
        if discard is not None:
            discard.discard()
        return SubmittedInput(challenge_id, generation, method, material.values, facts, token=token)

    def _drop_context_locked(self, key) -> None:
        """Drop one lineage context and destroy its material -- except
        material a live proven access still refers to (an adopted candidate's
        access outlives the lineage that proved it)."""
        context = self._contexts.pop(key, None)
        if context is None:
            return
        kept = {id(entry.material) for entry in self._proven.values()}
        for token in [token for token, entry in self._tokens.items()
                      if entry[0] == key and id(entry[1]) not in kept]:
            self._tokens.pop(token)
        for material in context.materials:
            if id(material) not in kept:
                material.values = {}

    def _forget_unreferenced_locked(self) -> None:
        """Destroy material no context and no proven access refers to any more."""
        live = {id(material) for context in self._contexts.values() for material in context.materials}
        live |= {id(entry.material) for entry in self._proven.values()}
        for token, entry in tuple(self._tokens.items()):
            if id(entry[1]) not in live:
                self._tokens.pop(token)
                entry[1].values = {}

    def _purge_locked(self):
        now = self.clock()
        for key, (expires, submitted, *_rest) in tuple(self._pending.items()):
            if expires <= now:
                submitted.discard()
                self._pending.pop(key, None)
        for key, (expires, submitted) in tuple(self._handoffs.items()):
            if expires <= now:
                submitted.discard()
                self._handoffs.pop(key, None)
        expired = False
        for candidate_id, entry in tuple(self._proven.items()):
            if (entry.established + self.context_ceiling_seconds <= now
                    or entry.material.state == _MaterialState.REJECTED or not entry.material.values):
                self._proven.pop(candidate_id)
                expired = True
        for key, context in tuple(self._contexts.items()):
            if context.established + self.context_ceiling_seconds <= now:
                self._drop_context_locked(key)
                expired = True
        if expired:
            self._forget_unreferenced_locked()


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
                (SELECT execution_attempt_id FROM download_files WHERE id=c.artifact_id) AS execution_attempt_id,
                (SELECT id FROM download_files WHERE request_id=c.request_id LIMIT 1) AS request_artifact
                FROM transfer_input_challenges c JOIN torrents t ON t.id=c.transfer_id WHERE c.transfer_id=?""", (transfer_id,))
            if not row:
                return None
            stale = row["transfer_status"] in SIDE_STATE_RETIRING_TRANSFER_STATES
            if row["origin"] == InputOrigin.PROVIDER.value:
                stale = stale or row["resolution_state"] != "input_required" or row["request_state"] != "input_required"
            elif row["origin"] == InputOrigin.EVIDENCE.value:
                # Evidence is acquired only while the request is still deciding
                # its materialization and owns no artifact yet. Candidate and
                # sampler currency are the engine's to judge (it owns the
                # resolved candidate set and the registry).
                stale = stale or row["request_state"] != "materializing" or bool(row["request_artifact"])
            else:
                execution_id = row["execution_attempt_id"]
                stale = stale or row["artifact_state"] != "input_required" or execution_id not in {None, row["operation_id"]}
            if stale:
                await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
                await db.commit()
                return None
            return _challenge(row)

    async def _next(self, db, transfer_id: int) -> tuple[str, int]:
        row = await db.fetchone("SELECT generation FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
        return new_identity(), (int(row["generation"]) + 1 if row else 1)

    async def wait_provider(self, attempt: ResolutionAttempt, requirement: InputRequirement,
                            integration_id: str) -> InputChallenge | None:
        """Durably ask a resolution's question -- unless another request of
        the transfer already has its question outstanding: a transfer asks one
        question at a time, and one request's question never overwrites
        another's. ``None`` means ask later (the caller holds the request)."""
        now = float(self.clock())
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.transfer_id,r.attempts,t.status,a.provider_id FROM transfer_requests r
                JOIN torrents t ON t.id=r.transfer_id JOIN resolution_attempts a ON a.id=? AND a.request_id=r.id
                WHERE r.id=?""", (attempt.id, attempt.request_id))
            if not row or row["status"] in SIDE_STATE_RETIRING_TRANSFER_STATES or row["provider_id"] != integration_id:
                raise InputSubmissionRejected("Input challenge is no longer applicable")
            outstanding = await db.fetchone("SELECT request_id FROM transfer_input_challenges WHERE transfer_id=?",
                                            (row["transfer_id"],))
            if outstanding and outstanding["request_id"] != attempt.request_id:
                await db.rollback()
                return None
            identity, generation = await self._next(db, row["transfer_id"])
            challenge = InputChallenge(identity, row["transfer_id"], generation, requirement.reason, InputOrigin.PROVIDER,
                integration_id, attempt.id, requirement.methods, request_id=attempt.request_id, facts=requirement.facts)
            await db.execute("""INSERT INTO transfer_input_challenges(transfer_id,challenge_id,generation,reason,origin,integration_id,
                operation_id,request_id,artifact_id,methods,facts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transfer_id) DO UPDATE SET challenge_id=excluded.challenge_id,generation=excluded.generation,
                reason=excluded.reason,origin=excluded.origin,integration_id=excluded.integration_id,operation_id=excluded.operation_id,
                request_id=excluded.request_id,artifact_id=NULL,methods=excluded.methods,facts=excluded.facts,updated_at=excluded.updated_at""",
                (challenge.transfer_id, challenge.id, challenge.generation, challenge.reason.value, challenge.origin.value,
                 challenge.integration_id, challenge.operation_id, challenge.request_id, None, _methods_payload(challenge.methods),
                 _facts_payload(challenge.facts), now, now))
            await db.execute("UPDATE resolution_attempts SET state='input_required',error=NULL,result=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (attempt.id,))
            await db.execute("UPDATE transfer_requests SET state='input_required',retry_at=0,error=NULL,attempts=MAX(0,attempts-1) WHERE id=?", (attempt.request_id,))
            await db.execute("UPDATE torrents SET status='input_required',normalized_error=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (challenge.transfer_id,))
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info',?)", (challenge.transfer_id, _EVENT_MESSAGES[challenge.reason]))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required',?)", (challenge.transfer_id, challenge.reason.value))
            await db.commit()
            return challenge

    async def hold_provider(self, attempt: ResolutionAttempt, integration_id: str) -> bool:
        """Hold one resolution unasked while another question of its transfer
        is outstanding: a transfer asks one question at a time. The request
        keeps no challenge of its own; ``release_provider_holds`` returns it
        to ordinary resolution once nothing is being asked."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT t.status,a.provider_id FROM transfer_requests r
                JOIN torrents t ON t.id=r.transfer_id JOIN resolution_attempts a ON a.id=? AND a.request_id=r.id
                WHERE r.id=?""", (attempt.id, attempt.request_id))
            if not row or row["status"] in SIDE_STATE_RETIRING_TRANSFER_STATES or row["provider_id"] != integration_id:
                await db.rollback()
                return False
            await db.execute("UPDATE resolution_attempts SET state='input_required',error=NULL,result=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (attempt.id,))
            await db.execute("UPDATE transfer_requests SET state='input_required',retry_at=0,error=NULL,attempts=MAX(0,attempts-1) WHERE id=?", (attempt.request_id,))
            await db.commit()
            return True

    async def accept_provider(self, challenge: InputChallenge) -> bool:
        """The transport accepted the answer to this provider question: the
        question retires NOW, while the resolution it answered goes on. One
        transaction, fenced on the exact challenge id and generation: the row
        is removed and the answered request and attempt return to resolving
        -- their state before they asked -- so no scheduler reads them as
        held, released or unanswered. True only when this call retired it."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "DELETE FROM transfer_input_challenges WHERE transfer_id=? AND challenge_id=? AND generation=?",
                (challenge.transfer_id, challenge.id, challenge.generation))
            if not cursor.rowcount:
                await db.rollback()
                return False
            await db.execute("UPDATE resolution_attempts SET state='started',updated_at=CURRENT_TIMESTAMP "
                             "WHERE id=? AND state='input_required'", (challenge.operation_id,))
            await db.execute("UPDATE transfer_requests SET state='resolving' WHERE id=? AND state='input_required'",
                             (challenge.request_id,))
            await db.commit()
            return True

    async def release_provider_holds(self, transfer_id: int) -> bool:
        """Resolutions held unasked (or whose question another one replaced)
        return to ordinary resolution when the transfer asks nothing; each
        then matches the settled lineage answer itself or asks its own."""
        async with get_db() as db:
            if not await db.fetchone("SELECT 1 FROM transfer_requests WHERE transfer_id=? AND state='input_required'",
                                     (transfer_id,)):
                return False  # the common case: a read, never a write, per cycle
            cursor = await db.execute("""UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL
                WHERE transfer_id=? AND state='input_required'
                AND NOT EXISTS (SELECT 1 FROM transfer_input_challenges WHERE transfer_id=?)""",
                (transfer_id, transfer_id))
            await db.commit()
            return bool(cursor.rowcount)

    async def wait_evidence(self, transfer_id: int, request_id: str, candidate_id: str, integration_id: str,
                            requirement: InputRequirement) -> InputChallenge:
        """Durably challenge pre-writer evidence acquisition for one resolved candidate.

        The request stays in MATERIALIZING -- it is still deciding, and a
        request-level ``input_required`` would read to its cohort siblings as
        a settled source -- while the transfer surfaces INPUT_REQUIRED. The
        candidate identity is the challenge's operation.
        """
        now = float(self.clock())
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.state,t.status,
                (SELECT id FROM download_files WHERE request_id=r.id LIMIT 1) AS artifact
                FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id WHERE r.id=? AND r.transfer_id=?""",
                (request_id, transfer_id))
            if (not row or row["status"] in SIDE_STATE_RETIRING_TRANSFER_STATES or row["state"] != "materializing"
                    or row["artifact"]):
                raise InputSubmissionRejected("Input challenge is no longer applicable")
            identity, generation = await self._next(db, transfer_id)
            challenge = InputChallenge(identity, transfer_id, generation, requirement.reason, InputOrigin.EVIDENCE,
                integration_id, str(candidate_id), requirement.methods, request_id=request_id, facts=requirement.facts)
            await db.execute("""INSERT INTO transfer_input_challenges(transfer_id,challenge_id,generation,reason,origin,integration_id,
                operation_id,request_id,artifact_id,methods,facts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transfer_id) DO UPDATE SET challenge_id=excluded.challenge_id,generation=excluded.generation,
                reason=excluded.reason,origin=excluded.origin,integration_id=excluded.integration_id,operation_id=excluded.operation_id,
                request_id=excluded.request_id,artifact_id=NULL,methods=excluded.methods,facts=excluded.facts,updated_at=excluded.updated_at""",
                (challenge.transfer_id, challenge.id, challenge.generation, challenge.reason.value, challenge.origin.value,
                 challenge.integration_id, challenge.operation_id, challenge.request_id, None, _methods_payload(challenge.methods),
                 _facts_payload(challenge.facts), now, now))
            await db.execute("UPDATE torrents SET status='input_required',normalized_error=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (transfer_id,))
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info',?)", (transfer_id, _EVENT_MESSAGES[challenge.reason]))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required',?)", (transfer_id, challenge.reason.value))
            await db.commit()
            return challenge

    async def wait_executor(self, artifact: Artifact, integration_id: str, operation_id: str, requirement: InputRequirement) -> InputChallenge:
        now = float(self.clock())
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.torrent_id,t.status,f.status AS artifact_state,f.execution_attempt_id FROM download_files f
                JOIN torrents t ON t.id=f.torrent_id WHERE f.id=?""", (artifact.id,))
            prestart = bool(row and row["artifact_state"] == "queued" and row["execution_attempt_id"] is None)
            challenged_execution = bool(row and row["artifact_state"] == "error" and row["execution_attempt_id"] == operation_id)
            if not row or row["status"] in SIDE_STATE_RETIRING_TRANSFER_STATES or not (prestart or challenged_execution):
                raise InputSubmissionRejected("Input challenge is no longer applicable")
            identity, generation = await self._next(db, artifact.transfer_id)
            challenge = InputChallenge(identity, artifact.transfer_id, generation, requirement.reason, InputOrigin.EXECUTOR,
                integration_id, operation_id, requirement.methods, request_id=artifact.request_id, artifact_id=artifact.id,
                facts=requirement.facts)
            await db.execute("""INSERT INTO transfer_input_challenges(transfer_id,challenge_id,generation,reason,origin,integration_id,
                operation_id,request_id,artifact_id,methods,facts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transfer_id) DO UPDATE SET challenge_id=excluded.challenge_id,generation=excluded.generation,
                reason=excluded.reason,origin=excluded.origin,integration_id=excluded.integration_id,operation_id=excluded.operation_id,
                request_id=excluded.request_id,artifact_id=excluded.artifact_id,methods=excluded.methods,facts=excluded.facts,
                updated_at=excluded.updated_at""",
                (challenge.transfer_id, challenge.id, challenge.generation, challenge.reason.value, challenge.origin.value,
                 challenge.integration_id, challenge.operation_id, challenge.request_id, challenge.artifact_id,
                 _methods_payload(challenge.methods), _facts_payload(challenge.facts), now, now))
            await db.execute("UPDATE download_files SET status='input_required',normalized_error=NULL WHERE id=?", (artifact.id,))
            await db.execute("UPDATE torrents SET status='input_required',normalized_error=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (artifact.transfer_id,))
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info',?)", (artifact.transfer_id, _EVENT_MESSAGES[challenge.reason]))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required',?)", (artifact.transfer_id, challenge.reason.value))
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
                challenge.integration_id, challenge.operation_id, requirement.methods, challenge.request_id, challenge.artifact_id,
                requirement.facts)
            await db.execute("""UPDATE transfer_input_challenges SET challenge_id=?,generation=?,reason=?,methods=?,facts=?,updated_at=?
                WHERE transfer_id=? AND challenge_id=? AND generation=?""",
                (replacement.id, replacement.generation, replacement.reason.value, _methods_payload(replacement.methods),
                 _facts_payload(replacement.facts), now, challenge.transfer_id, challenge.id, challenge.generation))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'input_required',?)", (challenge.transfer_id, replacement.reason.value))
            await db.commit()
            return replacement

    async def clear(self, challenge: InputChallenge) -> None:
        async with get_db() as db:
            await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=? AND challenge_id=? AND generation=?",
                             (challenge.transfer_id, challenge.id, challenge.generation))
            await db.commit()

    async def record(self, transfer_id: int, kind: str, detail: str = "") -> None:
        """One durable, non-secret authentication fact (``application_events``):
        what happened and to which scope family -- never a value, a URL or a
        username. The Transfer Trace exports it through its own owner."""
        async with get_db() as db:
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                             (int(transfer_id), str(kind), str(detail)))
            await db.commit()

    async def clear_transfer(self, transfer_id: int) -> None:
        async with get_db() as db:
            await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
            await db.commit()
