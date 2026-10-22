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


def expanded(facts: dict) -> bool:
    """Whether a root expanded into files of its own: its CURRENT generation
    has more than one member, or a member owns a canonical artifact (a file
    of a directory, a media member, ... -- governed by its decomposition
    generation; whatever generation is opening), or it is a BitTorrent root. A link a provider
    resolved through a copy of its own that it describes as ONE member whose
    candidate serves the root's file (a cloud transfer) is still one link of
    one file -- however many earlier providers' copies were made of it before
    (their members are history, ``_current_member_ids``): its switch holds and
    lands that file through the member (``_SWITCH_SUBJECT_ORIGIN``,
    ``CanonicalOwnership.replace_root_candidates``)."""
    if not facts["decomposed"]:
        return False
    return (facts["kind"] in BITTORRENT_REQUEST_KINDS or int(facts.get("members") or 0) > 1
            or bool(facts.get("member_owned")))


def manual_root(facts: dict, *, single: bool) -> bool:
    """Whether an operator may switch this root's provider at all: the one
    root of a torrent transfer (``single``), or any other root that did not
    expand into several files (``expanded``) and that produced the selected
    candidate of at least one unfinished artifact (``relevant``). A root
    supplying only alternates, an expanded root of anything but a single
    torrent, and a root whose candidate another transfer's artifact holds
    (``shared``: the switch fences only its own transfer's artifacts), is never
    a switch subject -- for the offer and the switch alike."""
    if single and torrent_root(facts):
        return True
    return not expanded(facts) and facts["relevant"] and not facts.get("shared")


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
    that root's picker), ``"transfer"`` when at least one of its several
    roots is -- or when some provider may become the primary of one of its
    files through another proven-equivalent source of that file
    (``equivalent_plan``) -- (the launcher opens the transfer's provider
    picker, ``transfer_route_status``: a best-effort choice that each root's
    own switch then admits or refuses), else ``None``."""
    if any(root_switches(registry, roots, memo).values()):
        return "root" if len(roots) == 1 else "transfer"
    if len(roots) < 2:
        return None
    entries: dict[str, dict[str, dict]] = {}

    def entry_for(root_id: str, provider_id: str | None):
        """``provider_id``'s entry for the root, or every entry (``None``)."""
        if root_id not in entries:
            entries[root_id] = {item["provider_id"]: item for item in provider_choices(registry, roots[root_id], memo)}
        return entries[root_id] if provider_id is None else entries[root_id].get(provider_id)

    # A provider is offered when it serves, or may take the route of, a root
    # that issued an equivalent source of some file.
    offered = set()
    for facts in roots.values():
        if not facts["alternates"]:
            continue
        if facts["current"]:
            offered.add(facts["current"])
        if _root_exclusion(facts) is None:
            offered.update(provider_id for provider_id, item in (entry_for(facts["root_id"], None) or {}).items()
                           if item["selectable"])
    # Exactly ``transfer_route_status``'s rule: a participant whose own link
    # the provider does not claim (``unsupported``) reaches it through an
    # equivalent source.
    return "transfer" if any(
        equivalent_plan(roots, facts, provider_id, entry_for)
        for facts in roots.values() if facts["relevant"] and _root_exclusion(facts) is None
        for provider_id in offered
        if provider_id and provider_id != facts["current"] and entry_for(facts["root_id"], provider_id) is None
    ) else None


# How a provider becomes the primary of a file through ANOTHER of its
# proven-equivalent sources (``equivalent_plan``): ``source`` -- that
# provider already serves an equivalent source of the file (a root of this
# transfer routed to it issued one), which is selected; ``alternate`` -- it
# may take the route of a root that issued an equivalent source, which is
# selected and then switched to it (its replacement lands in that file).
VIA_ROUTE, VIA_SOURCE, VIA_ALTERNATE = "route", "source", "alternate"


# A routing answer that is not a valid premium provider's claim on a link: the
# provider is disabled, not entitled on this account, or a generic provider
# deferring to the specialized ones.
_NOT_A_CLAIM = frozenset({"disabled", "not_entitled", "held"})


