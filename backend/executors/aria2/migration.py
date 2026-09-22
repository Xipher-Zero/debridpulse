"""Decode the persisted v1 executor identity without contacting the daemon."""
from transfers.models import ExecutionHandle


def legacy_handle(row, attempt_id, candidate=None):
    gid = str(row.get("download_id") or "").strip()
    if not gid:
        return None
    redactions = [endpoint.address for endpoint in candidate.endpoints] if candidate else []
    from core.config import get_settings
    from executors.aria2.executor import execution_binding
    from executors.aria2.runtime import rpc_url
    binding = execution_binding(get_settings().download_folder, rpc_url())
    return ExecutionHandle("aria2", {"gid": gid, "target": str(row.get("local_path") or ""), "redactions": redactions,
                                   "binding": binding}, attempt_id)
