"""*arr compatibility: the translation every download-client dialect shares.

A dialect (``api.arr_sabnzbd``) owns only wire syntax. What a client's item
*is* -- which logical job, whether it is finished, where its output lies, what
removing it means -- is decided here, from the native external contract
(``external.service.ExternalClients``) alone. Nothing here admits, schedules,
deletes or owns anything: no table, no queue, no worker.
"""
from __future__ import annotations

import posixpath

from external.service import ExternalClients, ExternalRefused

# The protocol endpoints whose clients can carry the Integration token only as
# an ``apikey`` query parameter. The authentication boundary verifies that key
# exactly as a Bearer token, and only on these paths.
QUERY_KEY_PATHS = frozenset({"/api/sabnzbd/api"})


def output_location(delivery: dict) -> tuple[str, int] | None:
    """Where a client imports a completed job from, and its total size: the
    single delivered file, or the one folder that holds every delivered file.

    Only an ``available`` delivery has an output. A folder must lie strictly
    beneath the profile's import root -- the import root itself is shared by
    every job delivered through the profile, so offering it would hand the
    client other jobs' files. Such a delivery has no single-location form
    (``None``)."""
    if delivery.get("state") != "available" or not delivery.get("files"):
        return None
    paths = [str(item["import_path"]) for item in delivery["files"]]
    size = int(delivery.get("total_bytes") or 0)
    if len(paths) == 1:
        return paths[0], size
    folder = posixpath.commonpath(paths)
    root = posixpath.normpath(str(delivery.get("import_root") or "/"))
    if folder == root or not folder.startswith(root.rstrip("/") + "/"):
        return None
    return folder, size


async def remove(external: ExternalClients, principal, job_id: str) -> str:
    """A client's "remove this item", decided by the job's own condition
    (never by a dialect flag):

    * completed with a committed delivery -> acknowledge that generation: the
      client's interest is satisfied; DebridPulse's transfer and history stay;
    * anything else -> withdraw the job's own demand: the core cancels the
      work only if no other demand still needs it (atomically, at its own
      commit), and leaves shared work running;
    * already withdrawn -> nothing.

    Another client's or an unknown job is ``job_not_found``."""
    view = await external.job(principal, job_id)
    if view["phase"] == "withdrawn":
        return "already_withdrawn"
    delivery = view["delivery"]
    if view["phase"] == "completed" and delivery.get("generation") is not None:
        try:
            await external.acknowledge(principal, job_id, {"delivery_id": delivery["delivery_id"],
                                                           "generation": delivery["generation"]})
        except ExternalRefused as exc:
            if exc.code != "stale_generation":
                raise
            # A newer generation was committed meanwhile: the removal is of
            # the item as it stands now.
            await external.acknowledge(principal, job_id, {"delivery_id": delivery["delivery_id"],
                                                           "generation": int(exc.extra["current_generation"])})
        return "acknowledged"
    await external.withdraw(principal, job_id)
    return "withdrawn"
