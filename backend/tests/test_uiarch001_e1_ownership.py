from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_e1_correction_layers_are_physically_absent_and_unreferenced() -> None:
    joined = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.js"))
    index = read("index.html")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in joined


def test_icon_owner_has_no_loader_observer_or_dom_reparenting() -> None:
    icons = read("operator-title.js")
    for forbidden in (
        "MutationObserver", "createElement('script')", "appendChild(script)",
        "bindThemeToggle", "decorateNavigation",
    ):
        assert forbidden not in icons


def test_shell_structure_is_static_and_download_rows_are_final_at_render_time() -> None:
    index = read("index.html")
    app = read("app.js")
    assert 'data-dp-ui="v1.0.12-canonical"' in index
    assert "topbar-theme-control" in index
    assert "dp-dashboard-quick-add" in index
    assert "dp-dashboard-activity" in index
    assert "dp-activity-card" in index
    assert "dp-downloads-card-title" in index
    assert "dp-downloads-detail-row" in app
    assert 'draggable="true"' not in app
    assert "ondragstart=" not in app
    assert "function renderTorrentPagination(" in app
    assert "function setFilter(" in app
