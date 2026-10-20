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
   no writer is live: a newer route or a live writer changes nothing. In the
   same commit the replaced provider's resource is kept as its backup of the
   root when that provider's Prepare Backup Torrents allows it
   (``TransferEngine._retains_replaced_route``), and otherwise given back
   through the one cleanup owner -- losing the route alone never gives it back;
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

Any OTHER root -- one of several links of a transfer, or the one link of a
transfer that is not a torrent -- is switched per root, never all roots of a
provider at once, and only a root that matters now: one that produced the
currently SELECTED candidate of at least one unfinished artifact
(``TransferRepository._artifact_route_roots``; a root supplying only
alternates is not offered). Its alternatives are exactly the providers
``provider_choices`` finds for that root's own request -- a provider reaching
the same content only through another host is the separate candidate switch.
A root decomposed into members is not switched this way in 1.0.13 (offer and
switch both refuse it). The same order holds, with the fence narrowed to what
the root affects:

1. preflight exactly as above, through ``root_providers``;
2. an exclusive ``USER_ROUTE_SWITCH`` recovery claim on each artifact whose
   selected candidate this root alone produced -- any claim unavailable
   refuses, changing nothing -- and the one writer retirement under each.
   Artifacts selecting another root keep their writers, selection and
   provenance;
3. the same ``replace_root_route`` commit, re-verifying those claims and that
   none of those artifacts has a live writer;
4. the claims are finished. Until the root resolves on its new route the
   affected artifacts stay HELD by a rule derived from stored facts
   (``_route_replacement_admission``: every origin of the selected candidate
   belongs to a released route), so a restart in between changes nothing;
   when it resolves, its candidates take the replaced ones' place and become
   selected only where the replaced root's candidate was
   (``CanonicalOwnership.replace_root_candidates``).

Automatic readiness promotion stays a torrent root's alone: ``route_providers``
answers ``None`` for every other transfer.
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
from transfers.recovery_execution import RecoveryTrigger
from transfers.registry import RoutingDisposition

# Provider status vocabulary of the one read model. The picker words them
# Current, Ready (``prepared``), Preparing, Waiting for selection, Deferred,
# Not checked (``available``: a legitimate claimant nothing has checked for
# this root), Failed earlier and Unavailable.
CURRENT, PREPARED, PREPARING, DEFERRED, AVAILABLE, FAILED_EARLIER, UNAVAILABLE = (
    "current", "prepared", "preparing", "deferred", "available", "failed_earlier", "unavailable")
WAITING_FOR_SELECTION = "waiting_for_selection"
# A deferred backup whose preparation failed recoverably and waits for its
# retry -- worded "Retrying…", never "Deferred": the backend keeps its real
# ``deferred`` state and retry schedule. A deferral for a provider "not now"
# (rate limit, concurrency, quota) or with no failure at all stays DEFERRED.
RETRYING = "retrying"
_DEFERRAL_NOT_A_FAILURE = frozenset({Category.RATE_LIMITED, Category.CONCURRENCY_LIMITED, Category.QUOTA_EXCEEDED})
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


