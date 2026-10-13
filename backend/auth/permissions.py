"""DebridPulse access control: the ONE authorization policy.

Four fixed roles each hold a fixed set of capabilities (``ROLE_CAPABILITIES``).
Every registered HTTP action is classified exactly once, by the endpoint that
serves it, in ``ACTIONS``; ``authorize_request`` -- installed once as the
application's dependency -- evaluates every routed request against it, so an
action nobody classified is denied (``403``), never admitted. Paths that are
not routed actions (the static interface, the API documentation) are
evaluated by the authentication boundary against the same table
(``non_route_capability``).

``401`` means no valid credential; ``403`` means an authenticated principal
whose role does not hold the capability.

Destructive transfer actions additionally require that the caller owns every
outstanding demand the transfer's work serves (``destruction_precondition``):
an Admin may destroy anything; a User only work nobody else still needs. The
demand a User was authorized against travels with the act to the transfer
core, which re-checks it inside the very transaction that commits the act
(``TransferRepository.hold_demand``), so demand arriving in between is never
destroyed.
"""
from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, Request

from auth.accounts import Role
from auth.models import Principal


class Capability(StrEnum):
    PUBLIC = "public"                      # no credential needed
    SESSION = "session"                    # the caller's own session / identity
    INTERFACE = "interface"                # the browser interface itself
    TRANSFERS_READ = "transfers.read"      # Dashboard, Downloads, Details, live stream
    STATISTICS_READ = "statistics.read"
    TRANSFERS_SUBMIT = "transfers.submit"
    TRANSFERS_OPERATE = "transfers.operate"  # pause, resume, retry, queue, priority, input, selection
    TRANSFERS_DESTROY = "transfers.destroy"  # delete, cancel, block, candidate/route switch (+ ownership)
    DEMAND_OWN = "demand.own"              # list and withdraw one's own demand
    DEMAND_ANY = "demand.any"              # list and withdraw anyone's demand
    METRICS_READ = "metrics.read"          # Prometheus scrape
    ADMINISTER = "administer"              # configuration, accounts, maintenance, diagnostics, global controls


_HUMAN = frozenset({Capability.SESSION, Capability.INTERFACE, Capability.TRANSFERS_READ,
                    Capability.STATISTICS_READ})
ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.ADMIN: frozenset(Capability) - {Capability.PUBLIC},
    Role.USER: _HUMAN | {Capability.TRANSFERS_SUBMIT, Capability.TRANSFERS_OPERATE,
                         Capability.TRANSFERS_DESTROY, Capability.DEMAND_OWN},
    Role.READONLY: _HUMAN,
    Role.INTEGRATION: frozenset({Capability.SESSION, Capability.TRANSFERS_SUBMIT, Capability.DEMAND_OWN,
                                 Capability.METRICS_READ}),
}


def capabilities(principal: Principal | None) -> frozenset[Capability]:
    try:
        role = Role(str(getattr(principal, "role", "") or ""))
    except ValueError:
        return frozenset()
    return ROLE_CAPABILITIES[role]


def permits(principal: Principal | None, capability: Capability) -> bool:
    return capability is Capability.PUBLIC or capability in capabilities(principal)


_P, _S, _I = Capability.PUBLIC, Capability.SESSION, Capability.INTERFACE
_R, _ST = Capability.TRANSFERS_READ, Capability.STATISTICS_READ
_SUB, _OP, _DES = Capability.TRANSFERS_SUBMIT, Capability.TRANSFERS_OPERATE, Capability.TRANSFERS_DESTROY
_DEM, _MET, _A = Capability.DEMAND_OWN, Capability.METRICS_READ, Capability.ADMINISTER

