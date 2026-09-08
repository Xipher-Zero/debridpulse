from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_retired_correction_assets_and_globals_are_absent() -> None:
    for name in (
        "ui-correction-batch1.js", "ui-correction-batch1-final.js",
        "ui-correction-p4-repair.js", "ui-correction-batch1.css",
        "ui-correction-batch1-provider-card.css", "ui-presentation-loader.js",
    ):
        assert not (STATIC / name).exists()
    joined = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.js"))
    for token in ("DPUICorrectionBatch1", "DPUICorrectionBatch1Final", "DPUICorrectionP4Repair", "DPPresentationLoader"):
        assert token not in joined


def test_bounded_presentation_boot_is_ordered_and_loader_free() -> None:
    provider = read("ui-provider-status.js")
    expected = (
        "/ui-toast-contract.js?v=2",
        "/ui-processing-presentation.js?v=1",
        "/ui-dashboard-transfer-presentation.js?v=1",
        "/ui-downloads-presentation.js?v=1",
        "/ui-activity-log-runtime.js?v=1",
        "/ui-settings-archive-passwords.js?v=1",
    )
    positions = []
    for src in expected:
        assert provider.count(src) == 1
        positions.append(provider.index(src))
    assert positions == sorted(positions)
    assert "bootPresentationOwners" in provider
    assert "ui-presentation-loader.js" not in provider
    assert "DPPresentationLoader" not in provider


def test_bounded_runtime_owners_are_present() -> None:
    provider = read("ui-provider-status.js")
    assert "ui-correction" not in provider
    assert "createElement('script')" not in read("ui-settings-card-icons.js")
    assert "window.DPDashboardTransferPresentation" in read("ui-dashboard-transfer-presentation.js")
    downloads = read("ui-downloads-presentation.js")
    assert "window.DPDownloadsPresentation" in downloads and "ResizeObserver" in downloads
    assert all(token in downloads for token in ("Friendly", "International", "ISO", "12-hour", "24-hour", "dp-pager-placeholder"))
    assert "window.DPProcessingPresentation" in read("ui-processing-presentation.js")
    activity = read("ui-activity-log-runtime.js")
    assert "window.DPActivityLog" in activity and "EVENT_LIMIT=500" in activity and "include_meta" in activity
    archive = read("ui-settings-archive-passwords.js")
    assert "window.DPArchivePasswords" in archive
    assert all(token in archive for token in ("Show all passwords", "Hide all passwords", "Escape", "Enter", "Backspace", "clipboardData"))


def test_canonical_css_graph_uses_named_component_owners() -> None:
    style = read("style-v11.css")
    for owner in (
        "ui-provider-summary.css", "ui-dashboard-transfer-presentation.css",
        "ui-downloads-presentation.css", "ui-activity-log-controls.css",
        "ui-settings-archive-passwords.css", "ui-detail-files.css",
    ):
        assert style.count(owner) == 1
    assert "ui-correction" not in style
    assert "width:136px" in read("ui-detail-files.css").replace(" ", "")


def test_import_existing_backend_capability_and_direct_source_metadata_remain() -> None:
    routes = (ROOT / "backend" / "api" / "routes.py").read_text(encoding="utf-8")
    definition = (ROOT / "backend" / "providers" / "general_http" / "definition.py").read_text(encoding="utf-8")
    assert "/torrents/import-existing" in routes
    assert "btn-import-existing" in read("ui-processing-presentation.js")
    assert 'status_group="direct_sources"' in definition
    assert 'status_group_label="Direct Sources"' in definition
