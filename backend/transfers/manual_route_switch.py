"""Provider-neutral operator replacement of one torrent root's provider route,
and the one read model of the providers that route can move to.

A root's provider is its committed root route (``bound_route_provider``) --
the route routing, promotion and failover all read -- never an origin, a child
candidate or a provider resource. An operator choosing another provider for a
root replaces that ROOT route; its members are never rewritten one by one. The
ordinary machinery then does the rest: the target's prepared backup is taken
over by the one promotion seam or the target resolves cold, a new decomposition
generation opens on the new binding (selection carried or offered by its one
owner, continuity proven or held), and members are rebuilt in place.

The replacement composes existing owners only, in an order that never leaves
an old writer alive under a new route and never leaves the root ownerless:

1. preflight: every refusal knowable without disturbing the transfer --
   stale, the root's own state (``root_route_replacement_refusal``, the one
   owner ``replace_root_route`` also decides through), target not selectable
   in the one read model (``route_providers``: not a legitimate, entitled,
   enabled claimant, or a backup still preparing), and TASK3d-1 admission of
   the target as PRIMARY work (``_make_primary_room``) unless that read model
   calls it ``prepared`` -- nothing is fenced, paused or retired on a refusal;
2. fence: the durable pause intent every admission reads, fencing exactly
   the recovery claims an older owner still holds
   (``set_pause_and_fence(claimed_only=True)``) -- never a per-member fence
   -- then the one writer retirement for each live writer (quiesce,
   checkpoint, fence), each detached once proven stopped. A writer that
   already succeeded is delivered through the canonical execution processing
   instead: its material is the old generation's finished work;
3. one transaction (``TransferRepository.replace_root_route``) that commits
   the replacement only while the route is still the one the operator saw and
   no writer is live: a newer route or a live writer changes nothing;
4. lift the fence this replacement set -- the intent alone, never an
   operator Resume: no recovery sweep over the decomposition. It sets one
   only when no operator pause (per transfer or Pause All) already fences
   the transfer, and it runs inside the application's one operator
   pause-control boundary (``ApplicationService.operator_controls``), so no
   Pause or Pause All can land in between and lifting it can never clear an
   operator's intent.

A refusal after the fence lifts it: the old route, binding and generation are
untouched, and the retired writers' members are queued again for the old
route's ordinary dispatch from their checkpointed material. A crash before the
commit leaves the transfer paused on its old route (Resume restores it); after
the commit, the new route is durable.
"""
from __future__ import annotations

from transfers.applicability import ApplicabilityUnresolved
from transfers.candidate_activation import retire_writer
from transfers.contracts import UpstreamSelection
from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Retryability,
    Stage,
    TransferError,
)
from transfers.models import BITTORRENT_REQUEST_KINDS, ResourceState
from transfers.registry import RoutingDisposition

# Provider status vocabulary of the one read model.
CURRENT, PREPARED, PREPARING, DEFERRED, AVAILABLE, FAILED_EARLIER, UNAVAILABLE = (
    "current", "prepared", "preparing", "deferred", "available", "failed_earlier", "unavailable")
_UNAVAILABLE_REASONS = {
    RoutingDisposition.DISABLED: "disabled",
    RoutingDisposition.NOT_ENTITLED: "not_entitled",
    RoutingDisposition.ENTITLEMENT_UNRESOLVED: "entitlement_unknown",
    RoutingDisposition.UNHEALTHY: "unhealthy",
    RoutingDisposition.DECLINED: "declined",
    RoutingDisposition.APPLICABILITY_UNRESOLVED: "applicability_unknown",
    RoutingDisposition.HELD_BY_SPECIALIZED_AUTHORITY: "held",
}


def _refusal(category: Category, *, domain: Domain = Domain.LIFECYCLE,
             retryability: Retryability = Retryability.NEVER, integration_id: str = "") -> TransferError:
    return TransferError(NormalizedError(domain, category, Stage.RESOLUTION, retryability=retryability,
                                         origin=Origin.CORE, operator_action_required=True,
                                         integration_id=integration_id))