def _equivalent_claim(roots: dict[str, dict], facts: dict, provider_id: str, entry_for) -> dict | None:
    """``provider_id``'s entry on a root that issued an equivalent source of
    one of ``facts``' files -- a legitimate claim it cannot act on now (for
    ``transfer_route_status``'s truthful status), or ``None``."""
    for artifact_id in facts["artifacts"]:
        for root_id, other in roots.items():
            if root_id == facts["root_id"] or _root_exclusion(other) is not None:
                continue
            if any(item["artifact_id"] == artifact_id for item in other["alternates"]):
                entry = entry_for(root_id, provider_id)
                if entry is not None and entry.get("reason") not in _NOT_A_CLAIM:
                    return entry
    return None


def equivalent_plan(roots: dict[str, dict], facts: dict, provider_id: str, entry_for) -> list[dict]:
    """For each unfinished file whose selected source ``facts``' root
    produced, the one way ``provider_id`` may become that file's provider
    through another current, proven-equivalent source of it (the root route
    facts' ``alternates``: canonical bindings of that same file, issued by
    roots of this transfer), in canonical candidate order: an equivalent
    source that provider already serves (``source``: its issuing root's
    committed route is that provider), else one whose issuing root it may
    legitimately take now (``alternate``: that root is a switch subject
    once its source is selected -- not excluded, ``entry_for(root, provider)``
    selectable). Files with neither are absent. Pure: route facts and the
    caller's provider entries only; nothing here promises the switch."""
    plan = []
    for artifact_id in facts["artifacts"]:
        offers = sorted(((item["order"], root_id, item) for root_id, other in roots.items()
                         for item in other["alternates"] if item["artifact_id"] == artifact_id),
                        key=lambda value: value[0])
        chosen = next(({"artifact_id": artifact_id, "candidate_id": item["candidate_id"], "via_root_id": root_id,
                        "via": VIA_SOURCE, "via_provider_id": roots[root_id]["current"]}
                       for _order, root_id, item in offers
                       if item["provider_id"] == provider_id and roots[root_id]["current"] == provider_id
                       and not roots[root_id]["terminal"]), None)
        if chosen is None:
            chosen = next(({"artifact_id": artifact_id, "candidate_id": item["candidate_id"], "via_root_id": root_id,
                            "via": VIA_ALTERNATE, "via_provider_id": roots[root_id]["current"]}
                           for _order, root_id, item in offers
                           if roots[root_id]["current"] and roots[root_id]["current"] != provider_id
                           and item["provider_id"] == roots[root_id]["current"]
                           and _root_exclusion(roots[root_id]) is None
                           and (entry_for(root_id, provider_id) or {}).get("selectable")), None)
        if chosen is not None:
            plan.append(chosen)
    return plan


# One root's part in a transfer-level provider choice (``transfer_route_status``).
# ``eligible``: the provider may take the root now (the root's own picker would
# offer it). ``already_selected``: the root's committed route is that
# provider. ``unsupported``: the provider does not claim the root's link.
# ``unavailable`` (with ``provider_choices``' reason) and ``preparing``: it
# claims the link but cannot take it now. ``inactive`` (the transfer is
# finished), ``decomposed`` (the root expanded into several files of a
# transfer of several roots), ``shared`` (another transfer's file uses a
# source this root produced) and ``no_route`` (nothing committed yet): the
# root is not a switch subject at all (``manual_root``).
ROOT_ELIGIBLE, ROOT_ALREADY_SELECTED, ROOT_UNSUPPORTED, ROOT_UNAVAILABLE, ROOT_PREPARING = (
    "eligible", "already_selected", "unsupported", "unavailable", "preparing")
ROOT_INACTIVE, ROOT_DECOMPOSED, ROOT_SHARED, ROOT_NO_ROUTE = "inactive", "decomposed", "shared", "no_route"
# ``pending``: every file of the root already has a pending choice of that
# provider (``transfers.provider_choice``): nothing more to request now.
ROOT_PENDING = "pending"


def _root_exclusion(facts: dict) -> str | None:
    """Why a root of a transfer of several roots is not a switch subject for
    any provider (``manual_root`` and ``root_actionable``), or ``None``."""
    if facts["terminal"]:
        return ROOT_INACTIVE
    if expanded(facts):
        return ROOT_DECOMPOSED
    if facts.get("shared"):
        return ROOT_SHARED
    if not facts["current"]:
        return ROOT_NO_ROUTE
    return None


