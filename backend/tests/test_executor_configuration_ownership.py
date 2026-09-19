"""DP 1.0.12 canonical architecture correction, Workstream C.

Concrete aria2 configuration ownership (specification sections 4.3, 9.1,
9.2, 9.3): ``integrations.aria2`` (``executors.aria2.definition.Aria2Options``)
is the canonical schema owner of aria2-native tuning; ``core.config
.AppSettings``'s flat ``aria2_*`` fields survive only as one-way migration
input, translated by the SAME generic ``legacy_fields``-driven mechanism
every other integration already uses (``integrations.configuration
.normalize_settings``) -- no new translation code was needed for this
correction, only additional fields on the existing typed schema. Runtime/
admin code (``executors.aria2.runtime.aria2_global_options``) rebuilds
native options from that canonical schema, never from the flat fields
(covered architecturally by ``test_canonical_runtime_architecture.py``;
this file proves the same thing functionally, plus the migration itself).

Existing coverage this file does not duplicate: scoped API mutation
(``test_settings_namespace_mutation.py``), the neutral bandwidth capability
(``test_executor_runtime_limits.py``).
"""
from core.config import AppSettings
from executors.aria2.definition import Aria2Options, definition as aria2_definition
from executors.aria2.runtime import _canonical_aria2_options, build_aria2_global_options
from integrations.configuration import migrate_legacy_settings, normalize_settings
from integrations.definition import IntegrationSettings
from providers.alldebrid.definition import definition as alldebrid_definition
from providers.general_http.definition import definition as general_http_definition
from transfers.runtime_limits import ExecutionRuntimeLimits
from transfers.settings import TransferSettings


DEFINITIONS = (alldebrid_definition, general_http_definition, aria2_definition)


def aria2_global_options(cfg):
    """Native option dict for the canonical namespaces of ``cfg``."""
    return build_aria2_global_options(
        _canonical_aria2_options(cfg),
        cfg.transfer_policy.max_concurrent_executions,
        cfg.execution_runtime_limits.max_download_bytes_per_second,
    )


def test_aria2_options_owns_every_specification_section_5_tuning_field():
    """Specification section 4.3's explicit field list must all be present on
    the canonical typed schema -- including disk_cache/file_allocation/
    lowest_speed_limit, which predate this correction as flat-only fields."""
    fields = set(Aria2Options.model_fields)
    for required in (
        "mode", "url", "secret", "download_path",
        "split", "min_split_size", "max_connection_per_server", "continue_downloads",
        "disk_cache", "file_allocation", "lowest_speed_limit",
    ):
        assert required in fields, f"Aria2Options is missing canonical field {required!r}"


def test_aria2_options_owns_every_remaining_operational_field():
    """Gate 9 rejection follow-up: every remaining flat aria2-specific
    operational setting must be reconciled against the canonical executor/
    integration ownership rule -- moved here, with no field left owned by
    ``AppSettings``."""
    fields = set(Aria2Options.model_fields)
    for required in (
        "max_upload_limit", "builtin_auto_start", "builtin_log_file",
        "builtin_log_max_mb", "builtin_log_backups", "builtin_session_file",
        "purge_interval_minutes", "max_download_result",
        "keep_unfinished_download_result", "deep_sync_interval_minutes",
        "restart_interval_hours",
    ):
        assert required in fields, f"Aria2Options is missing canonical field {required!r}"


def test_legacy_fields_are_auto_derived_not_hand_maintained():
    """The ``aria2_<field>`` legacy mapping is generated from
    ``Aria2Options.model_fields`` itself (``executors/aria2/definition.py``),
    so adding a canonical field automatically wires its one-way migration --
    there is no second, hand-maintained translation table to fall out of
    sync (specification section 2.1's "no second source of truth")."""
    legacy_map = dict(aria2_definition.legacy_fields)
    for field in Aria2Options.model_fields:
        assert legacy_map.get("aria2_" + field) == field


def _load_legacy(raw):
    """Translate a raw pre-canonical mapping exactly as ``load_settings`` does."""
    raw = dict(raw)
    migrate_legacy_settings(raw, DEFINITIONS)
    return normalize_settings(AppSettings(**{k: v for k, v in raw.items() if k in AppSettings.model_fields}), DEFINITIONS)


