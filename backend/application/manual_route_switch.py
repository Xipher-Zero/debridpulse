"""Application command and read for an operator's root provider route."""
from __future__ import annotations

from application.manual_candidate_failover import switch_candidate
from transfers.errors import TransferError
from transfers.manual_failover import DiscardConfirmationRequired
from transfers.manual_route_switch import (
    ROOT_ALREADY_SELECTED,
    ROOT_ELIGIBLE,
    VIA_ALTERNATE,
    VIA_ROUTE,
    route_status,
    switch_root_provider,
    transfer_choice_refusal,
    transfer_route_status,
)


async def switch_route_provider(application, transfer_id: int, provider_id: str, *, expected_provider_id: str,
                                expected_demand=None, artifact_id: int | None = None,
                                expected_root_id: str | None = None, source: tuple[int, str] | None = None) -> dict:
    # Inside the one operator pause-control boundary: the switch's temporary
    # writer fence can never clear a Pause or Pause All issued meanwhile.
    async with application.operator_controls, application.application_operation():
        await application.require(int(transfer_id))
        result = await switch_root_provider(application.engine, int(transfer_id), str(provider_id),
                                            expected_provider_id=str(expected_provider_id),
                                            expected_demand=expected_demand, artifact_id=artifact_id,
                                            expected_root_id=expected_root_id, source=source)
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


def committed(outcomes) -> bool:
    """Whether any participant reported so far changed anything durable: a
    root switched, or a file was switched or given a pending provider choice
    (``changed``)."""
    return any(item["outcome"] in {"switched", "pending"}
               or any(entry.get("changed") for entry in item.get("files", ())) for item in outcomes)


async def _switch_through_equivalents(application, transfer_id: int, provider_id: str, plan, *,
                                     expected_demand=None) -> tuple[list[dict], str | None]:
    """One participant's files moved to ``provider_id`` through other
    proven-equivalent sources (``transfers.manual_route_switch.equivalent_plan``),
    file by file, each through ONE existing command so that no step commits
    anything but the requested provider:

    * ``source`` -- the provider already serves that equivalent source: the
      operator candidate switch selects it (its claim, writer retirement,
      provenance and discard rule; never confirmed here, so a discard refuses);
    * ``alternate`` -- the one-root switch of the root that issued it, with
      the file's pending provider choice (``switch_root_provider``'s
      ``source``; ``transfers.provider_choice``): that root's route moves to
      the provider and the file keeps its committed source, writer and
      recovery until the provider's copy lands and is selected for it.

    Each file reports ``switched`` (the provider is its selected one now),
    ``pending`` (accepted: a pending choice, ``choice_id``; never reported as
    switched) or ``refused`` (with its reason; nothing changed), and
    ``changed`` -- whether anything durable changed for it.

    Returns ``(outcomes, stopped)``: anything other than a refusal stopping
    the operation once something changed is reported (``stopped``: ``gone``
    or ``error``; the remaining files ``not_attempted``); before that it is
    raised as it is."""
    outcomes, stopped = [], None
    for step in plan:
        outcome = {"artifact_id": step["artifact_id"], "via": step["via"], "via_root_id": step["via_root_id"],
                   "outcome": "switched", "reason": None, "changed": False}
        if stopped is not None:
            outcome.update(outcome="refused", reason="not_attempted")
            outcomes.append(outcome)
            continue
        try:
            if step["via"] == VIA_ALTERNATE:
                moved = await switch_route_provider(
                    application, int(transfer_id), str(provider_id),
                    expected_provider_id=str(step["via_provider_id"]), expected_demand=expected_demand,
                    expected_root_id=str(step["via_root_id"]),
                    source=(int(step["artifact_id"]), str(step["candidate_id"])))
                outcome.update(outcome="pending", changed=True, choice_id=moved.get("pending_choice_id"))
            else:
                await switch_candidate(application, int(transfer_id), int(step["artifact_id"]),
                                       str(step["candidate_id"]), expected_demand=expected_demand)
                outcome["changed"] = True
        except DiscardConfirmationRequired:
            outcome.update(outcome="refused", reason="discard_confirmation_required")
        except TransferError as exc:
            outcome.update(outcome="refused", reason=exc.error.category.value)
        except Exception as exc:
            if not any(item["changed"] for item in outcomes):
                raise
            stopped = "gone" if isinstance(exc, KeyError) else "error"
            outcome.update(outcome="refused", reason=stopped)
        outcomes.append(outcome)
    return outcomes, stopped


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
    is never reported as nothing.

    A participant whose own link the provider cannot take but whose files it
    may reach through other proven-equivalent sources (``via`` ``source`` or
    ``alternate``) is moved file by file (``_switch_through_equivalents``);
    its ``files`` report each file, and it counts as ``switched`` only when
    every one of them did (otherwise ``refused`` with the first reason, its
    switched files still reported; the result is then never ``complete``).
    A participant whose files were accepted but await the provider's copy is
    ``pending`` (counted apart, never as switched). A refused file is left
    exactly as it was. ``unchanged`` is reported only when nothing durable
    changed at all; ``pending`` when every participant switched, already had
    the provider or is pending, and at least one is pending."""
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
        elif root.get("via", VIA_ROUTE) != VIA_ROUTE:
            if str(expected_routes[root["root_id"]]) != str(root["current_provider_id"]):
                outcome.update(outcome="refused", reason="changed")
            else:
                try:
                    files, stopped = await _switch_through_equivalents(
                        application, int(transfer_id), str(provider_id), root["plan"],
                        expected_demand=expected_demand)
                except Exception:
                    if not committed(outcomes):
                        raise
                    stopped = "error"
                    files = []
                outcome["files"] = files
                failed = next((item for item in files if item["outcome"] not in {"switched", "pending"}), None)
                if files and failed is None:
                    outcome["outcome"] = ("pending" if any(item["outcome"] == "pending" for item in files)
                                          else "switched")
                else:
                    outcome.update(outcome="refused", reason=(failed or {}).get("reason") or stopped)
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
                if not committed(outcomes):
                    raise
                stopped = "gone" if isinstance(exc, KeyError) else "error"
                outcome.update(outcome="refused", reason=stopped)
        outcomes.append(outcome)
    counts = {name: sum(item["outcome"] == name for item in outcomes)
              for name in ("switched", "pending", "already_selected", "ineligible", "refused")}
    settled = counts["switched"] + counts["already_selected"]
    result = ("unchanged" if not committed(outcomes)
              else "complete" if settled == len(outcomes)
              else "pending" if settled + counts["pending"] == len(outcomes) else "partial")
    return {"transfer_id": int(transfer_id), "provider_id": str(provider_id), "result": result,
            "participants": len(outcomes), **counts, "roots": outcomes}