def provider_choices(registry, facts: dict, memo: dict | None = None) -> list[dict]:
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
    ``route_switch_available`` all read this one answer.

    ``memo`` (one bounded read, e.g. a list page): roots routed from the same
    facts share one routing decision instead of asking the registry again."""
    key = (facts["resolvable"], facts["declined"], facts["generic_closed"])
    try:
        known = memo is not None and key in memo
    except TypeError:
        memo, known = None, False
    if known:
        decision = memo[key]
    else:
        try:
            decision = registry.provider_route(facts["resolvable"], declined=facts["declined"], acquisition=True,
                                               generic_closed=facts["generic_closed"]).decision
        except ApplicabilityUnresolved:
            decision = None
        if memo is not None:
            memo[key] = decision
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


def manual_root(facts: dict, *, single: bool) -> bool:
    """Whether an operator may switch this root's provider at all: the one
    root of a torrent transfer (``single``), or any other root that is not
    decomposed into members and that produced the selected candidate of at
    least one unfinished artifact (``relevant``). A root supplying only
    alternates, a decomposed root of anything but a single torrent, and a
    root whose candidate another transfer's artifact holds (``shared``: the
    switch fences only its own transfer's artifacts), is never a switch
    subject -- for the offer and the switch alike."""
    if single and torrent_root(facts):
        return True
    return not facts["decomposed"] and facts["relevant"] and not facts.get("shared")


def root_switch_available(registry, facts: dict | None, memo: dict | None = None) -> bool:
    """Whether the root has a committed route, is actionable
    (``root_actionable``) and at least one provider other than the current
    one is legitimately selectable for it now (``provider_choices``)."""
    return bool(facts) and bool(facts["current"]) and root_actionable(facts) and any(
        entry["selectable"] for entry in provider_choices(registry, facts, memo))


def switch_available(registry, facts: dict | None) -> bool:
    """``root_switch_available`` for the one root of a torrent transfer."""
    return torrent_root(facts) and root_switch_available(registry, facts)


def root_switches(registry, roots: dict[str, dict], memo: dict | None = None) -> dict[str, bool]:
    """For every root of one transfer that is not its single torrent root
    (``_root_route_facts_by_root``): whether a provider switch of it is
    offered now -- a switch subject (``manual_root``) with a selectable
    alternative (``root_switch_available``). Details' per-file affordance and
    the bounded list's hint read this one answer."""
    single = len(roots) == 1
    return {root_id: manual_root(facts, single=single) and root_switch_available(registry, facts, memo)
            for root_id, facts in roots.items() if not (single and torrent_root(facts))}


def route_switch_offer(registry, roots: dict[str, dict], memo: dict | None = None) -> str | None:
    """The bounded list's launcher for a transfer that is not one torrent
    root: ``"root"`` when its one root is switchable now (the launcher opens
    that root's picker), ``"artifacts"`` when at least one of its several
    roots is (the launcher opens Details, where each file names the root that
    produced it; it never switches every root of a provider), else ``None``."""
    if not any(root_switches(registry, roots, memo).values()):
        return None
    return "root" if len(roots) == 1 else "artifacts"


def standby_choice(standby: dict, *, upstream_selection: bool = False,
                   selection_established: bool = False) -> tuple[str, bool]:
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
    resource (``upstream_selection``) is handed the root's selection only
    once that selection is established (``selection_established``;
    ``TransferRepository.established_selection``). Until then its PREPARING
    is no acquisition in progress: it is ``waiting_for_selection``. Once
    established, it is ``preparing``. Either way it stays the selectable
    target it always was: taking it over synchronizes (or verifies) exactly
    the selection the route then has, and the one torrent is adopted, never
    created again."""
    state, resource = standby["state"], standby.get("resource_state")
    if state == "bound" and resource == ResourceState.PREPARING.value and upstream_selection:
        return (PREPARING if selection_established else WAITING_FOR_SELECTION), True
    if state == "creating" or (state == "bound" and resource == ResourceState.PREPARING.value):
        return PREPARING, False
    if state == "deferred":
        error = standby.get("error")
        if (error is not None and error.category not in _DEFERRAL_NOT_A_FAILURE
                and error.retryability == Retryability.BACKOFF):
            return RETRYING, True
        return DEFERRED, True
    if state == "failed":
        return FAILED_EARLIER, True
    if state != "bound" or resource in {ResourceState.ABSENT.value, ResourceState.EXPIRED.value}:
        return AVAILABLE, True
    if resource == ResourceState.AVAILABLE.value and not standby.get("contradicted"):
        return PREPARED, True
    return FAILED_EARLIER, True


async def _providers(engine, transfer_id: int, facts: dict) -> list[dict]:
    """``provider_choices`` for one root, with each legitimate claimant's
    TASK3 backup of this root deciding its status and selectability
    (``standby_choice``; never its cache readiness, which stays the secondary
    ``readiness`` fact)."""
    standbys = {item["provider_id"]: item for item in await engine.repository.standbys(int(transfer_id))
                if item["request_id"] == facts["root_id"] and item.get("promoted_at") is None}
    providers = provider_choices(engine.registry, facts)
    record = None
    for entry in providers:
        standby = standbys.get(entry["provider_id"])
        if entry["status"] == AVAILABLE and standby:
            upstream = isinstance(engine.registry.providers.get(entry["provider_id"]), UpstreamSelection)
            established = False
            if upstream:
                record = record or next((item for item in await engine.repository.requests(int(transfer_id))
                                         if item.id == facts["root_id"]), None)
                established = record is not None and (await engine.repository.established_selection(
                    record, manifest_entries=standby.get("manifest_entries"))) is not None
            entry["status"], entry["selectable"] = standby_choice(
                standby, upstream_selection=upstream, selection_established=established)
            if entry["status"] == RETRYING:
                # When it is next tried, and the failure's normalized wording.
                entry["retry_in_seconds"] = max(0, int(round(float(standby.get("retry_at") or 0)
                                                             - engine.clock())))
                entry["detail"] = standby["error"].message
    return providers


async def route_providers(engine, transfer_id: int) -> dict | None:
    """THE provider status of one torrent root's route, provider-neutral:
    ``provider_choices`` with the root's backups overlaid (``_providers``).
    The picker and the switch's own preflight both read this one answer (the
    bounded list's launcher reads ``switch_available``, which no backup
    narrows: a root whose only alternative is still preparing opens a picker
    that says so). A root that is not actionable (``root_actionable``) is
    never ``switchable``; its providers' statuses stay as the history they
    are. ``None`` for a transfer that is not one torrent root -- automatic
    readiness promotion reads this, and stays a torrent root's alone."""
    facts = await engine.repository.root_route_facts(int(transfer_id))
    if not torrent_root(facts):
        return None
    providers = await _providers(engine, transfer_id, facts)
    return {"transfer_id": int(transfer_id), "current_provider_id": facts["current"], "providers": providers,
            "switchable": root_actionable(facts) and any(entry["selectable"] for entry in providers)}


