from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from auth.oidc_verification import oidc_verification_store
from auth.oidc_version import (
    authentication_configuration_baseline_version,
    oidc_configuration_version,
)


_OIDC_COMMIT_FIELDS = (
    "auth_oidc_enabled",
    "oidc_provider_name",
    "oidc_issuer_url",
    "oidc_client_id",
    "oidc_client_secret",
    "oidc_client_secret_clear",
    "oidc_scopes",
    "oidc_allow_all",
    "oidc_allowed_subjects",
    "oidc_allowed_emails",
    "oidc_allowed_groups",
    "oidc_group_claim",
    "public_base_url",
)


@dataclass(frozen=True, slots=True)
class PendingOidcConfiguration:
    settings: Any
    configuration_version: str
    created_at: float
    expires_at: float
    baseline_configuration_version: str = ""
    apply_password_enabled: bool = False


class PendingOidcConfigurationStore:
    """Bounded ephemeral proposed OIDC settings awaiting a full OIDC proof."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        max_entries: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._entries: OrderedDict[bytes, PendingOidcConfiguration] = OrderedDict()

    def _fingerprint(self, state: str) -> bytes:
        return hmac.new(
            self._key,
            str(state or "").encode("utf-8", errors="surrogatepass"),
            hashlib.sha256,
        ).digest()

    def stage(
        self,
        state: str,
        settings: Any,
        *,
        configuration_version: str,
        baseline_configuration_version: str = "",
        apply_password_enabled: bool = False,
    ) -> None:
        now = self._clock()
        self.cleanup()
        key = self._fingerprint(state)
        self._entries[key] = PendingOidcConfiguration(
            settings=settings,
            configuration_version=str(configuration_version),
            created_at=now,
            expires_at=now + self.ttl_seconds,
            baseline_configuration_version=str(baseline_configuration_version),
            apply_password_enabled=bool(apply_password_enabled),
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def has(self, state: str) -> bool:
        if not state:
            return False
        self.cleanup()
        return self._fingerprint(state) in self._entries

    def consume_verified(self, state: str) -> PendingOidcConfiguration | None:
        if not state:
            return None
        item = self._entries.pop(self._fingerprint(state), None)
        if item is None:
            return None
        if item.expires_at <= self._clock():
            return None
        actual = oidc_configuration_version(item.settings)
        if not actual or not secrets.compare_digest(item.configuration_version, actual):
            return None
        return item

    def discard(self, state: str) -> bool:
        if not state:
            return False
        return self._entries.pop(self._fingerprint(state), None) is not None

    def cleanup(self) -> int:
        now = self._clock()
        expired = [key for key, item in self._entries.items() if item.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        self.cleanup()
        return len(self._entries)


def _merge_verified_oidc_settings(current: Any, item: PendingOidcConfiguration):
    proposed = item.settings
    updates = {
        field: getattr(proposed, field)
        for field in _OIDC_COMMIT_FIELDS
        if hasattr(proposed, field)
    }
    if item.apply_password_enabled:
        updates["auth_password_enabled"] = bool(getattr(proposed, "auth_password_enabled", False))
    return current.model_copy(update=updates, deep=True)


def commit_verified_pending_oidc(
    state: str,
    *,
    expected_configuration_version: str,
) -> bool:
    """Commit a staged config only after matching proof and unchanged baseline."""
    item = pending_oidc_store.consume_verified(state)
    if item is None:
        return False

    proof_version = str(expected_configuration_version or "")
    if not proof_version or not secrets.compare_digest(
        proof_version,
        item.configuration_version,
    ):
        # A successful OIDC proof is valid only for the exact staged snapshot
        # that produced it. Reject before persistence; a post-write comparison is
        # too late to protect the appliance from a stale/mismatched proof.
        return False

    from auth.models import AuthMechanism
    from auth.sessions import session_store
    from core.config import apply_settings, get_settings, save_settings

    current = get_settings()
    current_baseline = authentication_configuration_baseline_version(current)
    if not item.baseline_configuration_version or not secrets.compare_digest(
        item.baseline_configuration_version,
        current_baseline,
    ):
        return False

    merged = _merge_verified_oidc_settings(current, item)
    # Record the proof before the configuration write. If proof persistence fails,
    # the configuration remains untouched. If the subsequent config write fails,
    # the stored fingerprint cannot validate against the unchanged live config.
    oidc_verification_store.record(item.configuration_version)
    save_settings(merged)
    authoritative = merged.model_copy(update={"oidc_client_secret_clear": False}, deep=True)
    apply_settings(authoritative)
    session_store.revoke_mechanism(AuthMechanism.OIDC_SESSION)
    return True


pending_oidc_store = PendingOidcConfigurationStore()
