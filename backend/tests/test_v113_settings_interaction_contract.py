"""DP 1.0.13 post-Usenet corrective pass, work items B, C, D and E.

* B -- browser-native dialogs are forbidden in maintained operator UI; the one
  application dialog owner (``DPSettingsModal``) gains the missing prompt shape.
* C -- exactly one canonical disclosure control, immediately after the card
  title, shared by Services and Downloads -> Executor Tuning.
* D -- every provider/source card explains what enabling it allows.
* E -- header flavour copy is centred against the FULL header, not the flex
  remainder left over after the title.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
MODAL_JS = (STATIC / "ui-settings-modal.js").read_text(encoding="utf-8")
USENET_JS = (STATIC / "ui-settings-usenet-servers.js").read_text(encoding="utf-8")
DOWNLOADS_JS = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
MODAL_CSS = (STATIC / "ui-modal-contract.css").read_text(encoding="utf-8")
SETTINGS_CSS = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
PROVIDER_CSS = (STATIC / "ui-provider-state.css").read_text(encoding="utf-8")
USENET_CSS = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")

MAINTAINED_JS = sorted(STATIC.glob("*.js"))

# --- B: no browser-native dialog survives in maintained operator UI --------

NATIVE_DIALOG = re.compile(
    r"(?:window\s*\.\s*(?:prompt|alert|confirm)\s*\()"
    r"|(?:(?<![\w.$])(?:prompt|alert|confirm)\s*\()"
)


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_no_maintained_frontend_module_uses_a_browser_native_dialog():
    offenders = []
    for path in MAINTAINED_JS:
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for match in NATIVE_DIALOG.finditer(body):
            text = match.group(0)
            # The canonical dialog owner's own methods are named confirm/prompt;
            # they are application dialogs, not browser primitives.
            head = body[:match.start()].rstrip()
            if head.endswith("function") or head.endswith("DPSettingsModal."):
                continue
            offenders.append(f"{path.name}: {text!r}")
    assert offenders == [], offenders


def test_the_canonical_dialog_owner_provides_the_prompt_shape():
    assert "function prompt(" in MODAL_JS
    assert re.search(r"window\.DPSettingsModal\s*=\s*Object\.freeze\(\{[^}]*\bprompt\b", MODAL_JS)
    # Built on the owner's own shell -- never a second overlay/focus implementation.
    body = MODAL_JS[MODAL_JS.index("function prompt("):]
    assert "open({" in body
    assert "dp-modal-field" in body


def test_the_prompt_dialog_adds_no_second_dialog_implementation():
    """The prompt shape is built INSIDE the canonical owner.

    ``ui-auth-required.js`` builds its own overlay for the generic
    INPUT_REQUIRED challenge; that is a pre-existing owner this pass neither
    introduces nor touches. What this asserts is that no NEW overlay/focus-trap
    implementation appeared, and that the Settings dialog family has exactly
    one.
    """
    assert ".dp-modal-field" in MODAL_CSS
    owners = sorted(path.name for path in MAINTAINED_JS
                    if "dp-modal-overlay" in path.read_text(encoding="utf-8")
                    and "document.createElement" in path.read_text(encoding="utf-8"))
    assert owners == ["ui-auth-required.js", "ui-settings-modal.js"], owners
    # The prompt does not build a shell of its own.
    body = MODAL_JS[MODAL_JS.index("function prompt("):MODAL_JS.index("window.DPSettingsModal")]
    assert "dp-modal-overlay" not in body
    assert "addEventListener('keydown'" not in body


def test_server_rename_uses_the_canonical_dialog_and_keeps_its_persistence_boundary():
    block = USENET_JS[USENET_JS.index("function rename("):USENET_JS.index("function onClick(")]
    assert "DPSettingsModal.prompt" in block
    assert "Edit Server Name" in block
    assert "Display Name" in block
    # Prefill is the explicit override only; the derived host never masquerades.
    assert "usenetNameOverride === '1'" in block
    # Card-local until the server card's own Save: no request is issued here.
    assert "api(" not in block


def test_downloads_label_prompt_uses_the_canonical_dialog():
    block = DOWNLOADS_JS[DOWNLOADS_JS.index("async function setLabel("):]
    block = block[:block.index("\n}") + 2]
    assert "DPSettingsModal.prompt" in block


# --- C: one canonical disclosure component --------------------------------

def test_one_canonical_disclosure_class_serves_both_card_families():
    assert SETTINGS_JS.count("dp-settings-disclosure") >= 2
    assert "dp-settings-provider-disclosure" not in SETTINGS_JS
    assert "dp-executor-tuning-disclosure" not in SETTINGS_JS
    for css in (PROVIDER_CSS, USENET_CSS):
        assert "dp-settings-provider-disclosure" not in css
        assert "dp-executor-tuning-disclosure" not in css
    # Declared exactly once, by the Settings page CSS owner.
    assert ".dp-settings-disclosure {" in SETTINGS_CSS


def test_the_disclosure_sits_immediately_after_the_card_title_in_both_families():
    """Both card families compose the SAME component in the SAME position: the
    chip is emitted by ``settingsDisclosure()`` inside the title group, after
    the title."""
    for marker, following in (("function providerCard(", "function usenetServerCard("),
                              ("function executorTuningCard(", "function directTransfersTuning(")):
        block = SETTINGS_JS[SETTINGS_JS.index(marker):SETTINGS_JS.index(following)]
        assert "settingsDisclosure(" in block, marker
        assert "dp-settings-card-title-group" in block, marker
        group = block[block.index("dp-settings-card-title-group"):]
        # Inside the title group the chip is emitted AFTER the title, and the
        # whole group precedes the centred copy and the right-side controls.
        chip = min(index for index in (group.find("settingsDisclosure("), group.find("${disclosure}"))
                   if index != -1)
        assert group.index("card-title") < chip, marker
        assert chip < group.index("dp-settings-card-header-center"), marker


def test_the_disclosure_keeps_its_accessible_state_and_never_swallows_the_header():
    component = SETTINGS_JS[SETTINGS_JS.index("function settingsDisclosure("):
                            SETTINGS_JS.index("function providerStatus(")]
    assert "aria-expanded" in component and "aria-controls" in component
    assert "aria-label" in component
    assert "<button type=\"button\"" in component
    # One behaviour owner, addressing the body through aria-controls.
    behaviour = SETTINGS_JS[SETTINGS_JS.index("function setDisclosureExpanded("):]
    behaviour = behaviour[:behaviour.index("\n  }") + 4]
    assert "getAttribute('aria-controls')" in behaviour
    assert "aria-expanded" in behaviour
    # Enable remains an independent control; no card header is clickable.
    for marker, following in (("function providerCard(", "function usenetServerCard("),
                              ("function executorTuningCard(", "function directTransfersTuning(")):
        block = SETTINGS_JS[SETTINGS_JS.index(marker):SETTINGS_JS.index(following)]
        assert "onclick" not in block, marker
        assert "<div class=\"card-header dp-settings-card-header\">" in block, marker


def test_the_canonical_chip_is_a_ghost_chip_not_a_naked_chevron():
    block = SETTINGS_CSS[SETTINGS_CSS.index(".dp-settings-disclosure {"):]
    block = block[:block.index("}") + 1]
    for token in ("width", "height", "border-radius", "border"):
        assert token in block, token
    assert "rotate(90deg)" in SETTINGS_CSS


# --- D: capability flavour copy -------------------------------------------

USENET_COPY = "Download NZB content from configured Usenet news servers."
ALLDEBRID_COPY = "Resolve supported links and torrents through your AllDebrid account."


def test_every_provider_source_card_explains_what_enabling_it_allows():
    panel = SETTINGS_JS[SETTINGS_JS.index("function sourcesPanel("):
                        SETTINGS_JS.index("const ARIA2_LIVE_FILTERS")]
    assert USENET_COPY in panel
    assert ALLDEBRID_COPY in panel
    # The two expandable Premium Services cards carry their copy in the
    # canonical card header. The Network Sources members are compact protocol
    # BOXES rather than cards with headers, so the same promise is kept as the
    # two centred lines each box presents, and its meaning is preserved.
    assert panel.count("headerCopy:") == 2
    copy = SETTINGS_JS[SETTINGS_JS.index("const SOURCE_BOX_COPY"):]
    copy = copy[:copy.index("});")]
    assert "'Direct downloads from', 'HTTP and HTTPS URLs.'" in copy
    assert "'Direct downloads from', 'FTP and SFTP URLs.'" in copy


def test_no_implementation_name_reaches_operator_copy():
    panel = SETTINGS_JS[SETTINGS_JS.index("function sourcesPanel("):
                        SETTINGS_JS.index("const ARIA2_LIVE_FILTERS")]
    for name in ("SABnzbd", "sabnzbd", "SAB ", "NNTP"):
        assert name not in panel


# --- E: centre copy is centred against the full header --------------------

def test_one_canonical_three_region_settings_card_header():
    assert ".card-header.dp-settings-card-header" in SETTINGS_CSS
    block = SETTINGS_CSS[SETTINGS_CSS.index(".card-header.dp-settings-card-header"):]
    block = block[:block.index("}") + 1]
    assert "display: grid" in block
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)" in block


def test_both_card_families_use_the_canonical_header():
    for marker, following in (("function providerCard(", "function usenetServerCard("),
                              ("function executorTuningCard(", "function directTransfersTuning(")):
        block = SETTINGS_JS[SETTINGS_JS.index(marker):SETTINGS_JS.index(following)]
        assert 'card-header dp-settings-card-header' in block, marker
        assert "dp-settings-card-header-center" in block, marker


def test_the_flex_remainder_centring_is_gone():
    """RED before this pass: .dp-executor-tuning-copy centred inside whatever
    space the title cluster left over, so the apparent centre moved with the
    title's width."""
    assert ".dp-executor-tuning-copy" not in USENET_CSS
    assert ".dp-executor-tuning-title" not in USENET_CSS
    assert "dp-settings-provider-card--alldebrid > .card-header" not in PROVIDER_CSS
    # Neither card family in scope declares its own column model beside the
    # canonical header. (The Global Download Settings card keeps its own
    # three-region grid: a different family, already geometrically centred, and
    # outside this pass's scope.)
    families = ("dp-settings-provider-card", "dp-settings-direct-source-card",
                "dp-executor-tuning-card")
    offenders = []
    for name, css in (("ui-settings-page.css", SETTINGS_CSS),
                      ("ui-provider-state.css", PROVIDER_CSS),
                      ("ui-settings-usenet-servers.css", USENET_CSS)):
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector, body = rule.group(1), rule.group(2)
            if ".card-header" not in selector or "dp-settings-card-header" in selector:
                continue
            if not any(family in selector for family in families):
                continue
            if "grid-template-columns" in body or re.search(r"display\s*:\s*(grid|flex)", body):
                offenders.append(f"{name}: {' '.join(selector.split())[:90]}")
    assert offenders == [], offenders


def test_provider_header_controls_are_one_canonical_right_region():
    block = SETTINGS_JS[SETTINGS_JS.index("function providerCard("):
                        SETTINGS_JS.index("function usenetServerCard(")]
    assert "dp-settings-card-header-controls" in block
    assert "dp-settings-provider-header-controls" not in SETTINGS_JS
    assert "dp-settings-provider-header-controls" not in PROVIDER_CSS
