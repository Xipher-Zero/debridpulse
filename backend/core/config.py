import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

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
    # Section participation. Whether the feature takes part at all is a
    # different fact from whether it is configured: disabling stops delivery
    # and erases nothing, and the status projection keeps reporting the truth
    # about the stored configuration either way.
    #
    # It defaults to True so a configuration written before this field existed
    # -- where Discord delivery was gated only by a stored webhook and the
    # per-event toggles -- keeps behaving exactly as it did. A fresh install is
    # equally sane: participation is on, nothing is configured, so nothing is
    # delivered. Its canonical runtime gate is
    # ``services.notification_service.NotificationService.client()``.
    discord_notifications_enabled: bool = True
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
    # Section participation, on the same terms as Discord above: it gates
    # SCHEDULED reporting only, never the stored destination, interval or
    # window, and never Discord. ``stats_report_interval_hours = 0`` remains
    # its own separate cadence fact ("no automatic reports"); the two are not
    # conflated. Defaults to True for the same upgrade reason.
    stats_reporting_enabled: bool = True
    stats_report_interval_hours: int = 0
    update_check_interval_hours: int = 12
    stats_report_window_hours: int = 24
    stats_report_webhook_url: str = ""

    # Durable notification verification evidence: ``{subject id: fingerprint}``,
    # exactly the shape and the meaning ``IntegrationSettings.verification``
    # carries -- one entry per independently testable notification subject
    # whose CURRENT SAVED material a successful Test has covered.
    #
    # It lives with the configuration it describes because that is the only
    # place it can stay true across a reload, and current truth is DERIVED
    # (does a stored fingerprint still describe what is saved?) rather than a
    # flag somebody has to remember to clear. It is internal: never published,
    # never accepted from a request. Its one owner is
    # ``services.notification_service``.
    notification_verification: dict[str, str] = Field(default_factory=dict, repr=False)

    # ── Event logging ─────────────────────────────────────────────────────────
    # How many events one Activity Log page shows (50, 100 or 250). It selects
    # a page size only: the event journal itself is kept indefinitely.
    activity_log_page_size: int = 100

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

    # ── Acquisition preferences ──────────────────────────────────────────────
    # The operator's Preferred Subtitle Language: one language code (a primary
    # language subtag, optionally with one region or script subtag -- "en",
    # "pt-br", "zh-hans"), stored lowercase. Global Downloads preference: an
    # integration that acquires subtitled media reads it and keeps no copy.
    preferred_subtitle_language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")

    @field_validator("preferred_subtitle_language", mode="before")
    @classmethod
    def _normalized_language(cls, value):
        return str(value or "").strip().casefold() if isinstance(value, str) or value is None else value


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


class LegacySettingsDocument(ValueError):
    """A configuration document written by an older DebridPulse."""


def validate_settings_document(data) -> AppSettings:
    """Strictly check a persisted configuration document without applying it.

    The side-effect-free twin of ``load_settings`` for a document that is not
    the live one (a backup's configuration): nothing is translated, clamped,
    migrated or written. A document that would need any of that was written by
    an older DebridPulse and raises ``LegacySettingsDocument``; one that cannot
    produce settings at all raises ``ValueError``."""
    import copy

    from integrations.catalog import definitions

    if not isinstance(data, dict):
        raise ValueError("configuration root must be a JSON object")
    candidate = copy.deepcopy(data)
    if "paused" in candidate or migrate_legacy_settings(candidate, definitions):
        raise LegacySettingsDocument("configuration requires legacy translation")
    if clamp_persisted_namespaces(candidate, definitions):
        raise ValueError("configuration holds out-of-range values")
    loaded = {k: v for k, v in candidate.items() if k in AppSettings.model_fields}
    if _migrate_password_settings(loaded):
        raise LegacySettingsDocument("configuration requires password migration")
    return normalize_settings(_build_effective_settings(loaded), definitions)


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


# Metadata about configuration, never an operator's configuration change.
_UNJOURNALED_SETTINGS = frozenset({"notification_verification", "integrations", "integration_groups"})
_SECRET_ATTRIBUTES = ("auth_password_hash", "oidc_client_secret")


def configuration_changes(previous: AppSettings, current: AppSettings) -> tuple[list[str], list[tuple[str, bool]]]:
    """The NAMES of what a configuration write changed -- never a value, so a
    secret's change is visible while the secret is not -- and the integrations
    whose enablement it flipped."""
    before, after = previous.model_dump(), current.model_dump()
    changed = []
    for key in sorted(set(before) | set(after)):
        if key in _UNJOURNALED_SETTINGS or before.get(key) == after.get(key):
            continue
        if isinstance(before.get(key), dict) and isinstance(after.get(key), dict):
            inner_before, inner_after = before[key], after[key]
            changed.extend(f"{key}.{name}" for name in sorted(set(inner_before) | set(inner_after))
                           if inner_before.get(name) != inner_after.get(name))
        else:
            changed.append(key)
    changed.extend(name for name in _SECRET_ATTRIBUTES
                   if str(getattr(previous, name, "") or "") != str(getattr(current, name, "") or ""))
    toggled = []
    for integration_id in sorted(set(previous.integrations) | set(current.integrations)):
        old, new = previous.integrations.get(integration_id), current.integrations.get(integration_id)
        old_enabled, new_enabled = bool(getattr(old, "enabled", False)), bool(getattr(new, "enabled", False))
        if old_enabled != new_enabled:
            toggled.append((integration_id, new_enabled))
        old_options = dict(getattr(old, "options", None) or {})
        new_options = dict(getattr(new, "options", None) or {})
        changed.extend(f"integrations.{integration_id}.{name}" for name in sorted(set(old_options) | set(new_options))
                       if old_options.get(name) != new_options.get(name))
        if getattr(old, "priority", None) != getattr(new, "priority", None) and old is not None and new is not None:
            changed.append(f"integrations.{integration_id}.priority")
    for group in sorted(set(previous.integration_groups or {}) | set(current.integration_groups or {})):
        if (previous.integration_groups or {}).get(group) != (current.integration_groups or {}).get(group):
            changed.append(f"integration_groups.{group}")
    return changed, toggled


async def journal_configuration_change(previous: AppSettings, current: AppSettings) -> None:
    """Record an operator's committed configuration change in the event
    journal, after it was saved (a settings document is a file, not a database
    transition): one event per integration enabled or disabled, and one naming
    every other changed setting. Nothing is recorded when nothing changed."""
    from db.event_journal import JournalEvent, record_now

    changed, toggled = configuration_changes(previous, current)
    for integration_id, enabled in toggled:
        await record_now(JournalEvent(
            "configuration", "configuration.integration_enabled" if enabled else "configuration.integration_disabled",
            "info", "Integration enabled" if enabled else "Integration disabled", "integration",
            subject_id=integration_id, integration_id=integration_id))
    if changed:
        await record_now(JournalEvent(
            "configuration", "configuration.settings_changed", "info",
            f"Settings changed ({len(changed)})", "settings", detail=", ".join(changed)))


_settings = load_settings()
settings = _settings
