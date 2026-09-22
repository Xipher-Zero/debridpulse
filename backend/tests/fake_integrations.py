"""Deterministic integrations with a parcel protocol unrelated to debrid APIs."""
from dataclasses import replace
import hashlib
from pathlib import Path

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Domain, NormalizedError, Stage
from transfers.input_required import auth_required, username_password
from transfers.models import (
    ArtifactFingerprint, Capability, CleanupDirective, Endpoint, ExecutionActivity, ExecutionControl,
    ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionSnapshot, ExecutionState,
    ExecutorCapabilities, ExecutorClaim, ExecutorHealth, FileManifest, FileManifestEntry, InputField, InputMethod,
    IntegrationDescriptor, MaterializationKind, MaterializationResult, MaterializedEntry, OutcomeKind,
    Ownership, ProviderObservation, ProviderResource, ResolutionResult, ResourceSnapshot, ResourceState,
    SourceEntry, SourceIdentity, TransferCandidate, TransferOutcome, TransferProgress, TransferRequest,
)


def neutral_facts(observation):
    """This fake's own native->neutral translation: a stored job carries only
    a lifecycle state, so activity and controls are derived from it unless the
    job already states them explicitly."""
    if observation.activity != ExecutionActivity() or observation.controls:
        return observation
    state = observation.state
    live = state in {ExecutionState.QUEUED, ExecutionState.RUNNING, ExecutionState.PAUSED}
    activity = ExecutionActivity(state == ExecutionState.RUNNING, live, state == ExecutionState.RUNNING)
    controls = (frozenset({ExecutionControl.PAUSE}) if state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
                else frozenset({ExecutionControl.RESUME}) if state == ExecutionState.PAUSED else frozenset())
    return replace(observation, activity=activity, controls=controls)


class ParcelProvider:
    def __init__(self, identity="parcel-lab", *, file_manifest=False):
        capabilities = {
            Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.METADATA,
            Capability.INVENTORY, Capability.CLEANUP, Capability.REFRESH,
        }
        # A non-debrid provider that opts in to the neutral early file-manifest
        # capability, so universal file-selection is proven without AllDebrid.
        if file_manifest:
            capabilities.add(Capability.FILE_MANIFEST)
        self.descriptor = IntegrationDescriptor(identity, "Parcel lab", frozenset(capabilities),
            request_types=frozenset({"parcel", "parcel-member"}))
        self.declares_file_manifest = bool(file_manifest)
        self.calls = []
        self.responses = []
        self.resources = {}
        self.members = {}
        self.file_manifests = {}
        self.inventory_items = ()
        self.cleanup_response = TransferOutcome(OutcomeKind.SUCCESS)
        self.entered = None
        self.release = None

    @property
    def applicability(self):
        return ProviderApplicability()

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return TransferCandidate(name, (Endpoint("memory", f"memory:{payload}"),), expected_bytes=4,
                                 provider_id=self.descriptor.id, refresh_request=TransferRequest("parcel-member", payload, name=name))

    def parcel(self, payload="parcel", *, state=ResourceState.PREPARING, ownership=Ownership.CREATED,
               files=None, file_manifest=None):
        resource = ProviderResource(self.descriptor.id, {"box_ticket": payload}, ownership, id=f"{self.descriptor.id}:{payload}")
        tree = file_manifest
        if tree is None and files is not None:
            tree = FileManifest(tuple(
                FileManifestEntry(name, path, size) for name, path, size in files
            ))
        observed = ProviderObservation(resource, state, "Parcel", request=TransferRequest("parcel", payload),
                                       file_manifest=tree if self.declares_file_manifest else None)
        self.resources[resource.id] = observed
        members = (SourceEntry("payload.bin", 4, "folder/payload.bin", TransferRequest("parcel-member", payload)),)
        if files is not None:
            members = tuple(
                SourceEntry(name, size, path, TransferRequest("parcel-member", f"{payload}:{path}", name=name))
                for name, path, size in files
            )
        self.members[resource.id] = members
        if tree is not None:
            self.file_manifests[resource.id] = tree
        return ResolutionResult(state, observation=observed)

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate(request.name or "payload.bin", payload=request.payload),))

    async def observe(self, resource):
        self.calls.append(("observe", resource.id))
        return self.resources.get(resource.id, ProviderObservation(resource, ResourceState.ABSENT))

    async def manifest(self, resource):
        self.calls.append(("manifest", resource.id))
        return self.members.get(resource.id, ())

    async def refresh(self, candidate):
        self.calls.append(("refresh", candidate.id))
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, expires_at=None),))

    async def inventory(self):
        self.calls.append(("inventory", None))
        return ResourceSnapshot(self.inventory_items, complete=False)

    async def cleanup(self, directive: CleanupDirective):
        self.calls.append(("cleanup", directive))
        if self.cleanup_response.kind == OutcomeKind.SUCCESS:
            self.resources.pop(directive.resource.id, None)
        return self.cleanup_response