def root_participation(facts: dict, entry: dict | None, provider_id: str) -> tuple[str, str | None]:
    """``(status, reason)`` of one root for ``provider_id``, from its route
    facts and that provider's entry in the root's own read model
    (``_providers``; ``None`` when the provider does not claim the root)."""
    excluded = _root_exclusion(facts)
    if excluded:
        return excluded, None
    if facts["current"] == provider_id:
        return ROOT_ALREADY_SELECTED, None
    if entry is None:
        return ROOT_UNSUPPORTED, None
    if entry["selectable"]:
        return ROOT_ELIGIBLE, None
    if entry["status"] == PREPARING:
        return ROOT_PREPARING, None
    return ROOT_UNAVAILABLE, entry.get("reason")


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


async def transfer_route_status(engine, transfer_id: int) -> dict | None:
    """The provider status of a transfer of SEVERAL roots, for one
    best-effort transfer-level choice. Its participants are the roots that
    produced the selected candidate of at least one unfinished file
    (``relevant``); a root supplying only alternates takes no part. For every
    provider claiming at least one participant, each participant's
    ``root_participation`` and the counts of roots it may take now
    (``eligible``) or already has (``already_selected``). Each root's status
    is exactly what that root's own picker (``root_providers``) reports; a
    choice made from it is admitted or refused root by root by the one-root
    switch, which re-reads everything. Nothing here promises that a switch
    will commit, or reserves anything. ``None`` for a transfer of one root:
    a torrent root and a single link keep their own picker.

    The choice is a provider as the operator's best-effort PRIMARY: it is
    evaluated against every current, proven-equivalent source of the files
    (the roots that issued their other sources -- ``alternates`` -- take
    part in which providers are offered), not only the selected one. A
    participant whose own route that provider cannot take now is still
    ``eligible`` for it when some of its files may reach that provider
    through another equivalent source (``equivalent_plan``: ``via``
    ``source`` or ``alternate``, with the ``plan`` per file); otherwise
    ``via`` is ``route`` (its own route). A provider claiming none of the
    sources is not listed."""
    roots = await engine.repository.root_route_facts_by_root(int(transfer_id))
    if len(roots) < 2:
        return None
    participants = [facts for facts in roots.values() if facts["relevant"]]
    choices: dict[str, dict[str, dict]] = {}
    order: list[str] = []
    for facts in participants:
        if facts["current"] and facts["current"] not in order:
            order.append(facts["current"])
        if _root_exclusion(facts) is None:
            entries = await _providers(engine, transfer_id, facts)
            choices[facts["root_id"]] = {entry["provider_id"]: entry for entry in entries}
            order.extend(entry["provider_id"] for entry in entries if entry["provider_id"] not in order)
    for facts in roots.values():
        # The roots that issued other equivalent sources: their provider, and
        # every provider that may legitimately take them now.
        if not facts["alternates"] or facts["relevant"]:
            continue
        if facts["current"] and facts["current"] not in order:
            order.append(facts["current"])
        if _root_exclusion(facts) is None:
            entries = await _providers(engine, transfer_id, facts)
            choices[facts["root_id"]] = {entry["provider_id"]: entry for entry in entries}
            # Every enabled provider with a legitimate claim on that link --
            # whether or not it may take it now (its restriction is shown).
            order.extend(entry["provider_id"] for entry in entries
                         if entry.get("reason") not in _NOT_A_CLAIM and entry["provider_id"] not in order)

    def entry_for(root_id: str, provider_id: str | None):
        return choices.get(root_id, {}).get(provider_id)

    providers = []
    for provider_id in order:
        rows = []
        for facts in participants:
            status, reason = root_participation(facts, choices.get(facts["root_id"], {}).get(provider_id),
                                                provider_id)
            row = {"root_id": facts["root_id"], "status": status, "reason": reason,
                   "current_provider_id": facts["current"], "artifact_ids": list(facts["artifacts"]),
                   "via": VIA_ROUTE if status == ROOT_ELIGIBLE else None}
            awaiting = {item["artifact_id"] for item in facts["pending_choices"] if item["provider_id"] == provider_id}
            if facts["artifacts"] and awaiting >= set(facts["artifacts"]):
                rows.append(dict(row, status=ROOT_PENDING, reason=None, via=None))
                continue
            # A provider that cannot claim this root's own link at all may still
            # reach its files through another equivalent source; one that
            # claims it but cannot take it now keeps that truthful status.
            if status == ROOT_UNSUPPORTED and not facts["terminal"]:
                plan = [step for step in equivalent_plan(roots, facts, provider_id, entry_for)
                        if step["artifact_id"] not in awaiting]
                if plan:
                    row.update(status=ROOT_ELIGIBLE, reason=None, plan=plan,
                               via=VIA_SOURCE if all(item["via"] == VIA_SOURCE for item in plan)
                               else VIA_ALTERNATE)
                else:
                    claim = _equivalent_claim(roots, facts, provider_id, entry_for)
                    if claim is not None:
                        # It claims an equivalent link of these files but cannot
                        # take it now: that link's own restriction, truthfully.
                        row.update(status=ROOT_PREPARING if claim["status"] == PREPARING else ROOT_UNAVAILABLE,
                                   reason=None if claim["status"] == PREPARING else claim.get("reason"))
            rows.append(row)
        eligible = sum(row["status"] == ROOT_ELIGIBLE for row in rows)
        providers.append({"provider_id": provider_id, "eligible": eligible,
                          "already_selected": sum(row["status"] == ROOT_ALREADY_SELECTED for row in rows),
                          "participants": len(rows), "selectable": eligible > 0, "roots": rows})
    return {"transfer_id": int(transfer_id), "scope": "transfer", "participants": len(participants),
            "providers": providers, "switchable": any(item["selectable"] for item in providers)}


