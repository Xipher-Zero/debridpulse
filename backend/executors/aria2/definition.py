"""aria2 registration and executor-owned settings schema."""
import logging
from typing import Literal
from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition

logger = logging.getLogger("debridpulse.config")


class Aria2Options(BaseModel):
    mode: Literal["builtin", "external"] = "builtin"
    url: str = "http://127.0.0.1:6800/jsonrpc"
    secret: str = Field(default="", repr=False)
    builtin_port: int = Field(default=6800, ge=1, le=65535)
    download_path: str = ""
    operation_timeout_seconds: int = Field(default=15, ge=5, le=300)
    split: int = Field(default=16, ge=1, le=64)
    min_split_size: str = "10M"
    max_connection_per_server: int = Field(default=16, ge=1, le=32)
    continue_downloads: bool = True
    disk_cache: str = "64M"
    file_allocation: str = "falloc"
    lowest_speed_limit: str = "0"
    waiting_window: int = Field(default=100, ge=10, le=1000)
    stopped_window: int = Field(default=100, ge=10, le=1000)
    # Remaining operational aria2 fields (DP 1.0.12 canonical architecture
    # correction, Workstream C rejection follow-up): these were previously
    # left as flat `AppSettings` fields read directly by `runtime.py`/
    # `admin.py`. Every aria2-specific tuning/lifecycle/administration field
    # belongs to this executor-owned schema, not universal core -- there is
    # no field-by-field justification for leaving any of them on
    # `AppSettings` as an authority. Bounds mirror `core.config_validator`'s
    # `numeric_bounds` for the equivalent legacy flat field.
    max_upload_limit: int = Field(default=0, ge=0)
    builtin_auto_start: bool = True
    builtin_log_file: str = "/app/data/aria2/aria2.log"
    builtin_log_max_mb: int = Field(default=25, ge=1, le=1024)
    builtin_log_backups: int = Field(default=3, ge=0, le=20)
    builtin_session_file: str = "/app/data/aria2/aria2.session"
    purge_interval_minutes: int = Field(default=5, ge=0, le=1440)
    max_download_result: int = Field(default=20, ge=10, le=5000)
    keep_unfinished_download_result: bool = False
    deep_sync_interval_minutes: int = Field(default=10, ge=0, le=1440)
    restart_interval_hours: float = Field(default=0, ge=0)


def _upgrade_legacy_options(legacy: dict, existing: dict) -> dict:
    """Adjust option values taken from pre-canonical flat configuration.

    Applied only to legacy input, never to values the canonical namespace
    already holds: an unknown mode is treated as ``external`` (the mode that
    never mutates a daemon DebridPulse does not own), and built-in tuning left
    at an older default is raised to the current default.
    """
    upgraded = dict(legacy)
    mode = upgraded.get("mode", existing.get("mode", "builtin"))
    if mode not in ("builtin", "external"):
        logger.warning("Config migration: unknown aria2 mode %r treated as external", mode)
        mode = upgraded["mode"] = "external"
    if mode == "builtin":
        for option in ("split", "max_connection_per_server"):
            if upgraded.get(option) in (4, 8):
                logger.info("Config migration: aria2 %s %s -> 16 (performance upgrade)", option, upgraded[option])
                upgraded[option] = 16
    return upgraded


def build(options, environment):
    from executors.aria2.client import Aria2Service
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from executors.aria2.runtime import _effective_rpc_config
    # Derived entirely from the injected typed `options` -- never a global
    # settings lookup (specification section 9.3): the builtin RPC URL/secret
    # are a pure function of `options.builtin_port`.
    url, secret = _effective_rpc_config(options)
    # Daemon ownership is decided here, once, from the canonical options and
    # injected; the client never consults global settings.
    client = Aria2Service(url, secret, options.operation_timeout_seconds, owns_daemon=options.mode == "builtin")
    configuration = Aria2Configuration(
        environment.download_root, options.download_path if options.mode == "external" else "", options.mode == "external",
        options.split, options.min_split_size, options.max_connection_per_server, options.continue_downloads,
        waiting_window=options.waiting_window, stopped_window=options.stopped_window,
        secrets=(secret,),
    )
    return Aria2Executor(client, configuration, environment.repository.authorize_execution)


definition = IntegrationDefinition(
    "aria2", "executor", "aria2", Aria2Options, build,
    secret_fields=frozenset({"secret"}),
    legacy_fields=tuple(("aria2_" + field, field) for field in Aria2Options.model_fields),
    legacy_upgrade=_upgrade_legacy_options,
    ownership_fields=frozenset({"mode", "url", "builtin_port", "download_path"}),
)
