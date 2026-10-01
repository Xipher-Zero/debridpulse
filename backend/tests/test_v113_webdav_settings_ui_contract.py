"""DP 1.0.13 WebDAV Settings and presentation: one owner for every fact.

The WebDAV identity (Lucide CloudSync, Sky Blue #38BDF8) lives in the one
Settings protocol-chip owner; its Network Sources box and its Transfer Method
card are the existing composers fed by published metadata; its one tunable
commits through the canonical integration scope; and the browser consumes the
backend's origin projection without deriving anything itself.
"""
from __future__ import annotations

import re
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_identity_is_declared_once_in_the_central_owners():
    page = _read("ui-settings-page.js")
    css = _read("ui-settings-card-icons.css")
    assert page.count("general_webdav: 'cloud-sync',") == 1
    assert re.search(r"\[data-protocol='general_webdav'\] \{\s*--dp-protocol-color: #38BDF8;\s*\}", css)
    glyph = (STATIC / "icons/lucide/cloud-sync.svg").read_text()
    assert 'stroke="#38BDF8"' in glyph and "Lucide cloud-sync @ 23f9abc4ed0146cffededd3d7f94c1018bfdf693" in glyph
    # Nowhere else hand-codes the colour or the glyph.
    for path in list(STATIC.glob("*.js")) + list(STATIC.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        if path.name not in {"ui-settings-card-icons.css"}:
            assert "#38bdf8" not in text.casefold(), path.name
        if path.name not in {"ui-settings-page.js"}:
            assert "cloud-sync" not in text, path.name


def test_the_network_sources_box_is_metadata_plus_copy_only():
    page = _read("ui-settings-page.js")
    assert "general_webdav: ['Direct downloads from', 'WebDAV files and folders.']," in page
    # No WebDAV-specific aggregate, status or group calculation anywhere.
    for name in ("ui-provider-status.js", "app.js", "ui-settings-page.js"):
        text = _read(name)
        occurrences = [line.strip() for line in text.splitlines()
                       if "general_webdav" in line and not line.strip().startswith(("*", "//", "/*"))]
        allowed = {"general_webdav: 'cloud-sync',", "general_webdav: ['Direct downloads from', 'WebDAV files and folders.'],",
                   "const webdavOf = s => s?.integrations?.general_webdav?.options || {};",
                   "const INTEGRATION_SCOPES = Object.freeze(['alldebrid', 'aria2', 'usenet', 'rsync', 'general_webdav']);",
                   "webdav_directory_depth: {scope: 'integration:general_webdav', option: 'directory_depth'},",
                   "'Which files DebridPulse collects from WebDAV folders.', webdavTuning(s), 'general_webdav') +"}
        assert set(occurrences) <= allowed, (name, set(occurrences) - allowed)


def test_directory_depth_maps_exactly_onto_the_backend_value_model():
    from providers.general_webdav.definition import DIRECTORY_DEPTHS, GeneralWebdavOptions
    page = _read("ui-settings-page.js")
    block = page.split("const WEBDAV_DIRECTORY_DEPTHS = Object.freeze([", 1)[1].split("]);", 1)[0]
    values = re.findall(r"\['([^']+)', '([^']+)'\]", block)
    literal = typing.get_args(GeneralWebdavOptions.model_fields["directory_depth"].annotation)
    assert [value for value, _label in values] == list(literal) == list(DIRECTORY_DEPTHS)
    assert [label for _value, label in values] == ["Current directory only", "1 subdirectory level",
                                                  "2 subdirectory levels", "3 subdirectory levels",
                                                  "All subdirectories"]
    assert GeneralWebdavOptions().directory_depth == "current"
    tuning = page.split("function webdavTuning(s) {", 1)[1].split("\n  }\n", 1)[0]
    # The existing grid and its bordered cell, the existing select, and the
    # existing closing sentence -- one tunable is one cell, never a group of one.
    assert "tuningCells(" in tuning and "selectField('webdav_directory_depth', 'Directory Depth'" in tuning
    assert "tuningGroup(" not in tuning and 'class="dp-settings-tuning-footer"' in tuning
    assert "options.directory_depth || 'current'" in tuning
    for jargon in ("PROPFIND", "Depth:", "infinity", "multistatus"):
        assert jargon not in tuning and jargon not in block


def test_the_card_and_the_commit_scope_are_the_canonical_ones():
    page = _read("ui-settings-page.js")
    assert ("executorTuningCard('webdav', 'WebDAV',\n        'Which files DebridPulse collects from WebDAV folders.', "
            "webdavTuning(s), 'general_webdav')") in page
    assert "webdav_directory_depth: {scope: 'integration:general_webdav', option: 'directory_depth'}," in page
    assert "'general_webdav']);" in page.split("const INTEGRATION_SCOPES", 1)[1].split("\n", 1)[0]


def test_the_browser_never_infers_a_webdav_origin():
    app = _read("app.js")
    # WebDAV appears in app.js only as accepted Quick Add syntax and its wording.
    lines = [line.strip() for line in app.splitlines() if "dav" in line.casefold()]
    assert all("(?:web)?davs?" in line or "WebDAV link" in line for line in lines), lines
    for name in ("ui-dashboard-transfer-presentation.js", "ui-downloads.js", "ui-provider-status.js"):
        text = _read(name).casefold()
        assert "webdav" not in text and "origin_provider" not in text, name


def test_route_history_names_a_declined_route_neutrally():
    app = _read("app.js")
    labels = app.split("function routeOutcomePresentation(value) {", 1)[1].split("};", 1)[0]
    assert "declined: 'Not Applicable'," in labels


def test_selectors_share_one_tuning_geometry_that_reads_values_whole():
    css = _read("ui-settings-page.css")
    # One bound for every tuning selector: 160px of value text plus the trigger's own 54px.
    assert css.count("--dp-tuning-select-max: 214px;") == 1
    rule = css.split("/* A selector is the same cell control", 1)[1].split("}", 1)[0]
    assert "select.input" in rule and ".dp-dropdown-shell" in rule
    assert "width: min(100%, var(--dp-tuning-select-max)) !important;" in rule
    # Every set holding a selector gets lanes of one selector cell (the bound
    # plus the cell's 22px of padding and border), so EVERY selector -- File
    # Allocation as much as Directory Depth -- reads its values whole wherever
    # its cell lands. Cardinalities themselves are untouched.
    assert "--dp-tuning-select-lane: calc(var(--dp-tuning-select-max) + 22px);" in css
    assert ("#view-settings .dp-settings-tuning-grid:has(select.input) "
            "{ --dp-tuning-lane-min: var(--dp-tuning-select-lane); }") in css
    assert '[data-tuning-lanes="1"]' not in css
    for lanes, minimum in (("4", "200px"), ("5", "160px"), ("6", "140px"), ("7", "120px")):
        assert (f'[data-tuning-lanes="{lanes}"] {{ --dp-tuning-lanes: {lanes}; --dp-tuning-lane-min: {minimum}; }}'
                in css)
    # Relation outlines switch on only where the set holds all its lanes: the
    # selector sets at N x 236px + (N - 1) x 16px, the others as before.
    for lanes, plain, selector in (("4", 848, 992), ("5", 864, 1244), ("7", 936, 1748)):
        assert (f"@container dp-tuning (min-width: {plain}px) {{\n  #view-settings .dp-settings-tuning-grid"
                f'[data-tuning-lanes="{lanes}"]:not(:has(select.input)) .dp-settings-tuning-group') in css
        assert (f"@container dp-tuning (min-width: {selector}px) {{\n  #view-settings .dp-settings-tuning-grid"
                f'[data-tuning-lanes="{lanes}"]:has(select.input) .dp-settings-tuning-group') in css
        assert int(lanes) * 236 + (int(lanes) - 1) * 16 == selector
    # No WebDAV-specific width anywhere.
    assert "webdav" not in css.casefold()


def test_the_input_question_names_another_authority_as_text_only():
    modal = _read("ui-auth-required.js")
    assert "const authority = text(active.challenge && active.challenge.authority).trim();" in modal
    assert "querySelector('[data-dp-auth-authority]').textContent = `Sign in to ${authority}`" in modal
    # Shown as text, never markup; no protocol or URL logic in the modal.
    assert "${authority}</" not in modal and "dav" not in modal.casefold()
