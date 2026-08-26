from pathlib import Path


def test_dashboard_startup_debug_surface_is_removed_without_changing_retry_logic():
    root = Path(__file__).resolve().parents[2]
    app = (root / "frontend/static/app.js").read_text()
    operator = (root / "frontend/static/operator-title.js").read_text()
    index = (root / "frontend/static/index.html").read_text()

    # The inherited startup helper remains defensive and therefore safely
    # becomes a no-op once the legacy dashboard node is removed.
    assert "function dbg(msg)" in app
    assert "const el = document.getElementById('debug-status');" in app
    assert "if (!el) return;" in app

    # v1 must not present the startup retry/debug stream as dashboard UI.
    assert "function removeLegacyStartupDebugSurface()" in operator
    assert "const debugStatus = document.getElementById('debug-status');" in operator
    assert "if (debugStatus) debugStatus.remove();" in operator
    assert "removeLegacyStartupDebugSurface();" in operator

    # Duplicate-status material is now statically owned by the frontend CSS
    # contract rather than being installed by the operator-title behavior shim.
    assert "installDuplicateStatusStyle" not in operator
    assert "document.createElement('style')" not in operator

    # The cleanup extension executes immediately after the core deferred script;
    # the async startup initializer yields on its first settings request, while
    # the second deferred script removes the presentation node.
    assert index.index('<script src="/app.js?v=14" defer></script>') < index.index(
        '<script src="/operator-title.js?v=21" defer></script>'
    )
