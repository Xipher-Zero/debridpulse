"""The operator queue move: a transfer's place in the durable queue.

The queue is ``torrents.priority`` (``TransferRepository.queue``): resolution
and execution admission already serve it in that order. A move names the
transfer's new neighbours (``before_id``/``after_id``) in the complete queue,
never page positions, and is refused unchanged when those neighbours are no
longer adjacent there (``queue_changed``). Move to Top (``top``) names a set
of transfers instead: its queue members become the front of the queue, in
their current queue order, ahead of every other member (``top_of_queue``) --
one transfer or a selection alike, planned and committed as one move.

A move is read as the overtakes it makes (``overtakes``): every transfer it
ranks above one it ranked below before -- the moved transfer when it goes
up, or every member it fell below when it goes down, so dragging A below B
and dragging B above A mean the same. Each overtaking
transfer holds the operator's promotion (``torrents.queue_promotion``),
committed with the new order: the authority to take an execution slot from
work ranked below it. Nothing else creates one -- an ordinary submission, an
absolute priority, or a move that overtakes nothing.

A promotion is acted on only by the real execution admission: when
``TransferEngine._dispatch`` proves an artifact of the promoted transfer
waits for nothing but a slot, it selects, by the current queue rank and the
writers actually occupying slots, the minimal number of writers ranked below
it, the lowest-ranked first (``displaced``) -- whichever transfers the drag
crossed -- and the one native control owner
(``TransferEngine._converge_execution``) re-checks eligibility immediately
before it retires each through the one writer retirement and requeues its
artifact. Whatever defers that -- the promoted transfer not yet resolved,
paused, held, or a writer finalizing or not proven stopped -- the promotion
stands and a later pass converges.

A writer whose acquired progress the retirement would lose is displaced only
when the operator confirmed exactly that attempt: a move previews the
displacement it would cause now and, unconfirmed, changes nothing and reports
the consequence (``RestartConfirmationRequired``). The confirmed attempts are
recorded with the promotion; no other attempt is ever restarted for it. When
admission finds the promotion waiting only on a restart nobody confirmed --
the transfer was not eligible when it was moved, or the confirmed attempt was
replaced since -- it says so (``restart_consent_ids``), and the operator
reaffirms the transfer's place (``reaffirm``) through the same confirmation.
A writer that cannot be interrupted safely now (finalizing, unresolved,
paused, a native transition) is never chosen.
"""
from __future__ import annotations

from dataclasses import dataclass

from transfers import journal_events as je
from transfers.continuation import retirement_keeps_progress


