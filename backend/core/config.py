import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from auth.passwords import hash_password
from core.branding import APP_SHORT_NAME
from core.secure_files import atomic_write_json
from integrations.configuration import clamp_persisted_namespaces, migrate_legacy_settings, normalize_settings
from integrations.definition import IntegrationGroupSettings, IntegrationSettings
from transfers.runtime_limits import ExecutionRuntimeLimits
from transfers.settings import TransferSettings

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config/config.json"))
logger = logging.getLogger("debridpulse.config")

# Narrow serialized config-mutation authority (DP 1.0.12 canonical
# architecture correction, Workstream C, specification sections 9.5, 13.8):
# every settings-namespace mutation route (whole-settings PUT and every
# scoped PATCH) must serialize its own load-modify-save critical section
# under this lock so a concurrent writer's read-modify-write cannot silently
# overwrite another namespace's newer value with a stale full snapshot. This
# is deliberately NOT ``ApplicationMaintenanceGate``/``configuration_admission()``
# (specification section 2.6, 6): it only ever contends with another config
# writer, never with an unrelated long-running resolution/execution
# operation, so it cannot reproduce the speed-cap collision this correction
# eliminated.
_config_write_lock = asyncio.Lock()


def config_write_lock() -> asyncio.Lock:
    return _config_write_lock


class AppSettings(BaseModel):
    # Canonical authorities. ``integrations.<id>`` owns each provider's and the
    # executor's configuration (including the AllDebrid credentials and every
    # executor option), ``transfer_policy`` owns execution/resolution policy, and
    # ``execution_runtime_limits`` owns runtime capability limits. Nothing below
    # duplicates them: pre-canonical flat names are migration input only
    # (``integrations.configuration.migrate_legacy_settings``).
    integrations: dict[str, IntegrationSettings] = Field(default_factory=dict, repr=False)
    # Aggregate participation gates over integration FAMILIES, keyed by the
    # ``presentation.status_group`` the members already declare. Composes with
    # ``integrations.<id>.enabled`` at runtime and never rewrites it.
    integration_groups: dict[str, IntegrationGroupSettings] = Field(default_factory=dict, repr=False)
    transfer_policy: TransferSettings = Field(default_factory=TransferSettings)
    execution_runtime_limits: ExecutionRuntimeLimits = Field(default_factory=ExecutionRuntimeLimits)

    # Logging
    log_level: str = "INFO"
    log_pretty: bool = False
    log_format: str = "plain"

    # Persistence — SQLite is the only runtime database.

    # Download control
    download_folder: str = "/download"

    # Discord
    discord_webhook_url: str = ""
    discord_webhook_added: str = ""
    discord_username: str = APP_SHORT_NAME
    discord_avatar_url: str = ""  # Discord only accepts PNG/JPG/WEBP — SVG rejected
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True
    discord_notify_update: bool = True

    # ── Advanced Extraction ───────────────────────────────────────────────────
    extraction_password: str = ""

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
    # Canonical externally reachable origin used for OIDC callback construction
    # and secure-cookie classification behind a trusted HTTPS reverse proxy.
    public_base_url: str = ""

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
    # is satisfied. Transfers already active in an executor are allowed to finish.
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


# Pre-1.0.12 configuration persisted the global pause flag. Processing pause is
# operational state whose only authority is the durable application state; the
# legacy value is read once, here, and consumed only by the v1.0.12 database
# migration (main.py) that seeds that state. It is never a setting.
_legacy_startup_inputs: dict = {"paused": False}


def legacy_paused_input() -> bool:
    return bool(_legacy_startup_inputs["paused"])


def get_settings() -> AppSettings:
    return _settings


def load_settings() -> AppSettings:
    from integrations.catalog import definitions

    loaded: dict = {}
    legacy_migrated = False
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("configuration root must be a JSON object")
            # The one translation boundary: pre-canonical flat keys are folded
            # into the canonical namespaces here and never seen again.
            legacy_migrated = migrate_legacy_settings(data, definitions)
            if "paused" in data:
                _legacy_startup_inputs["paused"] = bool(data["paused"])
            for name in clamp_persisted_namespaces(data, definitions):
                logger.warning("Config [%s]: value out of range - clamped", name)
                legacy_migrated = True
            loaded = {k: v for k, v in data.items() if k in AppSettings.model_fields}
        except Exception as exc:
            # A missing file is a fresh/default installation. An existing file
            # that cannot be read is materially different: defaulting it would
            # silently turn configured authentication into open mode.
            raise RuntimeError("Existing configuration could not be read safely") from exc

    password_migrated = _migrate_password_settings(loaded)
    try:
        # Loaded settings always carry complete, validated canonical namespaces.
        settings = normalize_settings(_build_effective_settings(loaded), definitions)
    except Exception as exc:
        raise RuntimeError("Existing configuration could not be read safely") from exc
    if password_migrated:
        try:
            save_settings(settings)
        except Exception as exc:
            # Do not run indefinitely with a successfully migrated verifier only
            # in memory while legacy plaintext remains on persistent storage.
            raise RuntimeError("Password migration could not be persisted safely") from exc
        logger.info("Config migration: local authentication password stored as Argon2id hash")
    elif legacy_migrated:
        try:
            save_settings(settings)
        except Exception as exc:
            # The canonical settings are already authoritative in memory and the
            # next load repeats the same deterministic migration, so a failed
            # rewrite is recoverable; the flat keys are simply not yet removed.
            logger.warning("Config migration: canonical settings could not be persisted yet: %s", type(exc).__name__)
        else:
            logger.info("Config migration: legacy flat settings folded into canonical namespaces")
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
