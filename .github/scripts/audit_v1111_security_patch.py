from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:120]!r}")
    if text.count(old) != 1:
        raise SystemExit(f"non-unique patch anchor in {path}: {text.count(old)} matches")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# SEC-001: the existing canonical per-addUri builder owns metadata-follow policy.
replace_once(
    "backend/services/manager_v2.py",
    '            "auto-file-renaming": "false",\n            # The application validates the initial public destination. Do not\n',
    '            "auto-file-renaming": "false",\n'
    '            # AllDebrid owns torrent/metalink processing. aria2 is only the\n'
    '            # file-transfer stage; never let a provider HTTP response spawn\n'
    '            # metadata-follow children outside the DebridPulse ownership ledger.\n'
    '            "follow-torrent": "false",\n'
    '            "follow-metalink": "false",\n'
    '            # The application validates the initial public destination. Do not\n',
)

# Built-in daemon keeps the same invariant globally as defense in depth.
replace_once(
    "backend/services/aria2_runtime.py",
    '            "follow-torrent": "false",\n            "enable-dht": "false",\n',
    '            "follow-torrent": "false",\n'
    '            "follow-metalink": "false",\n'
    '            "enable-dht": "false",\n',
)

# SEC-002: retain the existing preflight but bind the actual downloader connection
# to the guarded CONNECT proxy using per-job options. This applies to every
# ensure_download path, including reconciliation/error recovery.
replace_once(
    "backend/services/transfer_runtime_guard.py",
    'from services.aria2_runtime import effective_rpc_config\n',
    'from services.aria2_runtime import effective_rpc_config, is_builtin_mode\n'
    'from services.downloader_egress_guard import downloader_egress_guard\n',
)
replace_once(
    "backend/services/transfer_runtime_guard.py",
    'class GuardedTransferIntegrityAria2Service(TransferIntegrityAria2Service):\n'
    '    """Apply destination-network policy immediately before aria2 dispatch."""\n\n'
    '    async def ensure_download(self, uri: str, *args, **kwargs) -> str:\n'
    '        validated = await validate_resolved_public_destination(uri)\n'
    '        return await super().ensure_download(validated, *args, **kwargs)\n',
    'class GuardedTransferIntegrityAria2Service(TransferIntegrityAria2Service):\n'
    '    """Bind every owned aria2 connection to the guarded egress boundary."""\n\n'
    '    async def ensure_download(self, uri: str, options=None, *args, **kwargs) -> str:\n'
    '        # Keep the early resolution check as defense in depth, but do not rely\n'
    '        # on it for connection authorization: aria2 would otherwise resolve the\n'
    '        # hostname again later and re-open the DNS-rebinding race.\n'
    '        validated = await validate_resolved_public_destination(uri)\n'
    '        await downloader_egress_guard.ensure_started()\n'
    '        guarded_options = dict(options or {})\n'
    '        guarded_options.update(\n'
    '            downloader_egress_guard.job_options(\n'
    '                validated,\n'
    '                external=not is_builtin_mode(),\n'
    '            )\n'
    '        )\n'
    '        return await super().ensure_download(\n'
    '            validated,\n'
    '            guarded_options,\n'
    '            *args,\n'
    '            **kwargs,\n'
    '        )\n',
)

print("security source patches applied")
