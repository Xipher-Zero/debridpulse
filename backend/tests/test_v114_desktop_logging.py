"""The desktop application's log says what it is and keeps real errors visible.

* Its startup banner names the desktop application and the window's own
  address, never "Docker / Unraid" and 0.0.0.0 (a server's are unchanged).
* A peer resetting or aborting a connection DownloadPulse was closing (Windows'
  WinError 10054/10053, a page going away) is not logged as an error with a
  traceback; every other event-loop error still is.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_the_banner_names_the_desktop_application_and_its_windows_address():
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert 'mode="Desktop application" if desktop_installation() else "Docker / Unraid",' in source
    assert 'f"http://127.0.0.1:{desktop_port} (the application window)" if desktop_port' in source


def test_a_closed_connections_reset_is_dropped_and_every_other_loop_error_is_logged(caplog):
    from main import _quiet_closed_connections
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            for error in (ConnectionResetError(10054, "reset"), ConnectionAbortedError(10053, "aborted")):
                _quiet_closed_connections(loop, {
                    "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
                    "exception": error})
            assert [record for record in caplog.records if record.name == "asyncio"] == []
            # The same exception anywhere else, and any other error, reach the default handler.
            _quiet_closed_connections(loop, {"message": "Task exception was never retrieved",
                                             "exception": ConnectionResetError(10054, "reset")})
            _quiet_closed_connections(loop, {"message": "Exception in callback _call_connection_lost",
                                             "exception": ValueError("real")})
        assert len([record for record in caplog.records if record.name == "asyncio"]) == 2
    finally:
        loop.close()


def test_the_desktop_application_verifies_certificates_with_the_systems_trust():
    """Windows and macOS: truststore is in the desktop locks and injected
    before any project module loads (so before any TLS context exists); a
    server, Linux, or an installation without it is unchanged."""
    import os
    import subprocess
    import sys
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert source.index("SYSTEM_TRUST = _system_trust()") < source.index("from api.account_routes import")
    assert 'if not desktop_installation() or sys.platform not in {"win32", "darwin"}:' in source
    for platform in ("windows", "macos"):
        lock = (BACKEND.parent / "packaging" / "desktop" / platform / "requirements.txt").read_text(encoding="utf-8")
        assert "truststore==0.10.4 \\\n" in lock
    assert "truststore" not in (BACKEND / "requirements.txt").read_text(encoding="utf-8")   # never the server image
    # Here (Linux, not the desktop application): nothing is injected.
    env = {key: value for key, value in os.environ.items() if key != "DOWNLOADPULSE_DESKTOP"}
    done = subprocess.run([sys.executable, "-c", "import main; print(main.SYSTEM_TRUST)"], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip().splitlines()[-1] == "False", done.stderr[-1500:]
