"""An operator's pending provider choice for one canonical file.

The provider selector lets an operator name a best-effort PRIMARY provider for
a transfer. For a file whose own link that provider cannot take, but which has
another proven-equivalent source (a canonical binding of the same file issued
by another root of the transfer) on a link the provider may take, the choice is
recorded as an INTENT (``pending_provider_choices``): the root of that
equivalent link is switched to the provider (the one-root switch,
``transfers.manual_route_switch``) and, when the provider's copy of that link
lands in the file's candidates (``CanonicalOwnership.replace_root_candidates``,
which lands into a non-selected binding's place and keeps the file's
selection), the canonical candidate activation selects it for the file.

Until then the file keeps its committed source and its writer; nothing about
it is held, retired or reselected. Its own recovery and failover stay fully
authoritative: a choice never vetoes them, and any committed candidate switch
of the file by another authority supersedes the choice in the same
transaction (``TransferRepository.transition_recovery``).

This module decides each pending choice from durable truth alone
(``reconcile``), at the end of every resolution cycle -- the cycle that lands
the provider's copy -- never on a timer of its own:

* ``committed``: a current binding of the file issued by the requested
  provider for that root (the root or its member), from the choice's own
  switch attempt or a later one, is selected through the one activation
  (``fulfils_choice``: the choice commits with the selection, or nothing does);
* ``superseded``: the file settled, its selection changed, or the root was
  switched again;
* ``failed``: the root's route is no longer the requested provider's (routing
  fell back to another provider: that is never fulfilment), the provider's
  route ended failed, or the activation was refused for good (e.g. it would
  discard downloaded data the operator never agreed to discard).

A choice that is still pending has a real owner: the root's own resolution
(its retry/backoff, preparation observation and readiness deadlines), which
decides its next event.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SETTLED_FILE = frozenset({"completed", "cancelled", "duplicate"})
_SETTLED_TRANSFER = frozenset({"completed", "consolidated", "deleted", "cancelled"})
# A route attempt that ended the provider's chance on this root for good; a
# failed attempt may still be retried by the root's own resolution policy.
_FAILED_ROUTE = frozenset({"exhausted", "declined"})
# Activation refusals a later cycle decides again (a concurrent claim or
# commit, or a paused file whose writer is parked until Resume); every other
# refusal ends the choice.
_TRANSIENT_REFUSALS = frozenset({"claim_not_current", "commit_conflict", "source_paused",
                                 # the target moved or changed between this
                                 # decision's read and the activation's claim
                                 "target_moved", "choice_target_mismatch"})


async def reconcile(engine, transfer_ids=None) -> set[int]:
    """Decide every pending provider choice (of ``transfer_ids``, or all) from
    current durable truth. Returns the transfers whose choice changed."""
    wanted = None if transfer_ids is None else {int(item) for item in transfer_ids}
    changed: set[int] = set()
    for choice in await engine.repository.pending_provider_choices():
        if wanted is not None and int(choice["transfer_id"]) not in wanted:
            continue
        try:
            if await _decide(engine, choice):
                changed.add(int(choice["transfer_id"]))
        except Exception:
            # One choice's failure to decide never stops another's, nor the
            # resolution cycle: it is decided again next cycle.
            logger.exception("Pending provider choice %s could not be decided", choice["id"])
    return changed


async def _decide(engine, choice: dict) -> bool:
    repository = engine.repository
    facts = await repository.provider_choice_landing(choice)

    async def end(state: str, reason: str) -> bool:
        return await repository.end_provider_choice(str(choice["id"]), state, reason)

    if (facts["artifact_status"] is None or facts["standby"] or facts["artifact_status"] in _SETTLED_FILE
            or facts["transfer_status"] in _SETTLED_TRANSFER):
        return await end("superseded", "file_settled")
    if facts["selected_candidate_id"] != str(choice["expected_candidate_id"]):
        return await end("superseded", "selection_changed")
    if facts["latest_switch_attempt_id"] != str(choice["switch_attempt_id"]):
        return await end("superseded", "route_switched_again")
    if facts["route_provider_id"] != str(choice["provider_id"]):
        # The root's routing moved on from the requested provider (a fallback):
        # never fulfilment of this choice.
        return await end("failed", "provider_fell_back")
    if facts["route_state"] in _FAILED_ROUTE or facts["root_state"] == "failed":
        return await end("failed", "provider_failed")
    landed = facts["landed_candidate_id"]
    transfer_id, artifact_id = int(choice["transfer_id"]), int(choice["artifact_id"])
    artifact = await engine._current_artifact(transfer_id, artifact_id) if landed is not None else None
    index = next((index for index, item in enumerate(artifact.candidates if artifact else ())
                  if str(item.id) == landed), None)
    if index is None:
        if facts["root_state"] == "resolved" and facts["route_state"] == "succeeded":
            # The provider's route of the root settled without its copy landing
            # in this file: nothing will fulfil the choice any more.
            return await end("failed", "not_landed")
        return False                                  # the root's own resolution owns the next event
    # Named by identity: the activation re-finds it under its claim, checks it
    # is still this choice's authorized result before disturbing anything, and
    # the commit checks it again (``TransferRepository._settle_provider_choice``).
    result = await engine.activate_candidate_command(transfer_id, artifact_id, index, permit_discard=False,
                                                     fulfils_choice=str(choice["id"]), target_candidate_id=landed)
    if result is None or (not result.committed and result.reason in _TRANSIENT_REFUSALS):
        return False                                  # decided again next cycle
    if result.committed:
        return True
    reason = "discard_confirmation_required" if result.reason == "material_discard_refused" else result.reason
    return await end("failed", reason)
