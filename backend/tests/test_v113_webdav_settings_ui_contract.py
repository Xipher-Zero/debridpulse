"""DP 1.0.13 WebDAV Settings and presentation: one owner for every fact.

The WebDAV identity (Lucide CloudSync, Sky Blue #38BDF8) lives in the one
Settings protocol-chip owner; its Network Sources box and its Transfer Method
card are the existing composers fed by published metadata; its three tunables
are one relationship group of the shared tuning grammar and commit through the
canonical integration scope; and the browser consumes the backend's origin
projection without deriving anything itself.
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
                   "'general_webdav']);",
                   "webdav_directory_depth: {scope: 'integration:general_webdav', option: 'directory_depth'},",
                   "webdav_max_files: {scope: 'integration:general_webdav', option: 'max_files'},",
                   "webdav_collection_scan_timeout_seconds: {scope: 'integration:general_webdav',",
                   "'Which files DebridPulse collects from WebDAV folders.', webdavTuning(s), 'general_webdav') +"}
        assert set(occurrences) <= allowed, (name, set(occurrences) - allowed)


def test_directory_depth_maps_exactly_onto_the_backend_value_model():
    from integrations.definition import DIRECTORY_DEPTHS
    from providers.general_webdav.definition import GeneralWebdavOptions
    page = _read("ui-settings-page.js")
    # ONE operator vocabulary, shared by every source that offers the setting.
    assert page.count("const DIRECTORY_DEPTHS = Object.freeze([") == 1
    assert "WEBDAV_DIRECTORY_DEPTHS" not in page
    block = page.split("const DIRECTORY_DEPTHS = Object.freeze([", 1)[1].split("]);", 1)[0]
    values = re.findall(r"\['([^']+)', '([^']+)'\]", block)
    literal = typing.get_args(GeneralWebdavOptions.model_fields["directory_depth"].annotation)
    assert [value for value, _label in values] == list(literal) == list(DIRECTORY_DEPTHS)
    assert [label for _value, label in values] == ["Current directory only", "1 subdirectory level",
                                                  "2 subdirectory levels", "3 subdirectory levels",
                                                  "All subdirectories"]
    assert GeneralWebdavOptions().directory_depth == "current"
    for jargon in ("PROPFIND", "Depth:", "infinity", "multistatus"):
        assert jargon not in block


def test_the_three_webdav_tunables_are_one_relationship_group_of_the_shared_grammar():
    page = _read("ui-settings-page.js")
    tuning = page.split("function webdavTuning(s) {", 1)[1].split("\n  }\n", 1)[0]
    # The existing grid, ONE relationship group of exactly three bordered
    # cells, and the existing closing sentence beneath them.
    assert tuning.count("tuningCells(") == 1 and tuning.count("tuningGroup(") == 1
    assert 'class="dp-settings-tuning-footer"' in tuning
    order = [tuning.index(needle) for needle in (
        "selectField('webdav_directory_depth', 'Directory Depth', options.directory_depth || 'current',",
        "input('webdav_max_files', 'Maximum Files', options.max_files ?? 10000, {",
        "input('webdav_collection_scan_timeout_seconds', 'Collection Scan Timeout (seconds)',")]
    assert order == sorted(order)
    assert "type: 'number', min: 1, max: 10000," in tuning
    # No limit is the canonical 0, shown as the empty field's own "No limit" --
    # never as a number of seconds.
    assert ("options.collection_scan_timeout_seconds ? options.collection_scan_timeout_seconds : '', {"
            in tuning)
    assert "type: 'number', min: 0, max: 3600, placeholder: 'No limit'," in tuning
    assert "blank: 0}," in page.split("webdav_collection_scan_timeout_seconds: {", 1)[1].split("\n", 2)[1]
    for owner in ("function committedValue(key, raw) {", "function acceptedValue(key, accepted, draft) {"):
        assert "COMMIT_FIELDS[key]?.blank" in page.split(owner, 1)[1].split("\n  }\n", 1)[0], owner
    # Operator language only: no protocol mechanics.
    for jargon in ("PROPFIND", "Depth:", "infinity", "multistatus", "redirect", "ETag", "TLS", "auth"):
        assert jargon not in tuning, jargon


def test_the_card_and_the_commit_scope_are_the_canonical_ones():
    page = _read("ui-settings-page.js")
    assert ("executorTuningCard('webdav', 'WebDAV',\n        'Which files DebridPulse collects from WebDAV folders.', "
            "webdavTuning(s), 'general_webdav')") in page
    assert "webdav_directory_depth: {scope: 'integration:general_webdav', option: 'directory_depth'}," in page
    assert "webdav_max_files: {scope: 'integration:general_webdav', option: 'max_files'}," in page
    assert ("webdav_collection_scan_timeout_seconds: {scope: 'integration:general_webdav',\n"
            "                                             option: 'collection_scan_timeout_seconds', blank: 0},") in page
    scopes = page.split("const INTEGRATION_SCOPES = Object.freeze([", 1)[1].split("]);", 1)[0]
    assert "'general_webdav'" in scopes and "'general_rsync'" in scopes


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
    # The cell holding a selector -- File Allocation as much as Directory Depth
    # -- gets a lane of one selector cell (the bound plus the cell's 22px of
    # padding and border), wherever it lands. That is ITS lane's minimum only:
    # siblings keep the set's own, and no set-wide rule reacts to a selector.
    assert "--dp-tuning-select-lane: calc(var(--dp-tuning-select-max) + 22px);" in css
    assert ("#view-settings .dp-settings-tuning-grid .dp-settings-field:has(select.input) {\n"
            "  --dp-tuning-cell-min: var(--dp-tuning-select-lane);\n}") in css
    assert ".dp-settings-tuning-grid:has(" not in css
    for lanes, minimum in (("1", "160px"), ("2", "160px"), ("3", "160px"), ("4", "200px"), ("5", "160px"),
                           ("6", "140px"), ("7", "120px")):
        assert (f'[data-tuning-lanes="{lanes}"] {{ --dp-tuning-lanes: {lanes}; --dp-tuning-lane-min: {minimum}; }}'
                in css)
    # No WebDAV-specific width anywhere.
    assert "webdav" not in css.casefold()


def test_the_input_question_names_another_authority_as_text_only():
    modal = _read("ui-auth-required.js")
    assert "const authority = text(active.challenge && active.challenge.authority).trim();" in modal
    assert "querySelector('[data-dp-auth-authority]').textContent = `Sign in to ${authority}`" in modal
    # Shown as text, never markup; no protocol or URL logic in the modal.
    assert "${authority}</" not in modal and "dav" not in modal.casefold()