async def root_providers(engine, transfer_id: int, root_id: str) -> dict | None:
    """``route_providers``' read model for ONE root of a transfer that is not
    one torrent root, naming that ``root_id``: ``switchable`` only for a
    switch subject (``manual_root``) that is actionable with a selectable
    alternative. ``None`` when the transfer has no such root."""
    roots = await engine.repository.root_route_facts_by_root(int(transfer_id))
    facts = roots.get(str(root_id))
    if facts is None:
        return None
    providers = await _providers(engine, transfer_id, facts)
    return {"transfer_id": int(transfer_id), "root_id": facts["root_id"], "current_provider_id": facts["current"],
            "providers": providers,
            "switchable": manual_root(facts, single=len(roots) == 1) and root_actionable(facts)
            and any(entry["selectable"] for entry in providers)}


def subject_root(roots: dict[str, dict], artifact_id: int | None) -> str | None:
    """The root a switch request names: the root that alone produced the
    selected candidate of ``artifact_id`` (``facts["artifacts"]``), or, with
    no artifact, the transfer's only root. ``None`` when the artifact is not
    attributable to one root now, or a transfer of several roots names none."""
    if artifact_id is None:
        return next(iter(roots)) if len(roots) == 1 else None
    return next((root_id for root_id, facts in roots.items() if int(artifact_id) in facts["artifacts"]), None)


async def route_status(engine, transfer_id: int, artifact_id: int | None = None) -> dict | None:
    """The provider status a picker opens on: the torrent root's
    (``route_providers``) for a torrent transfer, otherwise the root
    ``subject_root`` names (``root_providers``)."""
    status = await route_providers(engine, int(transfer_id))
    if status is not None:
        return status
    root_id = subject_root(await engine.repository.root_route_facts_by_root(int(transfer_id)), artifact_id)
    return await root_providers(engine, int(transfer_id), root_id) if root_id is not None else None


def _unselectable(choice: dict | None, provider_id: str) -> TransferError:
    """Why the requested provider cannot take the root now."""
    if choice is not None and choice["status"] == PREPARING:
        # Its provider is still acquiring the content: the productive
        # route stays exactly as it is.
        return _refusal(Category.RESOURCE_STATE_CONFLICT, domain=Domain.PROVIDER,
                        retryability=Retryability.BACKOFF, integration_id=str(provider_id))
    if choice is not None and choice["reason"] in {"not_entitled", "entitlement_unknown"}:
        return _refusal(Category.ACCOUNT_LIMITED, domain=Domain.PROVIDER, retryability=Retryability.BACKOFF,
                        integration_id=str(provider_id))
    return _refusal(Category.PROVIDER_UNAVAILABLE, domain=Domain.PROVIDER, integration_id=str(provider_id))


