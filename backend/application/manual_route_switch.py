"""Application command and read for an operator's root provider route."""
from __future__ import annotations

from transfers.errors import TransferError
from transfers.manual_route_switch import (
    ROOT_ALREADY_SELECTED,
    ROOT_ELIGIBLE,
    route_status,
    switch_root_provider,
    transfer_choice_refusal,
    transfer_route_status,
)


async def switch_route_provider(application, transfer_id: int, provider_id: str, *, expected_provider_id: str,
                                expected_demand=None, artifact_id: int | None = None,
                                expected_root_id: str | None = None) -> dict:
    # Inside the one operator pause-control boundary: the switch's temporary
    # writer fence can never clear a Pause or Pause All issued meanwhile.
    async with application.operator_controls, application.application_operation():
        await application.require(int(transfer_id))
        result = await switch_root_provider(application.engine, int(transfer_id), str(provider_id),
                                            expected_provider_id=str(expected_provider_id),
                                            expected_demand=expected_demand, artifact_id=artifact_id,
                                            expected_root_id=expected_root_id)
        # The replacement left the root pending on its new route: resolution
        # work is runnable now, not at the next provider poll. The engine's
        # own opportunity reaches only a cycle already running.
        application.resolution_wakeup.set()
        application.execution_wakeup.set()
        await application._publish(int(transfer_id))
        return result


async def root_route_providers(application, transfer_id: int, *, artifact_id: int | None = None) -> dict | None:
    """Read-only: the provider status of one root's route -- the torrent
    root's, the transfer's only root's, or the root that produced the selected
    candidate of ``artifact_id``."""
    await application.require(int(transfer_id))
    return await route_status(application.engine, int(transfer_id), artifact_id)


async def transfer_route_providers(application, transfer_id: int) -> dict | None:
    """Read-only: the provider status of a transfer of several roots
    (``transfer_route_status``)."""
    await application.require(int(transfer_id))
    return await transfer_route_status(application.engine, int(transfer_id))


async def switch_transfer_provider(application, transfer_id: int, provider_id: str, *,
                                   expected_routes: dict[str, str], expected_demand=None) -> dict:
    """One operator choice of ``provider_id`` for a transfer of several
    roots: best effort, never atomic. Each root the provider may take now is
    switched through the one-root command above -- its own operator-control
    boundary, admission, routing-basis check, writer fence and provenance --
    naming the provider the operator saw on it (``expected_routes``, root id
    -> provider); a root the operator did not see as switchable, or whose
    route moved since, is refused by that same command and stays as it is.
    A root that fails never undoes one that switched. Every participating
    root's outcome is reported: ``switched`` (its replacement committed; the
    new provider resolves it next), ``already_selected``, ``ineligible``
    (with its ``transfer_route_status`` reason) or ``refused`` (with the
    switch's normalized reason). If anything other than a refusal stops the
    operation (the transfer was removed, storage became unavailable) before
    any root switched, it is raised as it is; once a root has switched, the
    report is returned instead, the root that stopped it ``refused`` (``gone``
    or ``error``) and every later root ``not_attempted``, so a committed switch
    is never reported as nothing."""
    status = await transfer_route_providers(application, int(transfer_id))
    entry = next((item for item in (status or {}).get("providers", ()) if item["provider_id"] == str(provider_id)),
                 None)
    if status is None:
        raise transfer_choice_refusal(None)
    if entry is None:
        raise transfer_choice_refusal(str(provider_id))
    outcomes = []
    stopped = None
    for root in entry["roots"]:
        outcome = {"root_id": root["root_id"], "previous_provider_id": root["current_provider_id"],
                   "artifact_ids": root["artifact_ids"], "reason": None}
        if stopped is not None:
            outcome.update(outcome="refused", reason="not_attempted")
        elif root["status"] == ROOT_ALREADY_SELECTED:
            outcome["outcome"] = "already_selected"
        elif root["status"] != ROOT_ELIGIBLE:
            outcome.update(outcome="ineligible", reason=root["reason"] or root["status"])
        elif root["root_id"] not in expected_routes:
            # Switchable now, but not when the operator chose: not switched.
            outcome.update(outcome="refused", reason="changed")
        else:
            try:
                await switch_route_provider(application, int(transfer_id), str(provider_id),
                                            expected_provider_id=str(expected_routes[root["root_id"]]),
                                            expected_demand=expected_demand, artifact_id=root["artifact_ids"][0],
                                            expected_root_id=root["root_id"])
                outcome["outcome"] = "switched"
            except TransferError as exc:
                outcome.update(outcome="refused", reason=exc.error.category.value)
            except Exception as exc:
                if not any(item["outcome"] == "switched" for item in outcomes):
                    raise
                stopped = "gone" if isinstance(exc, KeyError) else "error"
                outcome.update(outcome="refused", reason=stopped)
        outcomes.append(outcome)
    counts = {name: sum(item["outcome"] == name for item in outcomes)
              for name in ("switched", "already_selected", "ineligible", "refused")}
    result = ("unchanged" if not counts["switched"]
              else "complete" if counts["switched"] + counts["already_selected"] == len(outcomes) else "partial")
    return {"transfer_id": int(transfer_id), "provider_id": str(provider_id), "result": result,
            "participants": len(outcomes), **counts, "roots": outcomes}