# Every routed action, by the endpoint that serves it (``module.function``).
# A route test compares this table with the live route table both ways.
ACTIONS: dict[str, Capability] = {
    # Authentication boundary and the caller's own identity.
    "api.auth_routes.public_auth_status": _P,
    "api.auth_routes.login_page": _P,
    "api.auth_routes.password_login": _P,
    "api.auth_routes.oidc_start": _P,
    "api.auth_routes.oidc_callback": _P,
    "api.auth_config_routes.pending_aware_oidc_callback": _P,
    "api.auth_routes.application_javascript_bundle": _I,
    "api.auth_routes.auth_session_status": _S,
    "api.auth_routes.logout": _S,
    "api.account_routes.change_own_password": _S,
    "api.account_routes.change_own_display_name": _S,
    # Authentication configuration and accounts.
    "api.auth_config_routes.get_authentication_config": _A,
    "api.auth_config_routes.update_authentication_config": _A,
    "api.auth_config_routes.get_oidc_runtime_status": _A,
    "api.auth_config_routes.verify_pending_oidc_configuration": _A,
    "api.account_routes.list_accounts": _A,
    "api.account_routes.create_account": _A,
    "api.account_routes.update_account": _A,
    "api.account_routes.delete_account": _A,
    "api.account_routes.merge_account": _A,
    "api.account_routes.reset_account_password": _A,
    "api.account_routes.link_account_identity": _A,
    "api.account_routes.account_impact": _A,
    "api.account_routes.list_account_tokens": _A,
    "api.account_routes.create_account_token": _A,
    "api.account_routes.revoke_account_token": _A,
    # Submitter demand.
    "api.account_routes.list_requests": _DEM,
    "api.account_routes.withdraw_request": _DEM,
    # Public liveness and installation facts.
    "api.routes.health_check": _P,
    "api.routes.get_version_ep": _P,
    "api.routes.serve_avatar": _P,
    "api.routes.version_check": _I,
    "api.routes.get_changelog": _I,
    "api.settings_validation_routes.get_bundled_legal_document": _I,
    # Transfers: read.
    "api.operational_downloads.list_operational_torrents": _R,
    "api.operational_downloads.read_root_route": _R,
    "api.operational_downloads.preview_artifact_candidate": _R,
    "api.routes.get_torrent": _R,
    "api.routes.torrent_files_preview": _R,
    "api.routes.events_stream": _R,
    "api.file_selection_routes.list_file_selection_offers": _R,
    "api.file_selection_routes.get_file_selection": _R,
    # Statistics.
    "api.routes.get_stats": _ST,
    "api.routes.get_stats_detail": _ST,
    "api.routes.get_comprehensive_stats": _ST,
    "api.routes.list_stats_snapshots": _ST,
    "api.routes.export_stats": _ST,
    "api.routes.get_execution_throughput": _ST,
    "api.routes.prometheus_metrics": _MET,
    # Transfers: submit.
    "api.routes.add_magnet": _SUB,
    "api.routes.add_torrent_file": _SUB,
    "api.routes.add_usenet_file": _SUB,
    "api.routes.add_usenet_link": _SUB,
    "api.routes.add_multimeta_file": _SUB,
    "api.routes.add_debrid_links": _SUB,
    "api.routes.add_link_file": _SUB,
    # Transfers: ordinary operation (any transfer; core behaviour unchanged).
    "api.routes.retry_torrent": _OP,
    "api.routes.pause_torrent": _OP,
    "api.routes.resume_torrent": _OP,
    "api.routes.set_torrent_label": _OP,
    "api.routes.set_torrent_priority": _OP,
    "api.routes.move_to_top": _OP,
    "api.routes.submit_transfer_input": _OP,
    # Quick Add's duplicate preview: an interactive submitter's tool. It reads
    # every transfer, so it is not Integration's, and ReadOnly has no Quick Add.
    "api.routes.check_torrent_duplicate": _OP,
    "api.routes.bulk_action": _OP,  # each target re-evaluated; delete needs TRANSFERS_DESTROY
    "api.file_selection_routes.confirm_file_selection": _OP,
    "api.file_selection_routes.dismiss_file_selection": _OP,
    # Transfers: destructive (ownership of every affected demand required).
    "api.routes.delete_torrent": _DES,
    "api.routes.cancel_torrent": _DES,
    "api.routes.block_file": _DES,
    "api.operational_downloads.activate_artifact_candidate": _DES,
    "api.operational_downloads.replace_root_route": _DES,
    # Everything below is Admin: configuration, services and engines,
    # diagnostics, the Activity Log, global and executor-level controls,
    # backups and maintenance.
    "api.operational_downloads.count_activity_events": _A,
    "api.operational_downloads.list_activity_events": _A,
    "api.executor_work.list_executor_work": _A,
    "api.executor_work.control_executor_work": _A,
    "api.storage_health_routes.storage_health": _A,
    "api.settings_validation_routes.get_extraction_passwords": _A,
    "api.settings_validation_routes.browse_directories": _A,
    "api.settings_validation_routes.get_alldebrid_runtime_status": _A,
    "api.settings_validation_routes.validate_alldebrid": _A,
    "api.settings_validation_routes.get_realdebrid_runtime_status": _A,
    "api.settings_validation_routes.validate_realdebrid": _A,
    "api.settings_validation_routes.get_realdebrid_authorization": _A,
    "api.settings_validation_routes.start_realdebrid_authorization": _A,
    "api.settings_validation_routes.poll_realdebrid_authorization": _A,
    "api.settings_validation_routes.cancel_realdebrid_authorization": _A,
    "api.settings_validation_routes.disconnect_realdebrid": _A,
    "api.settings_validation_routes.get_torbox_runtime_status": _A,
    "api.settings_validation_routes.validate_torbox": _A,
    "api.settings_validation_routes.get_torbox_authorization": _A,
    "api.settings_validation_routes.start_torbox_authorization": _A,
    "api.settings_validation_routes.poll_torbox_authorization": _A,
    "api.settings_validation_routes.cancel_torbox_authorization": _A,
    "api.settings_validation_routes.disconnect_torbox": _A,
    "api.settings_validation_routes.get_debridlink_runtime_status": _A,
    "api.settings_validation_routes.validate_debridlink": _A,
    "api.settings_validation_routes.get_premiumize_runtime_status": _A,
    "api.settings_validation_routes.validate_premiumize": _A,
    "api.settings_validation_routes.validate_discord": _A,
    "api.settings_validation_routes.send_statistics_report": _A,
    "api.settings_validation_routes.get_media_runtime_status": _A,
    "api.settings_validation_routes.get_usenet_runtime_status": _A,
    "api.settings_validation_routes.test_usenet_server": _A,
    "api.settings_validation_routes.test_usenet_integration": _A,
    "api.settings_validation_routes.get_usenet_configuration_drift": _A,
    "api.routes.get_settings_ep": _A,
    "api.routes.update_settings": _A,
    "api.routes.upload_avatar": _A,
    "api.routes.test_alldebrid": _A,
    "api.routes.test_aria2": _A,
    "api.routes.run_aria2_housekeeping_ep": _A,
    "api.routes.aria2_runtime_status": _A,
    "api.routes.aria2_global_stat": _A,
    "api.routes.aria2_runtime_start": _A,
    "api.routes.aria2_runtime_stop": _A,
    "api.routes.aria2_runtime_restart": _A,
    "api.routes.aria2_runtime_apply": _A,
    "api.routes.aria2_downloads": _A,
    "api.routes.aria2_download_action": _A,
    "api.routes.aria2_get_global_options": _A,
    "api.routes.aria2_set_global_options": _A,
    "api.routes.import_existing": _A,
    "api.routes.diagnose_torrents": _A,
    "api.routes.recover_all_ready": _A,
    "api.routes.run_recovery": _A,
    "api.routes.export_transfer_trace": _A,
    "api.routes.performance_diagnostics": _A,
    "api.routes.pause_processing": _A,
    "api.routes.resume_processing": _A,
    "api.routes.trigger_backup": _A,
    "api.routes.list_backups": _A,
    "api.routes.add_backup": _A,
    "api.routes.save_backup": _A,
    "api.routes.remove_backup": _A,
    "api.routes.restore_backup": _A,
    "api.routes.drop_page_cache_ep": _A,
    "api.routes.memory_info_ep": _A,
    "api.routes.wipe_database_admin": _A,
    "api.routes.get_execution_runtime_limits": _A,
    "api.routes.patch_execution_runtime_limits": _A,
    "api.routes.get_execution_runtime_status": _A,
    "api.routes.get_transfer_policy_ep": _A,
    "api.routes.patch_transfer_policy": _A,
    "api.routes.get_integration_configuration": _A,
    "api.routes.patch_integration_configuration": _A,
    "api.routes.list_integration_groups": _A,
    "api.routes.patch_integration_group_configuration": _A,
    "api.routes.get_stats_report": _A,
    "api.routes.send_stats_report_ep": _A,
    "api.routes.trigger_stats_snapshot": _A,
    "api.routes.trigger_full_sync": _A,
    "api.routes.trigger_deep_sync": _A,
    "api.routes.sse_subscriber_count": _A,
    "api.routes.disk_guard_status": _A,
    "api.routes.cleanup_alldebrid_orphans_endpoint": _A,
    "api.routes.create_usenet_server": _A,
    "api.routes.update_usenet_server": _A,
    "api.routes.delete_usenet_server": _A,
}

