import json
import logging
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from auth.passwords import hash_password
from core.branding import APP_SHORT_NAME
from core.secure_files import atomic_write_json

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config/config.json"))
logger = logging.getLogger("alldebrid.config")


class AppSettings(BaseModel):
    # AllDebrid
    alldebrid_api_key: str = ""
    alldebrid_agent: str = APP_SHORT_NAME

    # Logging
    log_level: str = "INFO"
    log_pretty: bool = False
    log_format: str = "plain"

    # Persistence — SQLite is the only runtime database.

    # Download control
    download_folder: str = "/download"
    max_concurrent_downloads: int = 3
    max_speed_mbps: int = 0
    aria2_max_download_limit: int = 0  # bytes/s, 0=unlimited — persisted across restarts
    aria2_max_upload_limit: int = 0    # bytes/s, 0=unlimited

    # Download delivery
    download_client: str = "aria2"
    aria2_mode: str = "builtin"  # built-in is the default; no extra setup required
    aria2_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: str = ""
    aria2_download_path: str = ""
    aria2_builtin_auto_start: bool = True
    aria2_builtin_port: int = 6800
    aria2_builtin_log_file: str = "/app/data/aria2/aria2.log"
    aria2_builtin_log_max_mb: int = 25
    aria2_builtin_log_backups: int = 3
    aria2_builtin_session_file: str = "/app/data/aria2/aria2.session"
    aria2_operation_timeout_seconds: int = 15
    aria2_start_paused: bool = False
    aria2_poll_interval_seconds: int = 2  # validated scheduler cadence
    aria2_max_active_downloads: int = 3
    aria2_purge_interval_minutes: int = 5  # purge completed results more often to free RAM
    aria2_max_download_result: int = 20  # lower = less RAM for completed download metadata
    aria2_keep_unfinished_download_result: bool = False
    aria2_waiting_window: int = 100
    aria2_stopped_window: int = 100
    aria2_split: int = 16             # segments per file — more = faster on fast connections
    aria2_min_split_size: str = "10M"  # split files >40 MB with split=16 (aria2 default)
    aria2_max_connection_per_server: int = 16  # parallel connections per server
    aria2_disk_cache: str = "64M"  # 64 MiB write buffer; reduces FUSE/NFS round-trips and syscall overhead
    aria2_file_allocation: str = "falloc"  # prealloc disk space for fewer write syscalls; use 'none' on FUSE/NFSa2
    aria2_continue_downloads: bool = True
    aria2_lowest_speed_limit: str = "0"

    # Discord
    discord_webhook_url: str = ""
    discord_webhook_added: str = ""
    discord_username: str = APP_SHORT_NAME
    discord_avatar_url: str = ""  # Discord only accepts PNG/JPG/WEBP — SVG rejected
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True
    discord_notify_update: bool = True

    # Filters
    filters_enabled: bool = False
    blocked_extensions: List[str] = [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
        ".svg", ".ico", ".tiff", ".heic", ".nfo", ".sfv"
    ]
    blocked_keywords: List[str] = []
    min_file_size_mb: int = 0

    # ── Smart File Selection ──────────────────────────────────────────────────
    # Automatically block sample files, extras, and featurettes.
    # Works alongside blocked_keywords — enabling this adds the most common
    # sample/extra patterns without requiring manual keyword configuration.
    block_samples: bool = False
    block_extras: bool = False

    # ── Advanced Extraction ───────────────────────────────────────────────────
    extraction_password: str = ""

    # Deep aria2 filesystem sync
    aria2_deep_sync_interval_minutes: int = 10
    aria2_restart_interval_hours: float = 0

    # Polling
    poll_interval_seconds: int = 30
    paused: bool = False

    # Rate limiting — AllDebrid API calls per minute (0 = unlimited)
    alldebrid_rate_limit_per_minute: int = 60

    # Auto-recover stalled downloads
    stuck_download_timeout_hours: int = 6
    full_sync_interval_minutes: int = 5

    # Backups
    backup_enabled: bool = True
    backup_folder: str = "/app/data/backups"
    backup_keep_days: int = 7
    backup_interval_hours: int = 24

    # Database maintenance
    db_backup_enabled: bool = True
    db_backup_folder: str = "/app/data/db-backups"
    db_backup_keep_days: int = 7
    db_wipe_enabled: bool = False
    db_backup_before_wipe: bool = True

    # Post-download extraction
    extract_enabled: bool = False
    extract_delete_archive: bool = True
    extract_max_concurrent: int = 1
    extract_max_files: int = 20000
    extract_max_expanded_gb: float = 250.0
    extract_max_compression_ratio: float = 1000.0
    discord_notify_extract: bool = True

    # AllDebrid upload retry
    upload_fail_retry_count: int = 3
    upload_fail_retry_delay_minutes: int = 5

    # aria2 download retry on error
    aria2_error_retry_count: int = 3
    aria2_error_retry_delay_seconds: int = 60

    # Labels / categories
    torrent_labels: List[str] = []

    # ── Statistics & Reporting ────────────────────────────────────────────────
    stats_snapshot_interval_minutes: int = 60
    stats_snapshot_keep_days: int = 30
    stats_report_interval_hours: int = 0
    update_check_interval_hours: int = 12
    stats_report_window_hours: int = 24
    stats_report_webhook_url: str = ""

    # ── Event log TTL ─────────────────────────────────────────────────────────
    events_keep_days: int = 30

    # ── Authentication ────────────────────────────────────────────────────────
    auth_password_enabled: bool = False
    auth_username: str = ""
    auth_password_hash: str = Field(default="", exclude=True)
    auth_password: str = ""
    auth_password_hash_clear: bool = Field(default=False, exclude=True)
    auth_session_lifetime_hours: int = 12

    auth_oidc_enabled: bool = False
    oidc_provider_name: str = "OpenID Connect"
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = Field(default="", exclude=True)
    oidc_client_secret_clear: bool = Field(default=False, exclude=True)
    oidc_scopes: List[str] = ["openid", "profile", "email"]
    oidc_allow_all: bool = False
    oidc_allowed_subjects: List[str] = []
    oidc_allowed_emails: List[str] = []
    oidc_allowed_groups: List[str] = []
    oidc_group_claim: str = "groups"
    # Canonical externally reachable origin used for secure-cookie classification
    # behind a trusted HTTPS reverse proxy. The OIDC callback is separately
    # configurable, but must remain on this same external origin.
    public_base_url: str = ""
    oidc_callback_url: str = ""

    def model_dump(self, *args, **kwargs):
        """Carry explicit legacy clear intent across the broad settings merge."""
        data = super().model_dump(*args, **kwargs)
        requested_clears = {
            str(field)
            for field in (getattr(self, "clear_secrets", []) or [])
            if str(field)
        }
        if "auth_password" in requested_clears:
            data["auth_password_hash_clear"] = True
        return data

    # ── Disk space guard ─────────────────────────────────────────────────────
    # Minimum free disk space required on the download filesystem. At/below the
    # configured threshold, new dispatch is deferred until the resume hysteresis
    # is satisfied. Transfers already active in aria2 are allowed to finish.
    min_free_disk_gb: float = 0
    disk_guard_interval_seconds: int = 60
    disk_guard_resume_hysteresis_gb: float = 0.5