def transfer_choice_refusal(provider_id: str | None) -> TransferError:
    """Why a transfer-level choice cannot be made at all: the transfer is not
    one of several roots (``None``), or ``provider_id`` claims none of its
    participating roots."""
    if provider_id is None:
        return _refusal(Category.UNSUPPORTED_REQUEST, domain=Domain.REQUEST)
    return _unselectable(None, provider_id)


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
                               expected_root_id: str | None = None, source: tuple[int, str] | None = None) -> dict:
    """Replace one root's provider route with ``provider_id`` (see the module
    docstring). ``expected_provider_id`` is the provider the operator saw as
    current: if the committed route moved since, nothing changes. The root is
    the torrent transfer's one root, the transfer's only root, or -- required
    for a transfer of several roots -- the root that alone produced the
    selected candidate of ``artifact_id``, re-derived here; when it is not
    ``expected_root_id`` (the root the operator saw) nothing changes. Raises a
    normalized ``TransferError`` for every refusal; the requested provider is
    never silently replaced by another.

    ``source`` (``(artifact_id, candidate_id)``, with ``expected_root_id``):
    an operator's provider choice for ONE file whose own link the provider
    cannot take -- the root to switch is the one that issued that EQUIVALENT
    source of the file (``equivalent_plan``'s ``alternate``), and the file
    gets a pending provider choice (``transfers.provider_choice``) instead of
    any change now (``_switch_root``'s ``choice``)."""
    transfer = await engine.repository.get(int(transfer_id))
    if transfer is None:
        raise KeyError(transfer_id)
    roots = await engine.repository.root_route_facts_by_root(int(transfer_id))
    if source is not None:
        if expected_root_id is None or str(expected_root_id) not in roots or len(roots) < 2:
            raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
        return await _switch_root(engine, transfer, roots, str(expected_root_id), provider_id,
                                  expected_provider_id=expected_provider_id, expected_demand=expected_demand,
                                  choice_for=(int(source[0]), str(source[1])))
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
                       expected_provider_id: str, expected_demand=None,
                       choice_for: tuple[int, str] | None = None) -> dict:
    """One root of a transfer that is not one torrent root, fenced by
    exclusive recovery claims on exactly the artifacts whose selected
    candidate it alone produced (see the module docstring).

    ``choice_for`` (``(artifact_id, candidate_id)``): the operator chose this
    provider as the primary of a file the root does NOT produce the selected
    source of, but issued an equivalent source of (``_choice_subject``). The
    root's route is replaced exactly as any switch, and -- in that same commit,
    only while the file still selects what it selected here and the source is
    still that root's equivalent binding of it -- the file gets a pending
    provider choice (``transfers.provider_choice``): its selection, writer and
    recovery are untouched now; the provider's copy is selected for it only
    when it lands. Refusals decided before anything changes include moving
    the file's writing onto that source discarding existing material (the
    operator's own confirmation is required for that, never assumed)."""
    transfer_id = int(transfer.id)
    facts = roots[root_id]
    if choice_for is not None:
        if not _choice_subject(facts, *choice_for):
            raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    elif not manual_root(facts, single=len(roots) == 1):
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
    pending = None
    if choice_for is not None:
        pending = await _choice_preflight(engine, transfer_id, roots, choice_for)
    latest = await _preflight(engine, root, current, choice, target, provider_id)

    claims = await engine.claim_route_switch(facts["artifacts"])
    if claims is None:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.BACKOFF)
    outcome = None
    try:
        # Under the claims: the root still alone produced exactly these
        # artifacts' selections (a selection that moved meanwhile refuses).
        current_roots = await repository.root_route_facts_by_root(transfer_id)
        again = current_roots.get(root_id)
        if (again is None or again["artifacts"] != facts["artifacts"]
                or (choice_for is None and not manual_root(again, single=len(current_roots) == 1))
                or (choice_for is not None and not _choice_subject(again, *choice_for))):
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
            claims=tuple(claims), pending_choice=pending)
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
    result = {"transfer_id": transfer_id, "provider_id": str(provider_id), "previous_provider_id": current,
              "root_id": root_id, "artifact_ids": list(facts["artifacts"])}
    if pending is not None:
        result.update(pending_choice_id=pending["id"], pending_artifact_id=pending["artifact_id"])
    return result