def provider_choices(registry, facts: dict) -> list[dict]:
    """THE selectability of one torrent root's route, from its route facts
    (``TransferRepository._root_route_facts``) and the canonical competition
    alone -- pure: no I/O, no provider call. Every provider taking part in the
    root's competition gets ONE entry:

    * ``current`` -- it owns the committed root route;
    * ``failed_earlier`` -- a failure of its own ended its route of this root
      (exhausted, or backing off until it may re-enter), but it is still a
      legitimate claimant: an explicit choice retries it;
    * ``available`` -- a legitimate claimant (applicable, entitled, enabled,
      healthy): an operator switch may choose it;
    * ``unavailable`` with a bounded ``reason`` -- disabled, not entitled,
      unknown entitlement, unhealthy, declined, unresolved applicability or
      held by specialized authority: never selectable.

    A provider that cannot claim the root is not listed. Exhaustion is not
    applied to the competition here, so an exhausted claimant stays visible;
    which providers are legitimate is otherwise exactly what routing decides.
    The picker, the switch's own validation and the list's
    ``route_switch_available`` all read this one answer."""
    try:
        decision = registry.provider_route(facts["resolvable"], declined=facts["declined"], acquisition=True,
                                           generic_closed=facts["generic_closed"]).decision
    except ApplicabilityUnresolved:
        decision = None
    current, entries, seen = facts["current"], [], set()
    for disposition in (decision.providers if decision else ()):
        provider_id = disposition.provider_id
        if disposition.disposition == RoutingDisposition.NOT_APPLICABLE or provider_id in seen:
            continue
        seen.add(provider_id)
        entry = {"provider_id": provider_id, "reason": None,
                 "readiness": disposition.availability.value if disposition.availability else None}
        legitimate = disposition.disposition in {RoutingDisposition.SELECTED,
                                                 RoutingDisposition.APPLICABLE_NOT_SELECTED,
                                                 RoutingDisposition.EXHAUSTED}
        if provider_id == current:
            entry.update(status=CURRENT, selectable=False)
        elif legitimate:
            entry.update(status=FAILED_EARLIER if provider_id in facts["exhausted"] else AVAILABLE, selectable=True)
        else:
            entry.update(status=UNAVAILABLE, selectable=False,
                         reason=_UNAVAILABLE_REASONS.get(disposition.disposition, "unavailable"))
        entries.append(entry)
    if current and current not in seen:
        entries.insert(0, {"provider_id": current, "status": CURRENT, "selectable": False, "reason": None,
                           "readiness": None})
    return entries


def torrent_root(facts: dict | None) -> bool:
    """Whether route facts describe the one root a provider switch applies
    to: a transfer of exactly one root request (``_root_route_facts`` only
    reports those) of a BitTorrent request kind."""
    return bool(facts) and facts["kind"] in BITTORRENT_REQUEST_KINDS


def root_actionable(facts: dict) -> bool:
    """Whether the root's transfer still has work a route could do: not in a
    terminal lifecycle state (the route facts' ``terminal``, from
    ``policy.TERMINAL_TRANSFER_STATES``). A finished, cancelled or deleted
    transfer keeps its provider as history, but no provider can be switched
    to -- the switch itself refuses such a root
    (``root_route_replacement_refusal``: ``gone``), so nothing offers it."""
    return not facts["terminal"]


def switch_available(registry, facts: dict | None) -> bool:
    """Whether the root is actionable (``root_actionable``) and at least one
    provider other than the current one is legitimately selectable for it now
    (``provider_choices``)."""
    return torrent_root(facts) and bool(facts["current"]) and root_actionable(facts) and any(
        entry["selectable"] for entry in provider_choices(registry, facts))


