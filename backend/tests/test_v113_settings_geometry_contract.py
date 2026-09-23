"""DP 1.0.13 post-Usenet corrective pass, work items I, J and K.

These are the OWNERSHIP halves of three geometry corrections; the rendered
geometry itself is proved in the browser suite (`settings-field-geometry.spec.js`,
`route-history-spacing.spec.js`).

* J -- one canonical Settings field datum: a field's label, its control and its
  helper copy all begin at the control's outer-box inline edge. The previous
  tree declared a 3px "content datum" SIX times across five files and gave it
  to every panel except the Usenet server cards, which is exactly why those
  labels read as sitting to the left of every other Settings label.
* K -- one canonical single-line checkbox row whose control is optically
  centred on the label text by the font's own metrics.
* I -- one explicit constant Route History provenance/status gap.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
FORM_LAYOUT = (STATIC / "ui-settings-form-layout.css").read_text(encoding="utf-8")
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
TRANSFER_CSS = (STATIC / "ui-transfer-contract.css").read_text(encoding="utf-8")

def _rules(css: str):
    """Declaration blocks with comments stripped, so prose describing a retired
    declaration is never mistaken for the declaration."""
    return re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))


OFFSET_OWNERS = (
    "ui-settings-downloads-completion.css",
    "ui-settings-notifications.css",
    "ui-settings-maintenance-wipe.css",
    "ui-settings-authentication.css",
    "ui-settings-page.css",
    "ui-settings-usenet-servers.css",
)


# --- J: one canonical field datum -----------------------------------------

def _inline_start_offset(body: str) -> str | None:
    """The inline-start offset a declaration block actually applies, if any.

    Matched by VALUE rather than by a negative lookahead: `\\s*` can backtrack to
    zero width, which makes a lookahead pass on a value it was meant to exclude.
    """
    match = re.search(r"(?:inset-inline-start|(?<![-\w])left)\s*:\s*([^;}]+)", body)
    if not match:
        return None
    value = match.group(1).strip().lower()
    return None if value in {"auto", "0", "0px", "inherit", "initial", "unset"} else value


def test_no_settings_stylesheet_nudges_a_field_label_or_hint_inward():
    """A per-panel inline-start offset is exactly the compensating nudge this
    correction removes; the canonical owner establishes the datum instead."""
    offenders = []
    for name in OFFSET_OWNERS:
        css = (STATIC / name).read_text(encoding="utf-8")
        for rule in _rules(css):
            selector, body = rule.group(1), rule.group(2)
            if ".form-label" not in selector and ".form-hint" not in selector:
                continue
            if _inline_start_offset(body):
                offenders.append(f"{name}: {' '.join(selector.split())[:90]}")
    assert offenders == [], offenders


def test_the_never_assigned_auth_inset_variable_is_gone():
    css = (STATIC / "ui-settings-authentication.css").read_text(encoding="utf-8")
    assert "--dp-settings-auth-input-text-inset" not in css


def test_the_canonical_field_datum_is_declared_once_by_the_form_layout_owner():
    assert "dp-settings-field > .form-label" in FORM_LAYOUT
    assert "dp-settings-field > .form-hint" in FORM_LAYOUT
    assert "dp-usenet-field > .form-label" in FORM_LAYOUT
    block = FORM_LAYOUT[FORM_LAYOUT.index("dp-settings-field > .form-label"):]
    block = block[:block.index("}") + 1]
    assert "margin-inline-start: 0" in block
    assert "padding-inline-start: 0" in block


def test_no_usenet_only_alignment_compensation_exists():
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector, body = rule.group(1), rule.group(2)
        if "dp-usenet-field" not in selector:
            continue
        assert "margin-left" not in body and "margin-inline-start" not in body
        assert "transform" not in body


# --- K: one canonical checkbox row ----------------------------------------

def test_the_canonical_checkbox_row_is_declared_once_by_the_form_layout_owner():
    assert ".dp-settings-inline-check {" in FORM_LAYOUT
    block = FORM_LAYOUT[FORM_LAYOUT.index(".dp-settings-inline-check {"):]
    block = block[:block.index("}", block.index('input[type="checkbox"]')) + 1]
    # Optical centring comes from the font's own metrics, never a pixel nudge.
    assert "vertical-align: middle" in block
    assert not re.search(r"(?:top|margin-top|transform)\s*:\s*-?\d", block)
    # Its GEOMETRY is declared in exactly one stylesheet; other sheets may
    # still carry colour/spacing for the same class.
    geometry = re.compile(r"display\s*:|align-items\s*:|vertical-align\s*:|gap\s*:")
    offenders = []
    for name in OFFSET_OWNERS:
        css = (STATIC / name).read_text(encoding="utf-8")
        # Comments describe the geometry; they do not declare it.
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            if "dp-settings-inline-check" in rule.group(1) and geometry.search(rule.group(2)):
                offenders.append(f"{name}: {' '.join(rule.group(1).split())[:90]}")
    assert offenders == [], offenders


def test_the_clear_password_row_converges_on_the_shared_owner():
    block = SETTINGS_JS[SETTINGS_JS.index("function usenetServerCard("):
                        SETTINGS_JS.index("function usenetAddTile(")]
    row = block[block.index("data-usenet-clear-password") - 400:block.index("data-usenet-clear-password") + 200]
    assert "dp-settings-inline-check" in row
    # Interaction truth is unchanged: the label still wraps the control.
    assert "<label" in row
    assert "Clear the stored password for this server" in row


def test_the_private_checkbox_geometry_is_gone():
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    block = css[css.index(".dp-usenet-clear-password {"):]
    block = block[:block.index("}") + 1]
    assert "align-items" not in block
    assert "display: flex" not in block


# --- I: one explicit constant Route History gap ---------------------------

def test_the_provenance_status_gap_is_one_declared_constant():
    row = TRANSFER_CSS[TRANSFER_CSS.index(".dp-detail-route-row {"):]
    row = row[:row.index("}") + 1]
    assert "--dp-route-provenance-gap" in row
    assert "--dp-route-column-gap" in row
    assert "column-gap: var(--dp-route-column-gap)" in row
    outcome = TRANSFER_CSS[TRANSFER_CSS.index(".dp-detail-route-outcome {"):]
    outcome = outcome[:outcome.index("}") + 1]
    assert "calc(var(--dp-route-provenance-gap) - var(--dp-route-column-gap))" in outcome


def test_provenance_is_right_aligned_and_the_url_keeps_the_flexible_space():
    relation = TRANSFER_CSS[TRANSFER_CSS.index(".dp-detail-route-relation {"):]
    relation = relation[:relation.index("}") + 1]
    assert "text-align: right" in relation or "text-align:right" in relation
    assert "justify-self: end" in relation or "justify-self:end" in relation
    # DP 1.0.13 Item 1: the five tracks are declared ONCE on the list and every
    # row spans them as a subgrid, so provenance occupies one shared column
    # across the whole route history instead of being re-sized per row.
    listing = TRANSFER_CSS[TRANSFER_CSS.index(".dp-detail-route-list {"):]
    listing = listing[:listing.index("}") + 1]
    # The identity column keeps every flexible pixel; nothing fixed is reserved
    # for provenance or status.
    assert "minmax(0,1fr)" in listing.replace(" ", "")
    assert re.search(r"grid-template-columns:[^;]*auto\s+auto", listing)
    row = TRANSFER_CSS[TRANSFER_CSS.index(".dp-detail-route-row {"):]
    row = row[:row.index("}") + 1]
    assert "subgrid" in row
    assert "grid-column:1/-1" in row.replace(" ", "")


def test_the_responsive_stack_releases_the_inline_offset():
    mobile = TRANSFER_CSS[TRANSFER_CSS.index("@media (max-width:700px)"):]
    assert "dp-detail-route-outcome" in mobile
    assert "margin-inline-start: 0" in mobile