# Served paths that are not routed actions.
_DOCUMENTATION_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def action_name(endpoint) -> str:
    return f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__name__', '')}"


def non_route_capability(path: str) -> Capability:
    """The capability a request needs for a path no routed action serves."""
    return Capability.ADMINISTER if str(path or "") in _DOCUMENTATION_PATHS else Capability.INTERFACE


def denial(principal: Principal | None) -> HTTPException:
    if principal is None or not principal.role:
        return HTTPException(status_code=401, detail="Unauthorized")
    return HTTPException(status_code=403, detail="Forbidden")


async def authorize_request(request: Request) -> None:
    """The application dependency: every routed request, against ``ACTIONS``."""
    capability = ACTIONS.get(action_name(request.scope.get("endpoint")))
    principal = getattr(request.state, "principal", None)
    if capability is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not permits(principal, capability):
        raise denial(principal)


async def demand_owner(principal: Principal, account_id: str | None) -> bool:
    """Whether ``principal`` owns demand recorded for ``account_id``: the same
    account, or an account since merged into it. Unattributed and legacy
    demand belongs to nobody."""
    if not account_id or not principal.account_id:
        return False
    if account_id == principal.account_id:
        return True
    from auth.accounts import canonical_account_id

    return await canonical_account_id(account_id) == principal.account_id


