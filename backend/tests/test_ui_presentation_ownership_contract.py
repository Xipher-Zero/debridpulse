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
        "/ui-dashboard-transfer-presentation.js?v=3",
        "/ui-downloads-presentation.js?v=2",
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


_NETWORK_GEOMETRY_FRAGMENT = '<rect x="16" y="16" width="6" height="6" rx="1"/>'


def test_network_glyph_has_one_canonical_lucide_geometry_owner() -> None:
    icons = read("operator-title.js")
    assert "network:" in icons
    assert _NETWORK_GEOMETRY_FRAGMENT in icons
    assert 'd="M12 12V8"' in icons

    # No candidate presentation runtime (or any other static JS) pastes a second
    # copy of the Network geometry — they must call window.DPIcons.svg('network').
    for path in STATIC.glob("*.js"):
        if path.name == "operator-title.js":
            continue
        assert _NETWORK_GEOMETRY_FRAGMENT not in path.read_text(encoding="utf-8"), path.name

    for owner in ("ui-detail-candidates.js", "ui-dashboard-transfer-presentation.js"):
        source = read(owner)
        assert "DPIcons.svg('network'" in source or 'DPIcons.svg("network"' in source


def test_multi_source_candidate_chip_has_one_shared_base_visual_owner() -> None:
    transfer = read("ui-transfer-contract.css")
    # The shared passive/base chip material and geometry live with the reusable
    # transfer/provider row contract.
    assert ".dp-candidate-chip {" in transfer
    assert ".dp-candidate-chip .dp-candidate-chip-count {" in transfer

    # Surface files own only bounded layout differences, never a second base
    # definition of the chip material.
    for surface in ("ui-dashboard-transfer-presentation.css", "ui-downloads-presentation.css"):
        css = read(surface)
        assert ".dp-candidate-chip {" not in css
    details = read("ui-detail-candidates.css")
    assert ".dp-candidate-chip {" not in details
    # The obsolete circular count badge rule is gone, not left dormant.
    assert ".dp-detail-candidate-count{" not in details

    # One shared JS builder for the passive chip, reused by Downloads.
    dashboard = read("ui-dashboard-transfer-presentation.js")
    assert "candidateChipMarkup" in dashboard
    assert "DPDashboardTransferPresentation" in dashboard and "candidateChipMarkup" in dashboard
    downloads = read("ui-downloads-presentation.js")
    assert "DPDashboardTransferPresentation?.candidateChipMarkup" in downloads


def test_candidate_detail_css_is_in_the_canonical_graph_and_not_runtime_injected() -> None:
    style = read("style-v11.css")
    assert style.count("/ui-detail-candidates.css") == 1
    assert "@import url('/ui-detail-candidates.css?v=4');" in style

    runtime = read("ui-detail-candidates.js")
    # No self-injected stylesheet and no correction/compat layer.
    assert "createElement('link')" not in runtime
    assert "rel = 'stylesheet'" not in runtime
    assert "'/ui-detail-candidates.css" not in runtime
    assert "dpDetailCandidatesStyle" not in runtime
    assert "ui-correction" not in style
    assert "ui-compat" not in style


def test_import_existing_backend_capability_and_direct_source_metadata_remain() -> None:
    routes = (ROOT / "backend" / "api" / "routes.py").read_text(encoding="utf-8")
    definition = (ROOT / "backend" / "providers" / "general_http" / "definition.py").read_text(encoding="utf-8")
    assert "/torrents/import-existing" in routes
    assert "btn-import-existing" in read("ui-processing-presentation.js")
    assert 'status_group="direct_sources"' in definition
    assert 'status_group_label="Direct Sources"' in definition