_settings: AppSettings = AppSettings()


def _build_effective_settings(loaded: dict) -> AppSettings:
    return AppSettings(**{k: v for k, v in loaded.items() if k in AppSettings.model_fields})


def _migrate_password_settings(loaded: dict) -> bool:
    """Migrate legacy plaintext Basic credentials to the owned password model."""
    changed = False
    auth_state_present = any(
        field in loaded
        for field in ("auth_password_enabled", "auth_username", "auth_password_hash", "auth_password")
    )
    legacy_enable_semantics = "auth_password_enabled" not in loaded
    username = str(loaded.get("auth_username") or "").strip()
    plaintext = str(loaded.get("auth_password") or "")
    password_hash = str(loaded.get("auth_password_hash") or "").strip()

    if plaintext:
        # Plaintext is explicit credential input and therefore replaces any
        # older verifier rather than being silently discarded when both exist.
        loaded["auth_password_hash"] = hash_password(plaintext)
        password_hash = loaded["auth_password_hash"]
        loaded["auth_password"] = ""
        changed = True

    if auth_state_present and legacy_enable_semantics:
        loaded["auth_password_enabled"] = bool(username and password_hash)
        changed = True

    return changed


def get_settings() -> AppSettings:
    return _settings


def load_settings() -> AppSettings:
    loaded: dict = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("configuration root must be a JSON object")
            loaded = {k: v for k, v in data.items() if k in AppSettings.model_fields}
        except Exception as exc:
            # A missing file is a fresh/default installation. An existing file
            # that cannot be read is materially different: defaulting it would
            # silently turn configured authentication into open mode.
            raise RuntimeError("Existing configuration could not be read safely") from exc

    # ── Performance migration: built-in aria2 only ──────────────────────────
    if loaded.get("aria2_mode", "builtin") == "builtin":
        _PERF_UPGRADES = {
            "aria2_split": (4, 8, 16),
            "aria2_max_connection_per_server": (4, 8, 16),
        }
        for field, (old_low, old_mid, new_val) in _PERF_UPGRADES.items():
            stored = loaded.get(field)
            if stored in (old_low, old_mid):
                logger.info(
                    "Config migration: %s %s → %s (performance upgrade)",
                    field,
                    stored,
                    new_val,
                )
                loaded[field] = new_val

    password_migrated = _migrate_password_settings(loaded)
    settings = _build_effective_settings(loaded)
    if password_migrated:
        try:
            save_settings(settings)
        except Exception as exc:
            # Do not run indefinitely with a successfully migrated verifier only
            # in memory while legacy plaintext remains on persistent storage.
            raise RuntimeError("Password migration could not be persisted safely") from exc
        logger.info("Config migration: local authentication password stored as Argon2id hash")
    return settings


def save_settings(s: AppSettings):
    """Atomically persist configuration with secret-safe filesystem permissions."""
    global _settings
    plaintext = str(getattr(s, "auth_password", "") or "")
    if bool(getattr(s, "auth_password_hash_clear", False)):
        s.auth_password_hash = ""
        s.auth_password = ""
    elif plaintext:
        s.auth_password_hash = hash_password(plaintext)
        s.auth_password = ""
    elif not str(getattr(s, "auth_password_hash", "") or "").strip():
        s.auth_password_hash = str(getattr(_settings, "auth_password_hash", "") or "")

    if bool(getattr(s, "oidc_client_secret_clear", False)):
        s.oidc_client_secret = ""
    elif not str(getattr(s, "oidc_client_secret", "") or "").strip():
        s.oidc_client_secret = str(getattr(_settings, "oidc_client_secret", "") or "")

    data = s.model_dump()
    data.pop("auth_password", None)
    data["auth_password_hash"] = str(getattr(s, "auth_password_hash", "") or "")
    data["oidc_client_secret"] = str(getattr(s, "oidc_client_secret", "") or "")
    atomic_write_json(CONFIG_PATH, data, indent=2)

    s.auth_password_hash_clear = False
    s.oidc_client_secret_clear = False


def apply_settings(s: AppSettings):
    global _settings
    _settings = s


_settings = load_settings()
settings = _settings
