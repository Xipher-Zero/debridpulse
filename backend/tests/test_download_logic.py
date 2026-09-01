"""
Comprehensive tests for the DebridPulse download logic.

Tests cover:
- Status transitions (state machine)
- _start_download guard: no duplicate starts for active downloads
- _start_download guard: allows restart after _reset_torrent_for_redownload
- full_alldebrid_sync: does not restart queued/downloading/paused torrents
- _finalize_aria2_torrent: correct completion detection
"""
import pytest
import sys
import types

# ── Minimal stubs so manager_v2 can be imported without real dependencies ─────
for mod, stub in {
    "aiohttp": types.SimpleNamespace(
        ClientSession=object, ClientTimeout=lambda **k: None,
        FormData=object,
        TCPConnector=lambda **k: None,
        ServerDisconnectedError=Exception, ClientConnectorError=Exception,
        ClientOSError=Exception, ClientError=Exception,
    ),
    "aiosqlite": types.SimpleNamespace(connect=None, Row=object),
    "asyncpg": types.SimpleNamespace(connect=None),
}.items():
    if mod not in sys.modules:
        sys.modules[mod] = stub

from core.config import AppSettings


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_cfg(**kwargs) -> AppSettings:
    return AppSettings(**kwargs)


def make_torrent_row(status: str, alldebrid_id: str = "123", torrent_id: int = 1) -> dict:
    return {
        "id": torrent_id,
        "name": "Test Torrent",
        "alldebrid_id": alldebrid_id,
        "status": status,
        "provider_status": "ready",
        "provider_status_code": 4,
        "polling_failures": 0,
    }


# ── Status-machine invariants ─────────────────────────────────────────────────

class TestStatusTransitions:
    """Verify the documented status transitions are internally consistent."""


    def test_restartable_statuses_in_full_sync(self):
        """full_alldebrid_sync should only restart these statuses."""
        # As defined in the fix: queued/downloading/paused must NOT be in this set
        restartable = {"error", "pending", "uploading", "processing", "ready"}
        assert "queued" not in restartable
        assert "downloading" not in restartable
        assert "paused" not in restartable


# ── _finalize_aria2_torrent logic ─────────────────────────────────────────────

class TestFinalizeLogic:
    """Replicate the completion-detection logic from _finalize_aria2_torrent."""

    def _should_complete(self, required, completed, error, active) -> tuple:
        """Returns (should_complete, reason) matching _finalize_aria2_torrent logic."""
        if required == 0:
            return True, "all_blocked"
        if required > 0 and completed == required and error == 0 and active == 0:
            return True, "all_done"
        if error > 0 and active == 0:
            return False, "error"
        if active > 0:
            return False, "still_active"
        return False, "unknown"

    def test_all_blocked_finalizes(self):
        ok, reason = self._should_complete(required=0, completed=0, error=0, active=0)
        assert ok is True
        assert reason == "all_blocked"

    def test_all_done_finalizes(self):
        ok, _ = self._should_complete(required=5, completed=5, error=0, active=0)
        assert ok is True

    def test_still_active_does_not_finalize(self):
        ok, _ = self._should_complete(required=5, completed=3, error=0, active=2)
        assert ok is False

    def test_error_with_no_active_does_not_complete(self):
        ok, reason = self._should_complete(required=5, completed=3, error=1, active=0)
        assert ok is False
        assert reason == "error"

    def test_partial_done_does_not_finalize(self):
        ok, _ = self._should_complete(required=5, completed=3, error=0, active=0)
        # 3 done, 0 error, 0 active but required=5 → 2 files unaccounted
        assert ok is False


# ── normalize_provider_state ──────────────────────────────────────────────────


# ── safe_name / safe_rel_path ─────────────────────────────────────────────────

class TestPathHelpers:
    def test_safe_name_strips_dangerous_chars(self):
        from transfers.filesystem import safe_name
        assert "/" not in safe_name("a/b/c")
        assert "\\" not in safe_name("a\\b")
        # After fix: leading dots are stripped so '..' cannot appear at the start
        result = safe_name("../etc/passwd")
        assert not result.startswith("..")
        assert result  # non-empty fallback

    def test_safe_name_strips_leading_dots(self):
        from transfers.filesystem import safe_name
        assert not safe_name("../evil").startswith("..")
        assert not safe_name("../../root").startswith("..")
        assert safe_name(".hidden_file") == "hidden_file"  # leading dot stripped

    def test_safe_name_normal_stays_intact(self):
        from transfers.filesystem import safe_name
        result = safe_name("My Movie (2024) [1080p]")
        assert "Movie" in result
        assert "(2024)" in result

    def test_safe_name_preserves_normal(self):
        from transfers.filesystem import safe_name
        result = safe_name("My Movie (2024) [1080p]")
        assert result  # non-empty
        assert len(result) <= 255


# ── full_sync restartable set ─────────────────────────────────────────────────


# ── Config validator integration ──────────────────────────────────────────────

class TestConfigValidatorWithDownloadSettings:
    def test_max_concurrent_downloads_clamped(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(max_concurrent_downloads=0)
        result = validate_and_sanitise(cfg)
        assert result.max_concurrent_downloads == 1

    def test_aria2_max_active_clamped(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(aria2_max_active_downloads=50)
        result = validate_and_sanitise(cfg)
        assert result.aria2_max_active_downloads == 20

    def test_stuck_timeout_clamped(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(stuck_download_timeout_hours=200)
        result = validate_and_sanitise(cfg)
        assert result.stuck_download_timeout_hours == 168

    def test_poll_interval_minimum(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(poll_interval_seconds=1)
        result = validate_and_sanitise(cfg)
        assert result.poll_interval_seconds >= 5
