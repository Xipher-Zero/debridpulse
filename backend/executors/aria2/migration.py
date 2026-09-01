"""Decode the persisted v1 executor identity without contacting the daemon."""
from transfers.models import ExecutionHandle


def legacy_handle(row, attempt_id, candidate=None):
    gid = str(row.get("download_id") or "").strip()
    if not gid:
        return None
    redactions = [endpoint.address for endpoint in candidate.endpoints] if candidate else []
    return ExecutionHandle("aria2", {"gid": gid, "target": str(row.get("local_path") or ""), "redactions": redactions}, attempt_id)