def standby_choice(standby: dict, *, upstream_selection: bool = False) -> tuple[str, bool]:
    """What one TASK3 backup of the root makes an otherwise available target:
    ``(status, selectable)``.

    ``prepared`` says provider-side preparation is complete and the backup
    is eligible for immediate promotion: it is bound, its resource was last
    observed AVAILABLE, and no route of that provider on the root has failed
    since (it promises nothing about the candidates the provider later
    issues). A backup whose provider is still acquiring the content (a claim
    being created, or a resource still PREPARING) is ``preparing`` and not
    selectable: switching to it would leave productive work for an
    indefinite remote wait. A backup the provider itself contradicts (its
    route on the root failed after the resource was last seen available, or
    the resource is in no usable state), or a preparation that itself
    ended in failure, ``failed_earlier``: an explicit retry only. A resource
    known gone holds nothing, and a deferred claim holds no resource: the
    target resolves cold, as ``available`` / ``deferred``.

    A backup of a provider that executes only what is selected on its own
    resource (``upstream_selection``) is never selected while it is a
    backup, so PREPARING is no acquisition in progress there: it waits for
    the root's selection, which taking it over synchronizes. It is the
    ``available`` target it would be cold -- never ``prepared``, which it is
    not."""
    state, resource = standby["state"], standby.get("resource_state")
    if state == "bound" and resource == ResourceState.PREPARING.value and upstream_selection:
        return AVAILABLE, True
    if state == "creating" or (state == "bound" and resource == ResourceState.PREPARING.value):
        return PREPARING, False
    if state == "deferred":
        return DEFERRED, True
    if state == "failed":
        return FAILED_EARLIER, True
    if state != "bound" or resource in {ResourceState.ABSENT.value, ResourceState.EXPIRED.value}:
        return AVAILABLE, True
    if resource == ResourceState.AVAILABLE.value and not standby.get("contradicted"):
        return PREPARED, True
    return FAILED_EARLIER, True


async def route_providers(engine, transfer_id: int) -> dict | None:
    """THE provider status of one torrent root's route, provider-neutral:
    ``provider_choices``, with each legitimate claimant's TASK3 backup of this
    root deciding its status and selectability (``standby_choice``; never its
    cache readiness, which stays the secondary ``readiness`` fact). The picker
    and the switch's own preflight both read this one answer (the bounded
    list's launcher reads ``switch_available``, which no backup narrows: a
    root whose only alternative is still preparing opens a picker that says
    so). A root that is not actionable (``root_actionable``) is never
    ``switchable``; its providers' statuses stay as the history they are.
    ``None`` for a transfer that is not one torrent root."""
    facts = await engine.repository.root_route_facts(int(transfer_id))
    if not torrent_root(facts):
        return None
    standbys = {item["provider_id"]: item for item in await engine.repository.standbys(int(transfer_id))
                if item["request_id"] == facts["root_id"] and item.get("promoted_at") is None}
    providers = provider_choices(engine.registry, facts)
    for entry in providers:
        standby = standbys.get(entry["provider_id"])
        if entry["status"] == AVAILABLE and standby:
            entry["status"], entry["selectable"] = standby_choice(standby, upstream_selection=isinstance(
                engine.registry.providers.get(entry["provider_id"]), UpstreamSelection))
    return {"transfer_id": int(transfer_id), "current_provider_id": facts["current"], "providers": providers,
            "switchable": root_actionable(facts) and any(entry["selectable"] for entry in providers)}


