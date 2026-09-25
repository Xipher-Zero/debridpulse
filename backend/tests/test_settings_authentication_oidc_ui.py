from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
AUTH_CSS = STATIC / "ui-settings-authentication.css"
SETTINGS_JS = STATIC / "ui-settings-page.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_clearing_the_stored_oidc_secret_is_one_confirmed_action_not_a_deferred_checkbox():
    """Erasing a credential is an explicit confirmed act, with ONE path.

    The former checkbox armed a clear that only happened at Apply, which meant a
    destructive intent could sit latent in the page and be carried by an
    unrelated save. Both the checkbox and its clear-on-save payload are gone.
    """
    css = read(AUTH_CSS)
    js = read(SETTINGS_JS)

    assert "dp-settings-oidc-clear-secret" not in css
    assert "--dp-oidc-clear-copy-line" not in css
    assert "dp-auth-clear-oidc-secret" not in js
    assert "clear-on-save" not in js

    # One button, one canonical confirmation, one canonical clear.
    assert 'data-action="clear-oidc-secret"' in js
    assert "async function clearOidcSecret(button)" in js
    assert js.count("async function clearOidcSecret(") == 1
    assert "window.DPSettingsModal.confirm({" in js
    assert "writeAuthentication({clear_oidc_client_secret: true})" in js
    assert js.count("clear_oidc_client_secret") == 1


def test_oidc_access_separator_is_a_muted_centred_rule_not_a_subsection_boundary():
    """A change of subject, not a mini-page.

    A full-width hard divider read as a card edge and made Access Control look
    like a separate surface. The rule is now muted, centred and short of the
    body, drawn as the section's own leading element so it cannot be mistaken
    for structure.
    """
    css = read(AUTH_CSS)
    rule = css[css.index("#view-settings .dp-settings-oidc-access::before {"):]
    rule = rule[:rule.index("}")]
    assert "width: 70%;" in rule
    assert "justify-self: center;" in rule
    assert "height: 1px;" in rule
    assert "background: var(--dp-divider);" in rule

    section = css[css.index("#view-settings .dp-settings-oidc-access {"):]
    section = section[:section.index("}")]
    assert "border-top" not in section
    assert "border-top: 2px" not in css


def test_oidc_stylesheet_is_not_a_separate_runtime_owner():
    assert not (STATIC / "ui-settings-authentication-oidc.css").exists()


def test_oidc_test_action_is_relocated_to_the_card_rail_and_exists_exactly_once():
    """One Test, in the one canonical treatment, on the card's own rail.

    It used to live in the Settings footer as a bespoke button. It is now the
    shared provider Test -- same composer, same chip, same place relative to
    status and Enable -- so there is no OIDC-specific variant to drift.
    """
    js = read(SETTINGS_JS)

    assert "headerAction: providerTestAction('verify-oidc')" in js
    assert js.count("providerTestAction('verify-oidc')") == 1
    # Relocated, not duplicated: no footer/context action survives.
    assert 'data-context-action="authentication"' not in js
    assert "Test OIDC Sign-In" not in js
    # Exactly one thing can start a verification.
    assert js.count("data-action=\"verify-oidc\"") == 0  # built by the shared composer
    assert js.count("action === 'verify-oidc'") == 1


def test_oidc_header_status_is_projected_from_canonical_configuration_and_evidence():
    """Three words, derived from canonical truth and nothing else.

    Not the DOM, not a toast, not runtime reachability, and not a second
    `verified` bit this page keeps: a failed Test leaves complete configuration
    unverified, which is Configured.
    """
    js = read(SETTINGS_JS)
    status = js[js.index("function oidcHeaderStatus(a)"):]
    status = status[:status.index("\n  }")]

    assert "if (!a?.oidc_enabled) return {text: '', tone: 'none'};" in status
    assert "if (!a?.oidc_configured) return {text: 'Unconfigured', tone: 'error'};" in status
    assert "a?.oidc_verified ? {text: 'Verified', tone: 'success'} : {text: 'Configured', tone: 'warning'}" in status
    # No other source of truth is consulted.
    for forbidden in ("document.", "querySelector", "oidc_available", "toast", "classList"):
        assert forbidden not in status, forbidden
    # The repaint uses the same projection rather than a second derivation.
    assert "paintCardStatus(view, '.dp-settings-oidc-card', oidcHeaderStatus(a));" in js


def test_first_enable_mints_through_the_one_canonical_token_owner():
    """Enabling API Access with nothing stored completes the obvious intent.

    It does so through the SAME minting act Rotate/Generate performs -- one
    request, one adoption, one disclosure -- so there is no second token
    lifecycle. Disabling never revokes; only an explicit Revoke does.
    """
    js = read(SETTINGS_JS)

    assert js.count("request('POST', '/auth/api-token'") == 1
    assert "async function mintApiToken()" in js
    scope = js[js.index("persistence.defineScope('api-token'"):]
    scope = scope[:scope.index("\n    });")]
    assert "await mintApiToken()" in scope
    assert "!state.auth?.api_token_configured" in scope
    # Participation never removes a stored token.
    assert "DELETE" not in scope
    # The raw value is never configuration.
    assert "data-setting" not in js[js.index("const disclosure = state.oneTimeToken"):
                                    js.index("const apiAccess = card('API Access'")]