def _choice_subject(facts: dict, artifact_id: int, candidate_id: str) -> bool:
    """Whether a root may be switched for ONE file it did not produce the
    selected source of: it is not expanded, shared or finished, and it issued
    ``candidate_id`` -- a current, proven-equivalent source of that file on
    its committed route (the route facts' ``alternates``)."""
    return (not expanded(facts) and not facts.get("shared") and not facts["terminal"] and any(
        item["artifact_id"] == int(artifact_id) and item["candidate_id"] == str(candidate_id)
        and item["provider_id"] == facts["current"] for item in facts["alternates"]))


async def _choice_preflight(engine, transfer_id: int, roots: dict[str, dict], choice_for: tuple[int, str]) -> dict:
    """A pending provider choice's refusals knowable before anything changes,
    and the record to create: the file is an unfinished artifact of the
    transfer with that equivalent source among its candidates and a selected
    source produced by one of its roots (``owning_root_id``); moving its
    writing onto that source would not discard existing material (the same
    preview the operator's own source switch confirms -- never confirmed here,
    so refused). The file's selection now is the generation the choice is
    bound to (``expected_candidate_id``)."""
    from uuid import uuid4

    from transfers.manual_failover import DiscardConfirmationRequired
    artifact_id, candidate_id = choice_for
    artifact = await engine._current_artifact(int(transfer_id), int(artifact_id))
    candidate = next((item for item in (artifact.candidates if artifact else ())
                      if str(item.id) == str(candidate_id)), None)
    owning = subject_root(roots, int(artifact_id))
    if artifact is None or candidate is None or artifact.state == "completed" or owning is None:
        raise _refusal(Category.RESOURCE_STATE_CONFLICT, retryability=Retryability.IMMEDIATE)
    preview = await engine.preview_continuation(artifact, candidate)
    if preview is not None and preview.discarded_bytes:
        raise DiscardConfirmationRequired(preview.discarded_bytes, preview.retained_bytes,
                                          preview.material_generation, unusable_bytes=preview.unusable_bytes)
    return {"id": uuid4().hex, "artifact_id": int(artifact_id), "source_candidate_id": str(candidate_id),
            "owning_root_id": str(owning), "expected_candidate_id": str(artifact.candidates[artifact.selected].id)}
