"""Decode the persisted v1 executor identity without contacting the daemon."""
from transfers.models import ExecutionHandle


def legacy_handle(row, attempt_id, candidate=None):
    gid = str(row.get("download_id") or "").strip()
    if not gid:
        return None
    redactions = [endpoint.address for endpoint in candidate.endpoints] if candidate else []
    from core.config import get_settings
    from executors.aria2.executor import Aria2Configuration, execution_binding
    from executors.aria2.runtime import effective_rpc_config
    settings = get_settings()
    url, _secret = effective_rpc_config(settings)
    external = settings.aria2_mode == "external"
    configuration = Aria2Configuration(settings.download_folder, settings.aria2_download_path if external else "", external)
    return ExecutionHandle("aria2", {"gid": gid, "target": str(row.get("local_path") or ""), "redactions": redactions,
                                   "binding": execution_binding(configuration, url)}, attempt_id)
