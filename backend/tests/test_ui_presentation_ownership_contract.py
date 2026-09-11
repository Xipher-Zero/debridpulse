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

    for owner in ("ui-detail-candidates.js", "ui-group-candidates.js"):
        source = read(owner)
        assert "DPIcons.svg('network'" in source or 'DPIcons.svg("network"' in source


def test_candidate_chip_family_has_one_shared_base_visual_owner() -> None:
    transfer = read("ui-transfer-contract.css")
    # The shared chip material and geometry live with the reusable
    # transfer/provider row contract.
    assert ".dp-candidate-chip {" in transfer
    assert ".dp-candidate-chip .dp-candidate-chip-count {" in transfer

    # Surface / detail / group files own only bounded layout or interactive
    # differences, never a second base definition of the chip material.
    for surface in (
        "ui-dashboard-transfer-presentation.css", "ui-downloads-presentation.css",
        "ui-detail-candidates.css", "ui-group-candidates.css",
    ):
        assert ".dp-candidate-chip {" not in read(surface)
    # The obsolete circular count badge rule is gone, not left dormant.
    assert ".dp-detail-candidate-count{" not in read("ui-detail-candidates.css")


def test_transfer_level_candidate_control_is_one_shared_group_owner() -> None:
    # Downloads and Dashboard Recent both call the single shared group launcher
    # owner; neither carries its own transfer-level candidate/group semantics.
    group = read("ui-group-candidates.js")
    assert "window.DPGroupCandidates = Object.freeze" in group
    assert "function computeGroup" in group and "function launcherMarkup" in group

    for surface in ("ui-downloads-presentation.js", "ui-dashboard-transfer-presentation.js"):
        source = read(surface)
        assert "DPGroupCandidates" in source and "launcherMarkup" in source
        assert "candidateChipMarkup" not in source
        assert "candidate_source_max" not in source

    marker_owners = [
        path.name for path in STATIC.glob("*.js")
        if "window.DPGroupCandidates = Object.freeze" in path.read_text(encoding="utf-8")
    ]
    assert marker_owners == ["ui-group-candidates.js"]

    provider = read("ui-provider-status.js")
    index = read("index.html")
    # The shared runtime is loaded once, as an ordered defer script (like the
    # per-file candidate owner), not through the bounded presentation loader.
    assert index.count("/ui-group-candidates.js?v=1") == 1
    assert "ui-group-candidates.js" not in provider
    style = read("style-v11.css")
    assert style.count("/ui-group-candidates.css") == 1
    assert "@import url('/ui-group-candidates.css?v=1');" in style


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


# ── Universal file-selection modal ownership (specification section 62) ──────

def _all_js() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def test_shared_modal_shell_has_exactly_one_overlay_modal_and_footer() -> None:
    index = read("index.html")
    assert index.count('id="overlay"') == 1
    assert index.count('id="modal"') == 1
    assert index.count('id="modal-footer"') == 1
    modal_fragment = index[index.index('id="modal"'):index.index('id="toasts"')]
    assert 'id="modal-body"' in modal_fragment
    assert 'id="modal-footer"' in modal_fragment
    assert 'class="modal-ftr dp-card__footer"' in modal_fragment
    assert "dp-file-selection-overlay" not in index
    for name in _all_js():
        assert "dp-file-selection-overlay" not in name.read_text(encoding="utf-8"), name.name


def test_app_js_is_the_sole_modal_coordinator_and_showdetail_owner() -> None:
    app = read("app.js")
    assert "const DPModal = (function" in app
    assert "window.DPModal = DPModal" in app
    assert "window.showDetail = showDetail" in app
    assert "window.closeModal = closeModal" in app
    assert "debridpulse:detail-rendered" in app
    assert "debridpulse:detail-closed" in app
    assert "requestModalClose" in app
    for name in _all_js():
        if name.name == "app.js":
            continue
        source = name.read_text(encoding="utf-8")
        assert "window.showDetail =" not in source, name.name
        assert "window.showDetail=" not in source, name.name
        assert "window.closeModal =" not in source, name.name
        assert "window.closeModal=" not in source, name.name


def test_detail_candidates_listens_to_lifecycle_events_without_wrapping_globals() -> None:
    runtime = read("ui-detail-candidates.js")
    assert "window.showDetail" not in runtime
    assert "window.closeModal" not in runtime
    assert "dpCandidateWrapped" not in runtime
    assert "debridpulse:detail-rendered" in runtime
    assert "debridpulse:detail-closed" in runtime


def test_file_selection_has_one_bounded_runtime_and_style_owner() -> None:
    assert (STATIC / "ui-file-selection.js").exists()
    assert (STATIC / "ui-file-selection.css").exists()

    style = read("style-v11.css")
    assert style.count("/ui-file-selection.css") == 1
    assert "@import url('/ui-file-selection.css?v=1');" in style

    provider = read("ui-provider-status.js")
    assert provider.count("/ui-file-selection.js?v=1") == 1
    assert "'DPFileSelection'" in provider

    runtime = read("ui-file-selection.js")
    assert "window.DPFileSelection = Object.freeze" in runtime
    assert "createElement('link')" not in runtime
    assert "createElement('style')" not in runtime
    assert "rel = 'stylesheet'" not in runtime
    assert "'/ui-file-selection.css" not in runtime
    for banned in ("ui-correction", "ui-compat"):
        assert banned not in runtime
        assert banned not in style
    assert "refreshAuthoritative" in runtime
    for native in ("alldebrid", "statusCode", "magnet/files", "provider_resource_id", "selection_id"):
        assert native not in runtime

    marker_owners = [
        path.name for path in _all_js()
        if "window.DPFileSelection = Object.freeze" in path.read_text(encoding="utf-8")
    ]
    assert marker_owners == ["ui-file-selection.js"]


def test_file_selection_boot_entry_follows_the_bounded_owner_list() -> None:
    provider = read("ui-provider-status.js")
    ordered = (
        "/ui-toast-contract.js?v=2",
        "/ui-processing-presentation.js?v=1",
        "/ui-dashboard-transfer-presentation.js?v=3",
        "/ui-downloads-presentation.js?v=2",
        "/ui-activity-log-runtime.js?v=1",
        "/ui-settings-archive-passwords.js?v=1",
        "/ui-file-selection.js?v=1",
    )
    positions = [provider.index(item) for item in ordered]
    assert positions == sorted(positions)
    assert "bootPresentationOwners()" in provider


def test_details_exposes_a_stable_file_selection_action_host() -> None:
    app = read("app.js")
    assert 'id="dp-detail-actions"' in app
    runtime = read("ui-file-selection.js")
    assert "dp-detail-actions" in runtime
    assert "Select files" in runtime
    assert "Change file selection" in runtime
