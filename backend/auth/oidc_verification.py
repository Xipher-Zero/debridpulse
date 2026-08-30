from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.secure_files import atomic_write_json


DEFAULT_OIDC_VERIFICATION_PATH = Path(
    os.getenv("OIDC_VERIFICATION_PATH", "/app/data/oidc-verification.json")
)


@dataclass(frozen=True, slots=True)
class OidcVerificationStatus:
    verified: bool
    verified_at: str = ""


class OidcVerificationStore:
    """Persist proof that one exact security-critical OIDC configuration passed."""

    def __init__(self, path: Path = DEFAULT_OIDC_VERIFICATION_PATH) -> None:
        self.path = Path(path)

    @staticmethod
    def _valid_version(value: str) -> bool:
        raw = str(value or "").strip().casefold()
        return len(raw) == 64 and all(char in "0123456789abcdef" for char in raw)

    @staticmethod
    def _valid_timestamp(value: str) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None

    def _read(self) -> tuple[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return "", ""
        if not isinstance(payload, dict):
            return "", ""
        version = str(payload.get("configuration_version") or "").strip().casefold()
        verified_at = str(payload.get("verified_at") or "").strip()
        if not self._valid_version(version) or not self._valid_timestamp(verified_at):
            return "", ""
        return version, verified_at

    def record(self, configuration_version: str) -> str:
        version = str(configuration_version or "").strip().casefold()
        if not self._valid_version(version):
            raise ValueError("OIDC verification requires a valid configuration fingerprint")
        verified_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        atomic_write_json(
            self.path,
            {
                "configuration_version": version,
                "verified_at": verified_at,
            },
            indent=2,
        )
        return verified_at

    def status(self, current_configuration_version: str) -> OidcVerificationStatus:
        current = str(current_configuration_version or "").strip().casefold()
        recorded, verified_at = self._read()
        if not self._valid_version(current) or not recorded:
            return OidcVerificationStatus(False)
        if not secrets.compare_digest(current, recorded):
            return OidcVerificationStatus(False)
        return OidcVerificationStatus(True, verified_at)


oidc_verification_store = OidcVerificationStore()