async def _preflight(engine, root, current: str, choice: dict, target, provider_id: str) -> dict:
    """The refusals knowable without disturbing anything, in order: the
    root's own state (``root_route_replacement_refusal``), then TASK3d-1
    admission of the target as PRIMARY work -- unless it holds this root's
    prepared backup that the one promotion seam takes over without a new
    slot (exactly the order ``_resolve`` uses). Admission is the last
    preflight: it may give back a backup to make room. Returns the root's
    latest route attempt the commit is decided against."""
    repository = engine.repository
    latest = await repository.latest_root_route(root.id)
    if not latest or latest["provider_id"] != current:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    refusal = await repository.root_route_replacement_refusal(
        root.id, expected_attempt_id=str(latest["id"]), expected_provider_id=str(current))
    if refusal:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT,
                       retryability=Retryability.BACKOFF if refusal == "busy" else Retryability.IMMEDIATE)
    prepared = choice["status"] == PREPARED
    if not prepared and not await engine._make_primary_room(root, target):
        raise _refusal(Category.CONCURRENCY_LIMITED, domain=Domain.PROVIDER, retryability=Retryability.BACKOFF,
                       integration_id=str(provider_id))
    return latest


async def _retire_for_switch(engine, transfer_id: int, artifact, *, queued: bool) -> None:
    """The one writer retirement of a route switch for one artifact: a live
    writer is quiesced, checkpointed and fenced, then detached (``queued``, or
    ``paused`` under an operator pause); a writer that already succeeded is
    delivered (or rejected) by the canonical execution processing while its
    generation still governs. A writer that could not be proven stopped
    refuses the switch."""
    candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
    retired = await retire_writer(engine, artifact, candidate, artifact, candidate,
                                  boundary="operator_route_switch", park=False)
    if retired.reason == "writer_already_succeeded":
        current_artifact = await engine._current_artifact(int(transfer_id), artifact.id)
        if current_artifact is not None:
            await engine._process_executions(int(transfer_id), (current_artifact,), {})
            await engine._verify_pending()
        return
    if retired.reason:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.BACKOFF)
    await engine.repository.detach_retired_writer(artifact.id, artifact.execution.attempt_id,
                                                  state="queued" if queued else "paused")


async def switch_root_provider(engine, transfer_id: int, provider_id: str, *, expected_provider_id: str,
                               expected_demand=None, artifact_id: int | None = None,
                               expected_root_id: str | None = None) -> dict:
    """Replace one root's provider route with ``provider_id`` (see the module
    docstring). ``expected_provider_id`` is the provider the operator saw as
    current: if the committed route moved since, nothing changes. The root is
    the torrent transfer's one root, the transfer's only root, or -- required
    for a transfer of several roots -- the root that alone produced the
    selected candidate of ``artifact_id``, re-derived here; when it is not
    ``expected_root_id`` (the root the operator saw) nothing changes. Raises a
    normalized ``TransferError`` for every refusal; the requested provider is
    never silently replaced by another."""
    transfer = await engine.repository.get(int(transfer_id))
    if transfer is None:
        raise KeyError(transfer_id)
    roots = await engine.repository.root_route_facts_by_root(int(transfer_id))
    root_id = subject_root(roots, artifact_id)
    if root_id is None:
        raise _refusal(Category.UNSUPPORTED_REQUEST if artifact_id is None else Category.RESOURCE_STATE_CONFLICT,
                       domain=Domain.REQUEST if artifact_id is None else Domain.LIFECYCLE,
                       retryability=Retryability.NEVER if artifact_id is None else Retryability.IMMEDIATE)
    if expected_root_id is not None and str(expected_root_id) != root_id:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    if len(roots) == 1 and torrent_root(roots[root_id]):
        return await _switch_torrent_root(engine, transfer, provider_id, expected_provider_id=expected_provider_id,
                                          expected_demand=expected_demand)
    return await _switch_root(engine, transfer, roots, root_id, provider_id,
                              expected_provider_id=expected_provider_id, expected_demand=expected_demand)


