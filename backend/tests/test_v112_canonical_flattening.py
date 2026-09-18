"""DP 1.0.12 canonical flattening pass — negative and positive proof.

Enforces semantic boundaries for the CANON-001 (Downloads has two active
owners), CANON-001b (Provider Status is an accidental application bootstrap
loader), and CANON-002 (file-selection affordance classified twice) findings,
plus the CSS version-layer flattening. See TASK_DebridPulse_1.0.12_Canonical
_Flattening_Implementation_Prompt.md section 17.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def all_js() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


# ── CANON-001b: Provider Status owns provider status only ──────────────────

def test_provider_status_has_no_presentation_loader() -> None:
    provider = read("ui-provider-status.js")
    assert "bootPresentationOwners" not in provider
    assert "PRESENTATION_OWNERS" not in provider
    assert "createElement('script')" not in provider
    assert 'createElement("script")' not in provider
    assert "debridpulse:presentation-ready" not in provider


def test_required_ui_scripts_are_explicit_defer_dependencies_not_injected() -> None:
    index = read("index.html")
    for name in (
        "ui-toast-contract.js", "ui-processing-presentation.js",
        "ui-transfer-source-presentation.js", "ui-file-selection.js",
        "ui-dashboard-transfer-presentation.js", "ui-downloads.js",
        "ui-activity-log-runtime.js", "ui-settings-archive-passwords.js",
    ):
        assert f'src="/{name}' in index, name
        assert index.count(f'src="/{name}') == 1, name


def test_each_canonical_runtime_marker_has_exactly_one_owner() -> None:
    markers = (
        "window.DPToastContract", "window.DPProcessingPresentation",
        "window.DPTransferSourcePresentation", "window.DPFileSelection",
        "window.DPActivityLog", "window.DPArchivePasswords",
        "window.DPGroupCandidates", "window.DPProviderStatus",
    )
    for marker in markers:
        owners = [
            path.name for path in all_js()
            if f"{marker} = " in path.read_text(encoding="utf-8")
            or f"{marker}=" in path.read_text(encoding="utf-8")
        ]
        assert len(owners) == 1, f"{marker} has definition owners {owners}"


# ── CANON-001: exactly one Downloads owner, no wrapper/patch pattern ───────

def test_no_second_downloads_load_render_owner() -> None:
    assert not (STATIC / "ui-downloads-presentation.js").exists()
    assert not (STATIC / "ui-downloads-runtime.js").exists()
    owners = [path.name for path in all_js() if "window.loadTorrents = loadTorrents" in path.read_text(encoding="utf-8")]
    assert owners == ["ui-downloads.js"]


def test_downloads_owner_never_wraps_or_reassigns_another_owners_globals() -> None:
    for path in all_js():
        source = path.read_text(encoding="utf-8")
        assert "const baseApi = api" not in source, path.name
        assert "const baseApi=api" not in source, path.name
        assert "const canonical = loadTorrents" not in source, path.name
        assert "const canonical=loadTorrents" not in source, path.name
        assert "loadTorrents = async function" not in source, path.name
        assert "loadTorrents=async function" not in source, path.name
        assert "renderTorrentPagination = renderPagination" not in source, path.name
        assert "oldFmt" not in source, path.name
        assert "fmtDate = dateMarkup" not in source, path.name
        assert "fmtDate=dateMarkup" not in source, path.name


def test_no_other_owner_reassigns_badge_or_transferdisplaystatus_or_settings_globals() -> None:
    for path in all_js():
        source = path.read_text(encoding="utf-8")
        for pattern in (
            "const canonical=badge", "const canonical = badge",
            "const canonical=transferDisplayStatus", "const canonical = transferDisplayStatus",
            "const canonical=loadAria2SpeedLimit", "const canonical = loadAria2SpeedLimit",
            "const canonical=renderSettings", "const canonical = renderSettings",
            "const canonical=renderTopbarActions", "const canonical = renderTopbarActions",
        ):
            assert pattern not in source, f"{path.name} contains {pattern!r}"


def test_pauseable_presentation_set_has_exactly_one_owner_with_no_fallback_copy() -> None:
    # DP 1.0.12 Gate 9 fix-forward: ui-downloads.js and
    # ui-dashboard-transfer-presentation.js previously each carried their own
    # literal fallback copy of the canonical pause-eligible presentation-status
    # set "in case ui-processing-presentation.js failed to load" -- but the
    # explicit <script defer> dependency graph in index.html already
    # guarantees load order, so a missing owner is a visible bug, not
    # something a second definition should silently paper over.
    literal = "['downloading','queued','recovering','waiting_for_retry','waiting_for_provider','waiting_for_storage','waiting_for_executor']"
    owners = [
        path.name for path in all_js()
        if literal in path.read_text(encoding="utf-8").replace(" ", "").replace('"', "'")
    ]
    assert owners == ["ui-processing-presentation.js"], owners

    for path in all_js():
        if path.name == "ui-processing-presentation.js":
            continue
        source = path.read_text(encoding="utf-8")
        assert "PAUSEABLE_PRESENTATION||" not in source.replace(" ", ""), path.name
        assert "PAUSEABLE_PRESENTATION ||" not in source, path.name

    downloads = read("ui-downloads.js")
    dashboard = read("ui-dashboard-transfer-presentation.js")
    assert "new Set(window.DPProcessingPresentation.PAUSEABLE_PRESENTATION)" in downloads
    assert "new Set(window.DPProcessingPresentation.PAUSEABLE_PRESENTATION)" in dashboard


def test_badge_and_transferdisplaystatus_natively_consume_presentation_status() -> None:
    app = read("app.js")
    assert "presentation_status" in app
    assert "presentation_label" in app
    assert "presentation_badge_status" in app


# ── CANON-002: one backend file-selection affordance semantic owner ────────

def test_browser_file_selection_affordance_classifier_is_gone() -> None:
    runtime = read("ui-file-selection.js")
    assert "function classifyView" not in runtime
    assert "classifyView" not in runtime


def test_browser_file_selection_consumes_backend_affordance_field() -> None:
    runtime = read("ui-file-selection.js")
    assert "file_selection_affordance" in runtime
    # DP 1.0.12 Gate 9 fix-forward: the browser must not gate the field on
    # `view.eligible` either -- the ineligible/no-generation case is itself
    # classified 'none' by the backend, so `affordanceOf` only ever reads
    # `view.file_selection_affordance`, never reconstructs from `eligible`.
    assert "function affordanceOf(view) {\n    return (view && view.file_selection_affordance) || 'none';\n  }" in runtime
    assert "view.eligible && view.file_selection_affordance" not in runtime


def test_backend_has_one_file_selection_affordance_domain_function() -> None:
    fs = (ROOT / "backend" / "transfers" / "file_selection.py").read_text(encoding="utf-8")
    assert "def file_selection_affordance(" in fs

    operational_downloads = (ROOT / "backend" / "api" / "operational_downloads.py").read_text(encoding="utf-8")
    assert "def _file_selection_affordance(" not in operational_downloads
    assert "fs.file_selection_affordance(" in operational_downloads

    repository = (ROOT / "backend" / "transfers" / "repository.py").read_text(encoding="utf-8")
    assert "fs.file_selection_affordance(" in repository

    routes = (ROOT / "backend" / "api" / "file_selection_routes.py").read_text(encoding="utf-8")
    assert '"file_selection_affordance"' in routes


def test_backend_ineligible_fresh_read_also_reports_the_canonical_affordance() -> None:
    # DP 1.0.12 Gate 9 fix-forward: both ineligible-return paths (unknown
    # transfer, and a real transfer with no selection generation) go through
    # the same helper, which calls the one canonical domain function rather
    # than hardcoding "none" a second time.
    routes = (ROOT / "backend" / "api" / "file_selection_routes.py").read_text(encoding="utf-8")
    assert "def _ineligible_view()" in routes
    assert "fs.file_selection_affordance(None, None, None, 0)" in routes
    assert routes.count("return _ineligible_view()") == 2


# ── No compatibility surface without a real runtime consumer ───────────────

def test_no_test_only_compatibility_globals_survive() -> None:
    # DP 1.0.12 Gate 9 fix-forward: an empty marker or a private-state getter
    # whose only consumer was a browser test is exactly the compatibility
    # residue this pass is supposed to flatten -- each was either removed or
    # (for DPDownloads.scheduleCapacityCheck, which ui-processing
    # -presentation.js's syncPauseUi() genuinely calls) kept because a real
    # production consumer exists.
    for path in all_js():
        source = path.read_text(encoding="utf-8")
        assert "DPDashboardTransferPresentation" not in source, path.name
        assert "window.renderTorrentPagination" not in source, path.name

    downloads = read("ui-downloads.js")
    assert "DPDownloads.selectedIds" not in downloads
    assert "DPDownloads.pageSize" not in downloads
    assert "window.DPDownloads = Object.freeze({scheduleCapacityCheck});" in downloads

    processing = read("ui-processing-presentation.js")
    assert "window.DPDownloads?.scheduleCapacityCheck?.()" in processing


# ── CSS version-layer flattening ────────────────────────────────────────────

def test_style_v11_css_is_gone() -> None:
    assert not (STATIC / "style-v11.css").exists()
    index = read("index.html")
    assert "style-v11.css" not in index


# ── dp-v11-structural scoping-class removal (Gate 9 fix-forward, round 2) ───
# The class was a specificity-boosting device applied unconditionally to
# <body> so a scoped component rule would always beat an unscoped legacy
# rule regardless of import order. Cascade order is now correct on its own
# (see test_no_legacy_foundation_css below), so nothing needs the crutch.

def test_dp_v11_structural_class_has_zero_occurrences() -> None:
    result = subprocess.run(
        ["git", "grep", "-n", "dp-v11-structural", "--", "frontend/static"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0, f"dp-v11-structural still present:\n{result.stdout}"


def test_body_element_carries_no_scoping_class() -> None:
    index = read("index.html")
    assert '<body class="dp-v11-structural">' not in index
    assert re.search(r"<body\b[^>]*\bclass=", index) is None, (
        "index.html <body> should carry no class attribute at all now that "
        "dp-v11-structural is gone"
    )


def test_no_legacy_foundation_css() -> None:
    assert not (STATIC / "ui-legacy-foundation.css").exists()
    style = read("style.css")
    assert "@import url('/ui-legacy-foundation.css" not in style
    assert "@import" in style
    # style.css must stay a pure @import list -- no literal rule may follow
    # the imports (that would recreate the same "hidden base layer" pattern
    # the retired ui-legacy-foundation.css was invented to work around).
    lines = [ln for ln in style.splitlines() if ln.strip() and not ln.strip().startswith("*") and not ln.strip().startswith("/*")]
    non_import = [ln for ln in lines if not ln.strip().startswith("@import")]
    assert non_import == [], f"style.css has non-@import content: {non_import}"


# Names that read as a generic "everything that didn't fit elsewhere" base
# layer -- the exact failure mode this pass closed out. A handful of files
# are legitimately broad (design tokens, generic component material) and are
# named, bounded and documented as such; they are the only allowed exception.
_DUMPING_GROUND_NAME = re.compile(r"(legacy|misc|base|foundation|catch.?all|^style-v\d+)", re.IGNORECASE)
_ALLOWED_BROAD_OWNERS = {
    "design-tokens.css", "ui-language-tokens.css", "ui-foundation.css",
    "ui-components.css", "ui-universal-language.css", "ui-shared-contract.css",
}

def test_no_future_legacy_dumping_ground_stylesheet() -> None:
    style = read("style.css")
    imported = re.findall(r"@import url\('/([\w.-]+\.css)", style)
    assert len(imported) > 30, "sanity: expected the full component import graph"
    offenders = [
        name for name in imported
        if name not in _ALLOWED_BROAD_OWNERS and _DUMPING_GROUND_NAME.search(name)
    ]
    assert offenders == [], (
        f"{offenders} read like a generic legacy/base dumping-ground stylesheet; "
        "give each rule a real per-responsibility owner instead of a new catch-all file"
    )
    for name in imported:
        assert (STATIC / name).exists(), f"style.css imports {name} but the file is missing"


# Gate 9 round 3 fix-forward: ui-provider-state.css used to load as its own
# top-level <link>, bypassing the canonical graph entirely (Section 9's named
# "style.css + style-v11.css + extra top-level provider-state CSS" problem,
# only ever half-fixed). style.css is now the one first-party application
# stylesheet index.html links to.
_STYLESHEET_LINK = re.compile(r'<link\b[^>]*\brel=["\']stylesheet["\'][^>]*>', re.IGNORECASE)
_FIRST_PARTY_HREF = re.compile(r'href=["\']\/[\w.-]+\.css')

def test_index_html_has_exactly_one_first_party_stylesheet_entry_point() -> None:
    index = read("index.html")
    first_party = [
        tag for tag in _STYLESHEET_LINK.findall(index) if _FIRST_PARTY_HREF.search(tag)
    ]
    assert first_party == ['<link rel="stylesheet" href="/style.css?v=18">'], (
        f"index.html must link exactly one first-party stylesheet (style.css); "
        f"found {first_party}"
    )
    assert 'id="dp-provider-state-css"' not in index
    assert "ui-provider-state.css" not in index


def test_provider_state_css_is_loaded_through_the_canonical_graph() -> None:
    style = read("style.css")
    assert "@import url('/ui-provider-state.css" in style
    assert not (STATIC / "ui-provider-summary.css").exists()
    assert "ui-provider-summary.css" not in style


def test_no_second_downloads_stylesheet_layer() -> None:
    assert not (STATIC / "ui-downloads-presentation.css").exists()
    assert not (STATIC / "ui-downloads-desktop.css").exists()
    style = read("style.css")
    assert "ui-downloads-presentation.css" not in style
    assert "ui-downloads-desktop.css" not in style


def test_duplicate_owner_markers_are_gone() -> None:
    dashboard = read("ui-dashboard-transfer-presentation.js")
    assert "hostAsset" not in dashboard
    assert "sourceIconMarkup" not in dashboard
    assert "HOST_ASSETS" not in dashboard
    owners = [path.name for path in all_js() if "const HOST_ASSETS" in path.read_text(encoding="utf-8")]
    assert owners == ["ui-transfer-source-presentation.js"]