async def switch_root_provider(engine, transfer_id: int, provider_id: str, *, expected_provider_id: str) -> dict:
    """Replace one torrent root's provider route with ``provider_id`` (see the
    module docstring). ``expected_provider_id`` is the provider the operator
    saw as current: if the committed route moved since, nothing changes.
    Raises a normalized ``TransferError`` for every refusal; the requested
    provider is never silently replaced by another."""
    transfer = await engine.repository.get(int(transfer_id))
    if transfer is None:
        raise KeyError(transfer_id)
    facts = await engine.repository.root_route_facts(int(transfer_id))
    if not torrent_root(facts):
        raise _refusal(Category.UNSUPPORTED_REQUEST, domain=Domain.REQUEST)
    root = next(item for item in await engine.repository.requests(int(transfer_id)) if item.id == facts["root_id"])
    current = facts["current"]
    if current != expected_provider_id:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    if provider_id == current:
        raise _refusal(Category.INVALID_REQUEST, domain=Domain.REQUEST)
    target = engine.registry.providers.get(str(provider_id))
    choice = next((entry for entry in (await route_providers(engine, int(transfer_id)))["providers"]
                   if entry["provider_id"] == provider_id), None)
    if target is None or choice is None or not choice["selectable"]:
        if choice is not None and choice["status"] == PREPARING:
            # Its provider is still acquiring the content: the productive
            # route stays exactly as it is.
            raise _refusal(Category.RESOURCE_STATE_CONFLICT, domain=Domain.PROVIDER,
                           retryability=Retryability.BACKOFF, integration_id=str(provider_id))
        if choice is not None and choice["reason"] in {"not_entitled", "entitlement_unknown"}:
            raise _refusal(Category.ACCOUNT_LIMITED, domain=Domain.PROVIDER, retryability=Retryability.BACKOFF,
                           integration_id=str(provider_id))
        raise _refusal(Category.PROVIDER_UNAVAILABLE, domain=Domain.PROVIDER, integration_id=str(provider_id))
    repository = engine.repository
    latest = await repository.latest_root_route(root.id)
    if not latest or latest["provider_id"] != current:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    refusal = await repository.root_route_replacement_refusal(
        root.id, expected_attempt_id=str(latest["id"]), expected_provider_id=str(current))
    if refusal:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT,
                       retryability=Retryability.BACKOFF if refusal == "busy" else Retryability.IMMEDIATE)
    # The target is PRIMARY work: TASK3d-1 admission, never a bypass -- unless
    # it holds this root's prepared backup that the one promotion seam takes
    # over without a new slot (exactly the order ``_resolve`` uses). Admission
    # is the last preflight: it may give back a backup to make room.
    prepared = choice["status"] == PREPARED
    if not prepared and not await engine._make_primary_room(root, target):
        raise _refusal(Category.CONCURRENCY_LIMITED, domain=Domain.PROVIDER, retryability=Retryability.BACKOFF,
                       integration_id=str(provider_id))

    # The fence is a temporary pause intent this replacement owns -- only when
    # no operator pause (per transfer or global) already fences it, so lifting
    # it can never clear an operator's intent. The caller runs this inside
    # the application's one operator pause-control boundary, so no Pause or
    # Pause All can land between setting and lifting it.
    paused_here = not transfer.paused and not await repository.globally_paused()
    if paused_here:
        async with engine._dispatch_lock:
            await repository.set_pause_and_fence(int(transfer_id), True, claimed_only=True)
    try:
        for artifact in await repository.artifacts(int(transfer_id)):
            if artifact.execution is None or artifact.state == "completed":
                continue
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            retired = await retire_writer(engine, artifact, candidate, artifact, candidate,
                                          boundary="operator_route_switch", park=False)
            if retired.reason == "writer_already_succeeded":
                # Finished, not live: delivered (or rejected) by the canonical
                # execution processing while its generation still governs.
                current_artifact = await engine._current_artifact(int(transfer_id), artifact.id)
                if current_artifact is not None:
                    await engine._process_executions(int(transfer_id), (current_artifact,), {})
                    await engine._verify_pending()
                continue
            if retired.reason:
                raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.BACKOFF)
            await repository.detach_retired_writer(artifact.id, artifact.execution.attempt_id,
                                                   state="queued" if paused_here else "paused")
        outcome = await repository.replace_root_route(
            root.id, expected_attempt_id=str(latest["id"]), expected_provider_id=str(current),
            target_provider_id=str(provider_id))
        if outcome != "replaced":
            raise _refusal(Category.RESOURCE_STATE_CONFLICT,
                           retryability=Retryability.BACKOFF if outcome in {"busy", "writer_live"}
                           else Retryability.IMMEDIATE)
    finally:
        if paused_here:
            async with engine._dispatch_lock:
                await repository.set_pause_and_fence(int(transfer_id), False, claimed_only=True)
            # A writer the ordinary Pause retirement stopped meanwhile was
            # detached paused; with the fence lifted it is queued again.
            for artifact in await repository.artifacts(int(transfer_id)):
                if artifact.state == "paused" and artifact.execution is None:
                    await repository.artifact_state(artifact.id, "queued")
            await engine._aggregate(int(transfer_id))
    await engine._cleanup_pending()
    engine._resolution_opportunity(int(transfer_id))
    return {"transfer_id": int(transfer_id), "provider_id": str(provider_id), "previous_provider_id": current}
