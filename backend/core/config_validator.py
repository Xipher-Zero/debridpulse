"""
Config validation and sanitisation — runs at startup.

Checks the loaded AppSettings for common misconfigurations, type errors,
and stale / dangerous values. Logs warnings for every issue found and
returns a sanitised copy of the settings.  Never raises — startup must
not be blocked by a bad config value.

Numeric bounds of the canonical namespaces (``integrations.<id>``,
``transfer_policy``, ``execution_runtime_limits``) are owned by their pydantic
schemas and enforced once, at load (``integrations.configuration``); this module
only carries the cross-field/sanity rules that belong to no single schema.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

from core.logging_utils import sanitize_log_value

logger = logging.getLogger("debridpulse.config")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _options(cfg, integration_id: str) -> dict:
    """Canonical options of one integration namespace ({} when absent)."""
    entry = (getattr(cfg, "integrations", None) or {}).get(integration_id)
    return dict(getattr(entry, "options", None) or {})


def _is_valid_url(v: str, require_https: bool = False) -> bool:
    if not v:
        return True  # empty = not configured, not invalid
    pattern = r"^https?://.+" if not require_https else r"^https://.+"
    return bool(re.match(pattern, v.strip()))


# ── Validation rules ──────────────────────────────────────────────────────────
def _validate(cfg) -> List[Tuple[str, str, Any, Any]]:
    """
    Returns a list of (field, issue, bad_value, fixed_value) tuples.
    fixed_value=None means the field is logged as a warning but not changed.
    """
    issues: List[Tuple[str, str, Any, Any]] = []

    def warn(field: str, msg: str, bad, fixed=None):
        issues.append((field, msg, bad, fixed))

    # ── Provider / executor sanity (canonical namespaces) ─────────────────────
    api_key = str(_options(cfg, "alldebrid").get("api_key") or "")
    if api_key and len(api_key.strip()) < 10:
        warn("integrations.alldebrid.api_key", "looks too short to be valid", api_key)

    legacy_names = {
        "ACDC",
        "AllDebrid Control & Download Center",
        "AllDebrid-Client",
        "AllDebrid-Torrent-Client",
    }
    if getattr(cfg, "discord_username", "") in legacy_names:
        warn("discord_username", "legacy notification identity migrated to DebridPulse",
             cfg.discord_username, "DebridPulse")


    # ── URLs ──────────────────────────────────────────────────────────────────
    aria2_url = str(_options(cfg, "aria2").get("url") or "")
    if aria2_url and not _is_valid_url(aria2_url):
        warn("integrations.aria2.url", "not a valid HTTP(S) URL", aria2_url)

    for field in ("discord_webhook_url", "discord_webhook_added",
                  "stats_report_webhook_url"):
        val = getattr(cfg, field, "")
        if val and not _is_valid_url(val):
            warn(field, "not a valid HTTP(S) URL — webhook will not fire", val)

    public_base = getattr(cfg, "public_base_url", "")
    if public_base and not _is_valid_url(public_base, require_https=True):
        warn("public_base_url", "external base URL must use HTTPS", public_base)

    if getattr(cfg, "auth_oidc_enabled", False):
        issuer = getattr(cfg, "oidc_issuer_url", "")
        if issuer and not _is_valid_url(issuer, require_https=True):
            warn("oidc_issuer_url", "OIDC issuer must use HTTPS", issuer)

    # Discord avatar must be a real HTTP URL, not a data URI or SVG
    avatar = cfg.discord_avatar_url or ""
    if avatar.startswith("data:"):
        warn("discord_avatar_url", "data URI not accepted by Discord — cleared",
             avatar[:60] + "…", "")
    elif avatar.lower().endswith(".svg"):
        warn("discord_avatar_url",
             "SVG not accepted by Discord (use PNG/JPG/WEBP) — cleared",
             avatar, "")

    # ── Numeric ranges ────────────────────────────────────────────────────────
    numeric_bounds = {
        "full_sync_interval_minutes":     (0, 1440),
        "backup_keep_days":               (1, 365),
        "backup_interval_hours":          (1, 168),
        "db_backup_keep_days":            (1, 365),
        "stats_snapshot_interval_minutes":(0, 1440),
        "stats_snapshot_keep_days":       (1, 365),
        "stats_report_interval_hours":    (0, 168),
        "stats_report_window_hours":      (1, 8760),
        "auth_session_lifetime_hours":    (1, 168),
        "extract_max_files":              (1, 1_000_000),
        "extract_max_expanded_gb":        (1, 10_000),
        "extract_max_compression_ratio":  (1, 100_000),
    }
    for field, (lo, hi) in numeric_bounds.items():
        val = getattr(cfg, field, None)
        if val is None:
            continue
        if not isinstance(val, (int, float)):
            warn(field, f"expected number, got {type(val).__name__}", val, lo)
        elif val < lo:
            warn(field, f"value {val} below minimum {lo} — clamped", val, lo)
        elif val > hi:
            warn(field, f"value {val} above maximum {hi} — clamped", val, hi)

    # ── String sanity ─────────────────────────────────────────────────────────

    if getattr(cfg, "download_folder", "") == "/app/data/downloads":
        warn("download_folder", "legacy Docker default migrated to documented /download mount",
             cfg.download_folder, "/download")

    # ── List fields ───────────────────────────────────────────────────────────
    for field in (
        "oidc_scopes", "oidc_allowed_subjects", "oidc_allowed_emails", "oidc_allowed_groups",
    ):
        val = getattr(cfg, field, None)
        if val is not None and not isinstance(val, list):
            warn(field, f"expected list, got {type(val).__name__} — reset to []", val, [])

    return issues


# ── Public API ────────────────────────────────────────────────────────────────
def validate_and_sanitise(cfg) -> Any:
    """Validate settings without ever echoing configured secrets to logs."""
    from core.config import AppSettings

    issues = _validate(cfg)
    if not issues:
        logger.info("Config validation: OK — no issues found")
        return cfg

    sensitive = {
        "integrations.alldebrid.api_key", "discord_webhook_url",
        "discord_webhook_added", "stats_report_webhook_url",
        "auth_password", "auth_password_hash", "oidc_client_secret",
        "extraction_password",
    }
    fixes: Dict[str, Any] = {}
    for field, msg, bad, fixed in issues:
        shown = "<redacted>" if field in sensitive else sanitize_log_value(bad, max_length=160)
        if fixed is not None:
            logger.warning("Config [%s]: %s (was: %s -> corrected)", field, msg, shown)
            fixes[field] = fixed
        else:
            logger.warning("Config [%s]: %s (value: %s)", field, msg, shown)

    if not fixes:
        return cfg
    data = cfg.model_dump()
    data.update(fixes)
    sanitised = AppSettings(**{k: v for k, v in data.items() if k in AppSettings.model_fields})

    # These fields are intentionally excluded from model_dump() so ordinary
    # settings serialization cannot expose them. A sanitization rebuild must
    # nevertheless carry the private credential state and one-shot clear intent
    # forward; otherwise correcting an unrelated setting could silently convert
    # an authentication secret replacement/clear into "preserve existing".
    for field in (
        "auth_password_hash",
        "auth_password_hash_clear",
        "oidc_client_secret",
        "oidc_client_secret_clear",
    ):
        if hasattr(cfg, field):
            setattr(sanitised, field, getattr(cfg, field))

    logger.info("Config validation: %d issue(s) found, %d field(s) corrected", len(issues), len(fixes))
    return sanitised