class MemoryExecutor:
    descriptor = IntegrationDescriptor("memory-copy", "Memory copy", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True)
    # This fake's private applicability fact: the endpoint transports it copies.
    claim_schemes = frozenset({"memory"})

    def __init__(self, authorize):
        self.authorize = authorize
        self.calls = []
        self.jobs = {}
        self.start_errors = []

    def claim(self, subject):
        return ExecutorClaim(any(endpoint.scheme in self.claim_schemes for endpoint in subject.candidate.endpoints))

    @staticmethod
    def sidecar(target):
        """This fake's native resume file beside a planned FILE target."""
        return str(target) + ".memory-progress"

    def footprint(self, work):
        return ExecutionFootprint((self.sidecar(work.materialization.target),))

    def prepare(self, request):
        plan = request.work.materialization
        return ExecutionHandle(self.descriptor.id, request.attempt_id,
                               {"copy_ticket": request.attempt_id, "destination": plan.target, "root": plan.root})

    async def fingerprint(self, subject):
        """Deterministic fake of provider-neutral sampled payload identity."""
        candidate = subject.candidate
        if not candidate.endpoints or candidate.expected_bytes <= 0:
            return None
        endpoint = candidate.endpoints[0]
        return ArtifactFingerprint(candidate.expected_bytes, f"{endpoint.scheme}:{endpoint.address}")

    async def start(self, request, handle):
        assert await self.authorize(handle, "start"), "Core must persist authority before executor contact"
        self.calls.append(("start", handle))
        error = self.start_errors.pop(0) if self.start_errors else None
        result = neutral_facts(ExecutionObservation(handle, ExecutionState.FAILED if error else ExecutionState.RUNNING,
                                                    TransferProgress(4, 1, 1), error))
        self.jobs[handle.attempt_id] = result
        return result

    async def observe(self, handle):
        assert await self.authorize(handle, "observe")
        self.calls.append(("observe", handle))
        observed = neutral_facts(self.jobs.get(handle.attempt_id, ExecutionObservation(handle, ExecutionState.ABSENT)))
        if (observed.state == ExecutionState.SUCCEEDED and observed.materialization is None
                and {"destination", "root"} <= set(handle.correlation)):
            # A FILE copier reports the one planned file it produced.
            observed = replace(observed, materialization=self.file_result(handle))
        return observed

    async def observe_many(self, handles):
        return ExecutionSnapshot(tuple([await self.observe(handle) for handle in handles]))

    async def pause(self, handle):
        assert await self.authorize(handle, "pause")
        current = await self.observe(handle)
        if current.resumable:
            current = replace(current, state=ExecutionState.PAUSED, activity=ExecutionActivity(),
                              controls=frozenset())
            self.jobs[handle.attempt_id] = current
        return neutral_facts(current)

    async def resume(self, handle):
        assert await self.authorize(handle, "resume")
        current = await self.observe(handle)
        if current.resumable:
            current = replace(current, state=ExecutionState.RUNNING, activity=ExecutionActivity(),
                              controls=frozenset())
            self.jobs[handle.attempt_id] = current
        return neutral_facts(current)

    async def cancel(self, handle):
        assert await self.authorize(handle, "cancel")
        self.calls.append(("cancel", handle))
        if handle.attempt_id in self.jobs:
            self.jobs[handle.attempt_id] = replace(self.jobs[handle.attempt_id], state=ExecutionState.CANCELLED,
                                                   activity=ExecutionActivity(), controls=frozenset())
        # The acknowledgement is not the answer: report what is observed now.
        return await self.observe(handle)

    async def health(self):
        return ExecutorHealth(True, True)

    @staticmethod
    def file_result(handle, size=None):
        """The FILE materialization this fake reports for its planned target."""
        relative = Path(handle.correlation["destination"]).relative_to(handle.correlation["root"]).as_posix()
        return MaterializationResult(MaterializationKind.FILE, (MaterializedEntry(relative, size),))

    def finish(self, handle, *, materialize=True):
        current = self.jobs[handle.attempt_id]
        handle = current.handle
        target = Path(handle.correlation["destination"])
        if materialize:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"done")
        self.jobs[handle.attempt_id] = replace(
            current, state=ExecutionState.SUCCEEDED, progress=TransferProgress(4, 4), activity=ExecutionActivity(),
            controls=frozenset(), materialization=self.file_result(handle, 4))