async def destruction_precondition(principal: Principal, repository, transfer_id: int):
    """Whether ``principal`` may destroy or redirect one transfer's work, and
    the outstanding demand it is authorized against.

    Requires ``TRANSFERS_DESTROY`` and, unless Admin, ownership of every
    outstanding demand that work serves -- its own and every transfer
    consolidated into its artifacts (nobody's demand outstanding means nobody
    is deprived). Returns ``(permitted, expected_demand)``: ``None`` for an
    Admin's unconditional act, otherwise the demand IDs the act must still
    find, and no more, when it commits."""
    if not permits(principal, Capability.TRANSFERS_DESTROY):
        return False, None
    if principal.role == Role.ADMIN.value:
        return True, None
    rows = await repository.outstanding_demand(int(transfer_id))
    for row in rows:
        if not await demand_owner(principal, row.get("account_id")):
            return False, None
    return True, frozenset(str(row["id"]) for row in rows)


async def destruction_permitted(principal: Principal, repository, transfer_id: int) -> bool:
    return (await destruction_precondition(principal, repository, transfer_id))[0]


async def require_destruction(request: Request, repository, transfer_id: int):
    """The demand a destructive route is authorized against, or ``403``."""
    principal = getattr(request.state, "principal", None)
    permitted, expected = (await destruction_precondition(principal, repository, transfer_id)
                           if principal is not None else (False, None))
    if not permitted:
        raise HTTPException(
            status_code=403,
            detail="Another account still needs this transfer; withdraw your own request instead",
        )
    return expected
