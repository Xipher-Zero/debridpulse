"""aria2 registration and executor-owned settings schema."""
import logging
import posixpath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations.definition import IntegrationDefinition

logger = logging.getLogger("debridpulse.config")


# The one size grammar the packaged aria2 (1.37.0) parses for its byte and
# byte-rate options: a whole number with an optional K or M suffix (1K = 1024,
# 1M = 1024K). It refuses G, fractions, signs and anything else -- "18EGGS",
# "1.5M", "1G" and "-1" all make the daemon exit 28 at startup.
_SIZE = re.compile(r"([0-9]{1,19})([KkMm]?)")
_UNITS = {"": 1, "k": 1024, "m": 1024 * 1024}
_MIB = 1024 * 1024
# aria2 holds a size in a signed 64-bit integer: a value that, with its suffix,
# exceeds this overflows and the daemon exits 28.
_ARIA2_SIZE_MAX = 2 ** 63 - 1
# What aria2 changes only when it starts: its RPC accepts a new value for these
# and keeps the old one, so a change takes effect at the daemon's next start.
STARTUP_ONLY_OPTIONS = frozenset({"disk-cache"})


def aria2_size_bytes(value: str) -> int:
    """The bytes an aria2 size string stands for; ``ValueError`` for anything
    the packaged aria2 would refuse."""
    match = _SIZE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("not an aria2 size")
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def _sized(value: str, label: str, low: int, high: int = _ARIA2_SIZE_MAX) -> str:
    try:
        size = aria2_size_bytes(value)
    except ValueError:
        size = None
    if size is None or not low <= size <= high:
        lowest = "0" if low == 0 else f"{low // _MIB}M"
        span = (f"from {lowest} to {high // _MIB}M" if high != _ARIA2_SIZE_MAX
                else f"from {lowest} up to aria2's limit of {high // _MIB}M")
        raise ValueError(f"{label} must be a whole number with an optional K or M suffix (for example 64M), {span}")
    return value


class Aria2Options(BaseModel):
    """The aria2 settings an operator may set, bounded by what the packaged
    aria2 executable accepts. Strict: a value of the wrong
    type is refused, never coerced. One schema owns these rules: the Settings
    write, the load-time recovery (``integrations.configuration``) and the
    daemon's arguments all go through it."""
    model_config = ConfigDict(strict=True)

    operation_timeout_seconds: int = Field(default=15, ge=5, le=300)
    split: int = Field(default=16, ge=1, le=64)
    # aria2: 1M to 1024M.
    min_split_size: str = "10M"
    # aria2: 1 to 16 -- above 16 the daemon refuses to start.
    max_connection_per_server: int = Field(default=16, ge=1, le=16)
    continue_downloads: bool = True
    # aria2: any size its 64-bit parser holds.
    disk_cache: str = "64M"
    file_allocation: Literal["none", "prealloc", "trunc", "falloc"] = "falloc"
    # Bytes per second; aria2: any size its 64-bit parser holds.
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
    # `numeric_bounds` for the equivalent legacy flat field. (There is no
    # upload limit: DebridPulse's aria2 is compiled without BitTorrent, so it
    # never uploads; a stored ``max_upload_limit`` is an unknown key and ignored.)
    auto_start: bool = True
    log_file: str = "/app/data/aria2/aria2.log"
    log_max_mb: int = Field(default=25, ge=1, le=1024)
    log_backups: int = Field(default=3, ge=0, le=20)
    session_file: str = "/app/data/aria2/aria2.session"
    purge_interval_minutes: int = Field(default=5, ge=0, le=1440)
    max_download_result: int = Field(default=20, ge=10, le=5000)
    keep_unfinished_download_result: bool = False
    deep_sync_interval_minutes: int = Field(default=10, ge=0, le=1440)
    restart_interval_hours: float = Field(default=0, ge=0, allow_inf_nan=False)

    @field_validator("min_split_size")
    @classmethod
    def _min_split_size(cls, value: str) -> str:
        return _sized(value, "Minimum split size", _MIB, 1024 * _MIB)

    @field_validator("disk_cache")
    @classmethod
    def _disk_cache(cls, value: str) -> str:
        return _sized(value, "Disk cache", 0)

    @field_validator("lowest_speed_limit")
    @classmethod
    def _lowest_speed_limit(cls, value: str) -> str:
        return _sized(value, "Lowest speed limit", 0)

    @field_validator("log_file", "session_file")
    @classmethod
    def _runtime_path(cls, value: str) -> str:
        if (not value.startswith("/") or len(value) > 1024 or any(ord(char) < 32 for char in value)
                or posixpath.normpath(value) != value):
            raise ValueError("aria2 log and session files are normalized absolute paths")
        return value


def _upgrade_legacy_options(legacy: dict, existing: dict) -> dict:
    """Adjust option values taken from pre-canonical flat configuration.

    Applied only to legacy input, never to values the canonical namespace
    already holds: tuning left at an older default is raised to the current
    default.
    """
    upgraded = dict(legacy)
    for option in ("split", "max_connection_per_server"):
        if upgraded.get(option) in (4, 8):
            logger.info("Config migration: aria2 %s %s -> 16 (performance upgrade)", option, upgraded[option])
            upgraded[option] = 16
    return upgraded


def build(options, environment):
    from executors.aria2.admin import Aria2Administration
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from executors.aria2.runtime import RPC_SECRET, Aria2RuntimeConfiguration, rpc_service, runtime
    # Derived entirely from the injected typed `options` and the download root
    # -- never a global settings lookup and never DebridPulse global policy.
    # The daemon owner constructs the client for the one daemon DebridPulse runs.
    runtime.configure(Aria2RuntimeConfiguration(options=options, download_root=environment.download_root))
    client = rpc_service(options)
    configuration = Aria2Configuration(
        environment.download_root,
        options.split, options.min_split_size, options.max_connection_per_server, options.continue_downloads,
        waiting_window=options.waiting_window, stopped_window=options.stopped_window,
        secrets=(RPC_SECRET,),
    )
    executor = Aria2Executor(client, configuration, environment.repository.authorize_execution, runtime=runtime)
    # The managed daemon lifecycle and aria2's administration surface are
    # aria2-owned and reach the application through the generic integration
    # seam (``ManagedIntegration`` / ``AdministeredIntegration``).
    administration = Aria2Administration(executor, environment.repository, environment.commands, runtime)
    executor.lifecycle = administration
    executor.administration = administration
    return executor


definition = IntegrationDefinition(
    "aria2", "executor", "aria2", Aria2Options, build,
    legacy_fields=tuple(("aria2_" + field, field) for field in Aria2Options.model_fields),
    legacy_upgrade=_upgrade_legacy_options,
    # Earlier versions accepted values the managed aria2 cannot start on
    # (``disk_cache="18EGGS"``): a stored one is reset to its default at load.
    reset_invalid_options=True,
)