async def _switch_torrent_root(engine, transfer, provider_id: str, *, expected_provider_id: str,
                               expected_demand=None) -> dict:
    """The torrent root's replacement, fenced by the transfer's pause intent."""
    transfer_id = transfer.id
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
        raise _unselectable(choice, provider_id)
    repository = engine.repository
    latest = await _preflight(engine, root, current, choice, target, provider_id)

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
            await _retire_for_switch(engine, transfer_id, artifact, queued=paused_here)
        outcome = await repository.replace_root_route(
            root.id, expected_attempt_id=str(latest["id"]), expected_provider_id=str(current),
            target_provider_id=str(provider_id), expected_demand=expected_demand,
            retain_replaced=engine._retains_replaced_route(root, str(current)), now=engine.clock())
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


async def _switch_root(engine, transfer, roots: dict[str, dict], root_id: str, provider_id: str, *,
                       expected_provider_id: str, expected_demand=None) -> dict:
    """One root of a transfer that is not one torrent root, fenced by
    exclusive recovery claims on exactly the artifacts whose selected
    candidate it alone produced (see the module docstring)."""
    transfer_id = int(transfer.id)
    facts = roots[root_id]
    if not manual_root(facts, single=len(roots) == 1):
        raise _refusal(Category.UNSUPPORTED_REQUEST, domain=Domain.REQUEST)
    current = facts["current"]
    if current != expected_provider_id:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    if provider_id == current:
        raise _refusal(Category.INVALID_REQUEST, domain=Domain.REQUEST)
    repository = engine.repository
    root = next(item for item in await repository.requests(transfer_id) if item.id == root_id)
    target = engine.registry.providers.get(str(provider_id))
    status = await root_providers(engine, transfer_id, root_id)
    choice = next((entry for entry in (status or {}).get("providers", ()) if entry["provider_id"] == provider_id),
                  None)
    if target is None or choice is None or not choice["selectable"]:
        raise _unselectable(choice, provider_id)
    latest = await _preflight(engine, root, current, choice, target, provider_id)

    claims = await engine.claim_route_switch(facts["artifacts"])
    if claims is None:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.BACKOFF)
    outcome = None
    try:
        # Under the claims: the root still alone produced exactly these
        # artifacts' selections (a selection that moved meanwhile refuses).
        again = (await repository.root_route_facts_by_root(transfer_id)).get(root_id)
        if again is None or again["artifacts"] != facts["artifacts"]:
            raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
        if again.get("shared"):
            raise _refusal(Category.UNSUPPORTED_REQUEST, domain=Domain.REQUEST)
        queued = not transfer.paused and not await repository.globally_paused()
        for claim in claims:
            artifact = await engine._current_artifact(transfer_id, claim.artifact_id)
            if artifact is not None and artifact.execution is not None and artifact.state != "completed":
                await _retire_for_switch(engine, transfer_id, artifact, queued=queued)
        outcome = await repository.replace_root_route(
            root.id, expected_attempt_id=str(latest["id"]), expected_provider_id=str(current),
            target_provider_id=str(provider_id), expected_demand=expected_demand,
            retain_replaced=engine._retains_replaced_route(root, str(current)), now=engine.clock(),
            claims=tuple(claims))
        if outcome == "shared":
            raise _refusal(Category.UNSUPPORTED_REQUEST, domain=Domain.REQUEST)
        if outcome != "replaced":
            raise _refusal(Category.RESOURCE_STATE_CONFLICT,
                           retryability=Retryability.BACKOFF if outcome in {"busy", "writer_live"}
                           else Retryability.IMMEDIATE)
    finally:
        for claim in claims:
            await engine._finish_claim(
                claim, action=RecoveryTrigger.USER_ROUTE_SWITCH.value,
                reason="route_switched" if outcome == "replaced" else (outcome or "route_switch_refused"),
                outcome="applied" if outcome == "replaced" else "not_applied",
                artifact=await engine._current_artifact(transfer_id, claim.artifact_id))
        await engine._aggregate(transfer_id)
    await engine._cleanup_pending()
    engine._resolution_opportunity(transfer_id)
    return {"transfer_id": transfer_id, "provider_id": str(provider_id), "previous_provider_id": current,
            "root_id": root_id, "artifact_ids": list(facts["artifacts"])}