class QueueMoveRefused(Exception):
    """Nothing was moved: ``not_queued`` (the transfer is no queue member) or
    ``queue_changed`` (the named neighbours are not where the operator saw
    them, or the plan was overtaken while it was committed)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RestartConfirmationRequired(Exception):
    """The move would interrupt active writers that cannot keep what they
    acquired. Nothing was changed. ``transfers`` lists each affected logical
    transfer once, with every attempt the operator must confirm; ``changed``
    marks a confirmation that no longer covers the current consequence."""

    def __init__(self, transfers: tuple[dict, ...], *, changed: bool):
        super().__init__("reordering restarts active transfers")
        self.transfers = transfers
        self.changed = bool(changed)


@dataclass(frozen=True)
class QueueMove:
    priority: int
    moved: bool
    # The transfers whose place the move set: the moved one, or Move to
    # Top's block in its queue order.
    members: tuple[int, ...]
    # ``unchanged`` | ``queued`` (admitted by ordinary admission whenever it
    # is eligible and a slot is free) | ``preempting`` (lower-ranked writers
    # yield it their slots) | ``waiting`` (it waits for a slot nothing could
    # safely release now).
    outcome: str
    preempting: tuple[dict, ...] = ()


def proposed_queue(queue: tuple[int, ...], transfer_id: int, *, before_id: int | None,
                   after_id: int | None) -> tuple[int, ...]:
    """The queue with ``transfer_id`` placed between its named neighbours."""
    rest = [member for member in queue if member != transfer_id]
    anchors = [anchor for anchor in (before_id, after_id) if anchor is not None]
    if not anchors or any(anchor == transfer_id or anchor not in rest for anchor in anchors):
        raise QueueMoveRefused("queue_changed")
    if before_id is not None and after_id is not None and rest.index(after_id) + 1 != rest.index(before_id):
        raise QueueMoveRefused("queue_changed")
    index = rest.index(before_id) if before_id is not None else rest.index(after_id) + 1
    rest.insert(index, transfer_id)
    return tuple(rest)


def top_of_queue(queue: tuple[int, ...], transfer_ids) -> tuple[int, ...]:
    """The queue with the members among ``transfer_ids`` first, in their
    current queue order, and every other member after them in its own."""
    wanted = frozenset(transfer_ids)
    return (tuple(member for member in queue if member in wanted)
            + tuple(member for member in queue if member not in wanted))


def interruption(writer: dict, executor) -> str | None:
    """How retiring ``writer`` now would end: ``resumable`` (its progress
    becomes DP material a fresh writer continues from), ``idle`` (it has not
    started acquiring), ``restart`` (its acquired progress is lost), or
    ``None`` when it cannot be interrupted safely now."""
    if (executor is None or writer["paused"] or writer["native_transition_from"]
            or writer["state"] not in {"queued", "running"}
            or writer["artifact_state"] not in {"queued", "downloading"}):
        return None
    if writer["state"] == "running" and writer["progress"].get("acquiring") is False:
        return None                                   # finalizing: wait for its own end
    if retirement_keeps_progress(executor.capabilities, writer["continuation"].get("strategy")):
        return "resumable"
    return "restart" if writer["state"] == "running" else "idle"


def _affected(victims: list[dict], names: dict[int, str]) -> tuple[dict, ...]:
    """Each affected logical transfer once, whatever number of its writers,
    named by ``names`` (its display name, as Downloads shows it)."""
    grouped: dict[int, list[dict]] = {}
    for victim in victims:
        grouped.setdefault(victim["transfer_id"], []).append(victim)
    affected = []
    for transfer_id, writers in grouped.items():
        progress = [writer["progress"] for writer in writers]
        totals = [int(item.get("total_bytes") or 0) for item in progress]
        estimates = [item.get("estimated_percentage") for item in progress]
        affected.append({
            "transfer_id": transfer_id,
            "name": str(writers[0]["name"] or ""),
            "display_name": names.get(transfer_id) or str(writers[0]["name"] or ""),
            "attempt_ids": [writer["attempt_id"] for writer in writers],
            "completed_bytes": sum(max(0, int(item.get("completed_bytes") or 0)) for item in progress),
            "total_bytes": sum(totals) if all(totals) else None,
            "estimated_percentage": (float(estimates[0]) if len(writers) == 1
                                     and isinstance(estimates[0], (int, float)) else None),
        })
    return tuple(affected)


def overtakes(queue: tuple[int, ...], proposed: tuple[int, ...]) -> frozenset[int]:
    """The transfers the move ranks above a transfer they ranked below
    before: for one moved transfer, itself when it passed any member going
    up, or each member it fell below going down; for Move to Top, each
    selected transfer that passed an unselected one -- never one that
    already led every member it now leads."""
    new = {member: index for index, member in enumerate(proposed)}
    overtaking, lowest_above = set(), -1
    for member in queue:                                # in the old order
        if lowest_above > new[member]:
            overtaking.add(member)
        lowest_above = max(lowest_above, new[member])
    return frozenset(overtaking)


def displaced(writers, rank: dict[int, int], transfer_id: int, *, wanted: int, free: int, capacity: int,
              executor_of, restartable: frozenset[str] | None = None,
              releasing: frozenset[str] = frozenset()) -> tuple[list[dict], bool, list[dict]]:
    """THE displacement rule: by current queue rank and the writers actually
    occupying slots, the writers ranked below ``transfer_id`` that yield
    their slots so ``wanted`` waiting artifacts (its own and every one ranked
    above it) get one; whether that is still unmet; and the restarts it would
    need that nobody confirmed (``withheld``).

    Never more than capacity allows; slots already free, writers already
    ``releasing`` theirs and lower-ranked writers that are finalizing (about
    to free theirs) count first. Writers are taken lowest-ranked first; a
    restart is taken only for an attempt in ``restartable`` (``None``: any,
    to preview what a move would need confirmed)."""
    lower = [writer for writer in writers if rank.get(writer["transfer_id"], -1) > rank[transfer_id]]
    finalizing = sum(1 for writer in lower if writer["attempt_id"] not in releasing
                     and writer["state"] == "running" and writer["progress"].get("acquiring") is False)
    needed = max(0, min(wanted, max(1, capacity)) - free - len(releasing) - finalizing)
    victims, withheld = [], []
    for writer in sorted(lower, key=lambda item: (-rank[item["transfer_id"]], -int(item["artifact_id"]))):
        if len(victims) >= needed:
            break
        if writer["attempt_id"] in releasing:
            continue
        kind = interruption(writer, executor_of(writer["executor_id"]))
        if kind is None:
            continue
        if kind == "restart" and restartable is not None and writer["attempt_id"] not in restartable:
            withheld.append(writer)
            continue
        victims.append({**writer, "kind": kind})
    return victims, len(victims) < needed, withheld


async def _preview(engine, overtaking: frozenset[int], rank: dict[int, int]):
    """The displacement the overtakes would cause now. Each eligible
    overtaking transfer asks for a slot for each queued artifact without a
    writer (one while it is still resolving), behind the capacity-only
    waiters ranked above it; an ineligible one is left to admission."""
    repository = engine.repository
    owners = await repository.artifact_transfers(engine.capacity_only_blocked_ids())
    writers = await repository.queue_writers()
    free = await engine.free_execution_slots()
    victims, unmet, demand = [], False, 0
    for transfer_id in sorted(overtaking, key=rank.__getitem__):
        if not engine.dispatch_permitted or not await engine._live(transfer_id, admission=True):
            continue
        demand += await repository.writer_demand(transfer_id)
        ahead = sum(1 for owner in owners.values() if owner in rank and rank[owner] < rank[transfer_id])
        chosen, short, _withheld = displaced(
            writers, rank, transfer_id, wanted=ahead + demand, free=free,
            capacity=engine.policy.max_active_executions, executor_of=engine.registry.executors.get,
            releasing=frozenset(victim["attempt_id"] for victim in victims))
        victims += [{**victim, "for": transfer_id} for victim in chosen]
        unmet = unmet or short
    return victims, unmet


async def move_in_queue(engine, transfer_id: int | None = None, *, before_id: int | None = None,
                        after_id: int | None = None, top: frozenset[int] = frozenset(), reaffirm: bool = False,
                        confirmed: frozenset[str] = frozenset()) -> QueueMove:
    """Plan and commit one operator queue move, or change nothing.

    ``top`` is Move to Top: the queue members among those transfers move to
    the front together (``top_of_queue``) -- their overtakes, displacement
    and restart confirmation planned once over the whole proposed queue;
    transfers that are no queue member are left as they are, and only a
    selection without one is refused (``not_queued``). ``reaffirm`` keeps
    the transfer where it is and renews the promotion it already holds, with
    the restarts the operator confirmed now. Planned from
    durable truth under the execution-admission lock, so no admission or
    pause intent interleaves; committed in one transaction that re-checks the
    queue, re-planned once when that check finds the plan overtaken.
    ``preempting`` names the writers displaced now; admission still selects
    them, from current truth, when a promoted transfer actually waits."""
    repository = engine.repository
    confirmed = frozenset(str(item) for item in confirmed)
    async with engine._dispatch_lock:
        for _attempt in range(2):
            queue = await repository.queue()
            if top:
                members = tuple(member for member in queue if member in top)
                if not members:
                    raise QueueMoveRefused("not_queued")
                transfer_id = members[0]
                proposed = top_of_queue(queue, members)
                overtaking = overtakes(queue, proposed)
            elif transfer_id not in queue:
                raise QueueMoveRefused("not_queued")
            else:
                members = (transfer_id,)
                if reaffirm:
                    proposed = queue
                    held = await repository.queue_promotion(transfer_id)
                    overtaking = frozenset({transfer_id}) if held is not None else frozenset()
                else:
                    proposed = proposed_queue(queue, transfer_id, before_id=before_id, after_id=after_id)
                    overtaking = overtakes(queue, proposed)
            if not overtaking and proposed == queue:
                transfer = await repository.get(transfer_id)
                return QueueMove(int(transfer.priority or 0), False, members, "unchanged")
            rank = {member: index for index, member in enumerate(proposed)}
            victims, unmet = await _preview(engine, overtaking, rank)
            held = {member: set((await repository.queue_promotion(member) or {}).get("restart") or ())
                    for member in overtaking}
            restarts = [victim for victim in victims if victim["kind"] == "restart"]
            unconfirmed = [victim for victim in restarts
                           if victim["attempt_id"] not in confirmed | held[victim["for"]]]
            if unconfirmed:
                names = await repository.display_names({victim["transfer_id"] for victim in unconfirmed})
                raise RestartConfirmationRequired(_affected(unconfirmed, names), changed=bool(confirmed))
            if reaffirm and not restarts:
                # Nothing to confirm now: the promotion stands as it is.
                transfer = await repository.get(transfer_id)
                return QueueMove(int(transfer.priority or 0), False, members, "preempting" if victims
                                 else "waiting" if unmet else "queued", tuple(
                    {"transfer_id": victim["transfer_id"], "attempt_id": victim["attempt_id"],
                     "restart": False} for victim in victims))
            promotions = {member: {"restart": sorted(
                victim["attempt_id"] for victim in restarts if victim["for"] == member)}
                for member in overtaking}
            if reaffirm:
                events = (je.queue_reaffirmed(transfer_id, confirmed=len(restarts)),)
            elif top:
                # Each transfer whose place the block changed, once.
                events = tuple(je.queue_moved(member, before_id=None, after_id=None,
                                              promoted=member in overtaking)
                               for index, member in enumerate(members) if queue[index] != member)
            else:
                events = (je.queue_moved(transfer_id, before_id=before_id, after_id=after_id,
                                         promoted=bool(overtaking)),)
            priority = await repository.commit_queue_move(
                transfer_id, expected=queue, proposed=proposed, promotions=promotions, events=events)
            if priority is not None:
                outcome = "preempting" if victims else "waiting" if unmet else "queued"
                return QueueMove(priority, not reaffirm, members, outcome, tuple(
                    {"transfer_id": victim["transfer_id"], "attempt_id": victim["attempt_id"],
                     "restart": victim["kind"] == "restart"} for victim in victims))
    raise QueueMoveRefused("queue_changed")