def test_legacy_flat_fields_migrate_into_canonical_integration_namespace():
    """Specification section 9.2: legacy flat settings migrate into the
    canonical ``integrations.aria2`` namespace; canonical tuning values are
    correct, not defaults."""
    migrated = _load_legacy({
        "aria2_split": 32, "aria2_min_split_size": "20M", "aria2_max_connection_per_server": 4,
        "aria2_continue_downloads": False, "aria2_disk_cache": "128M", "aria2_file_allocation": "none",
        "aria2_lowest_speed_limit": "10K", "aria2_mode": "external", "aria2_url": "http://host:6800/jsonrpc",
        "aria2_secret": "s3cr3t", "aria2_download_path": "/mnt/downloads",
        "aria2_max_upload_limit": 1_000_000, "aria2_builtin_auto_start": False,
        "aria2_builtin_log_file": "/data/aria2/custom.log", "aria2_builtin_log_max_mb": 50,
        "aria2_builtin_log_backups": 7, "aria2_builtin_session_file": "/data/aria2/custom.session",
        "aria2_purge_interval_minutes": 15, "aria2_max_download_result": 200,
        "aria2_keep_unfinished_download_result": True, "aria2_deep_sync_interval_minutes": 30,
        "aria2_restart_interval_hours": 12,
    })
    options = migrated.integrations["aria2"].options
    assert options["split"] == 32
    assert options["min_split_size"] == "20M"
    assert options["max_connection_per_server"] == 4
    assert options["continue_downloads"] is False
    assert options["disk_cache"] == "128M"
    assert options["file_allocation"] == "none"
    assert options["lowest_speed_limit"] == "10K"
    assert options["mode"] == "external"
    assert options["url"] == "http://host:6800/jsonrpc"
    assert options["secret"] == "s3cr3t"
    assert options["max_upload_limit"] == 1_000_000
    assert options["builtin_auto_start"] is False
    assert options["builtin_log_file"] == "/data/aria2/custom.log"
    assert options["builtin_log_max_mb"] == 50
    assert options["builtin_log_backups"] == 7
    assert options["builtin_session_file"] == "/data/aria2/custom.session"
    assert options["purge_interval_minutes"] == 15
    assert options["max_download_result"] == 200
    assert options["keep_unfinished_download_result"] is True
    assert options["deep_sync_interval_minutes"] == 30
    assert options["restart_interval_hours"] == 12
    assert options["download_path"] == "/mnt/downloads"


def test_builtin_legacy_tuning_left_at_an_older_default_is_upgraded_but_external_is_not():
    builtin = _load_legacy({"aria2_mode": "builtin", "aria2_split": 8, "aria2_max_connection_per_server": 4})
    assert builtin.integrations["aria2"].options["split"] == 16
    assert builtin.integrations["aria2"].options["max_connection_per_server"] == 16
    external = _load_legacy({"aria2_mode": "external", "aria2_split": 8})
    assert external.integrations["aria2"].options["split"] == 8


def test_unknown_legacy_aria2_mode_becomes_the_conservative_external_mode():
    assert _load_legacy({"aria2_mode": "legacy-garbage"}).integrations["aria2"].options["mode"] == "external"


def test_migration_is_idempotent():
    once = _load_legacy({"aria2_split": 32, "aria2_disk_cache": "128M"})
    twice = normalize_settings(once, DEFINITIONS, previous=once)
    assert once.integrations["aria2"].options["split"] == 32
    assert twice.integrations["aria2"].options["split"] == 32
    assert once.integrations["aria2"].options == twice.integrations["aria2"].options


def test_settings_model_has_no_flat_aria2_field_to_regenerate():
    """Specification section 9.2: the canonical namespace is authoritative on
    every save, and the flat legacy names are not even fields of the settings
    document, so nothing can regenerate them as a second persisted truth."""
    assert not [name for name in AppSettings.model_fields if name.startswith("aria2_")]
    current = AppSettings(integrations={"aria2": IntegrationSettings(options={"split": 32})})
    saved = normalize_settings(current, DEFINITIONS, previous=current).model_dump()
    assert saved["integrations"]["aria2"]["options"]["split"] == 32
    assert not [name for name in saved if name.startswith("aria2_")]


def test_aria2_global_options_sources_native_tuning_from_canonical_namespace():
    """Functional counterpart to the architecture-level source grep in
    ``test_canonical_runtime_architecture.py``: the native option VALUES
    themselves come from ``integrations.aria2``/``transfer_policy``/
    ``execution_runtime_limits``, not from flat fields, even when the flat
    fields disagree."""
    cfg = AppSettings(
        integrations={"aria2": IntegrationSettings(options={
            "split": 3, "min_split_size": "1M", "max_connection_per_server": 2,
            "continue_downloads": False, "disk_cache": "7M", "file_allocation": "none",
            "lowest_speed_limit": "5K", "max_download_result": 77,
            "keep_unfinished_download_result": True, "max_upload_limit": 555,
        })},
        transfer_policy=TransferSettings(max_concurrent_executions=11),
        execution_runtime_limits=ExecutionRuntimeLimits(max_download_bytes_per_second=4321),
    )
    options = aria2_global_options(cfg)
    assert options["split"] == "3"
    assert options["min-split-size"] == "1M"
    assert options["max-connection-per-server"] == "2"
    assert options["continue"] == "false"
    assert options["disk-cache"] == "7M"
    assert options["file-allocation"] == "none"
    assert options["lowest-speed-limit"] == "5K"
    assert options["max-concurrent-downloads"] == "11"
    assert options["max-overall-download-limit"] == "4321"
    assert options["max-download-result"] == "77"
    assert options["keep-unfinished-download-result"] == "true"
    assert options["max-overall-upload-limit"] == "555"


def test_aria2_global_options_falls_back_to_typed_defaults_when_namespace_absent():
    """A settings document with no ``integrations.aria2`` entry at all (a
    freshly-constructed ``AppSettings()`` before first normalization) must
    still produce a usable option set from ``Aria2Options``' own defaults,
    never crash."""
    options = aria2_global_options(AppSettings())
    assert options["split"] == "16"
    assert options["max-connection-per-server"] == "16"
    assert options["disk-cache"] == "64M"