class TransientInputExecutor(MemoryExecutor):
    """A memory copier participating in the one INPUT_REQUIRED lifecycle
    (``transient_input``). Subclasses override whichever entry points they
    ask through; the defaults ask for nothing more."""

    capabilities = replace(MemoryExecutor.capabilities, transient_input=True)

    def prepare_with_input(self, request, submitted):
        return self.prepare(request)

    def input_requirement(self, candidate, observation):
        return None

    async def start_with_input(self, request, handle, submitted):
        return await self.start(request, handle)

    async def fingerprint_with_input(self, subject, submitted):
        return await self.fingerprint(subject)


class VaultProvider:
    """Resolution-only provider over an unrelated ``vault`` transport.

    Payloads are ``<host>/<object>`` (``|``-separated alternatives resolve to
    several candidates of one request); every candidate advertises neutral
    transient username/password input, exactly like a direct-source provider,
    and carries no authoritative identity, so equivalence needs sampled evidence.
    """

    def __init__(self, identity="vault-lab"):
        self.descriptor = IntegrationDescriptor(identity, "Vault lab", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"vault"}))

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        candidates = []
        for alternative in str(request.payload).split("|"):
            host, _, item = alternative.partition("/")
            candidates.append(TransferCandidate(
                request.name or item, (Endpoint("vault", f"vault://{alternative}"),),
                provider_id=self.descriptor.id, source_identity=SourceIdentity("host", host),
                accepted_input_methods=(InputMethod.USERNAME_PASSWORD,),
            ))
        return ResolutionResult(ResourceState.AVAILABLE, tuple(candidates))


class VaultExecutor(MemoryExecutor):
    """A non-aria2 executor whose CandidateSampling may require operator input.

    ``objects`` maps ``<host>/<object>`` to bytes; ``locks`` maps a host to the
    one username/password it accepts. Sampling a locked host without input, or
    with the wrong input, reports the neutral AUTH_REQUIRED requirement; a
    locked host's native start without input fails with a definitive native
    auth diagnostic that ``input_requirement`` turns into an execution challenge.
    """

    descriptor = IntegrationDescriptor("vault-copy", "Vault copy", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True)
    claim_schemes = frozenset({"vault"})

    def __init__(self, authorize, *, objects=None, locks=None):
        super().__init__(authorize)
        self.objects = dict(objects or {})
        self.locks = dict(locks or {})
        self.samples = []
        self.input_starts = []

    @staticmethod
    def _object(candidate):
        return candidate.endpoints[0].address.removeprefix("vault://")

    def prepare_with_input(self, request, submitted):
        return self.prepare(request)

    def _evidence(self, key):
        content = self.objects[key]
        return ArtifactFingerprint(len(content), hashlib.sha256(content).hexdigest())

    async def fingerprint(self, subject):
        candidate = subject.candidate
        key = self._object(candidate)
        self.samples.append((str(candidate.id), None))
        if key.partition("/")[0] in self.locks:
            return auth_required(username_password())
        return self._evidence(key)

    async def fingerprint_with_input(self, subject, submitted):
        candidate = subject.candidate
        key = self._object(candidate)
        self.samples.append((str(candidate.id), submitted.value(InputField.USERNAME)))
        expected = self.locks.get(key.partition("/")[0])
        if expected is None or (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) != expected:
            return auth_required(username_password())
        return self._evidence(key)

    async def start(self, request, handle):
        if self._object(request.work.subject.candidate).partition("/")[0] in self.locks and not self._unlocked:
            self.start_errors = [NormalizedError(Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION,
                                                 native_code="vault-auth")]
        return await super().start(request, handle)

    _unlocked = False

    def input_requirement(self, candidate, observation):
        if (observation.state == ExecutionState.FAILED and observation.error is not None
                and observation.error.native_code == "vault-auth"):
            return auth_required(username_password())
        return None

    async def start_with_input(self, request, handle, submitted):
        # Only the evidence handoff reaches a freshly prepared attempt here.
        assert await self.authorize(handle, "start")
        self.input_starts.append((str(request.work.subject.candidate.id), submitted.value(InputField.USERNAME)))
        self._unlocked = True
        try:
            return await self.start(request, handle)
        finally:
            self._unlocked = False
