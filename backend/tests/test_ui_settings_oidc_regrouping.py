from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OIDC_JS = ROOT / "frontend" / "static" / "ui-settings-authentication-oidc.js"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def test_oidc_regrouping_accepts_missing_stored_client_secret():
    source = OIDC_JS.read_text(encoding="utf-8")
    clear_block = source.split("function configureClearSecret", 1)[1].split("function configureAccessControl", 1)[0]
    group_block = source.split("function groupOidc", 1)[1].split("function polish", 1)[0]

    # No stored client secret means the base renderer intentionally omits the
    # clear-secret checkbox. That is a valid state and must not abort grouping.
    assert "if (!checkbox || !label) return null;" not in clear_block
    assert "No stored client secret is configured." in clear_block
    assert "disabledCheckbox.disabled = true;" in clear_block

    # The prior crash reset the grouping marker after DOM mutation. The
    # MutationObserver then scheduled the same transformation indefinitely.
    assert "card.dataset[OIDC_MARKER] = '0'" not in group_block
    assert "const clearSecret = configureClearSecret(secret);" in group_block
    assert "body.replaceChildren(originRow, identityRow, credentialsRow, protocolRow, access);" in group_block


def test_oidc_regrouping_text_mutations_are_idempotent():
    source = OIDC_JS.read_text(encoding="utf-8")

    assert "if (labelNode && textOf(labelNode) !== label) labelNode.textContent = label;" in source
    assert "if (textOf(hintNode) !== hint) hintNode.textContent = hint;" in source
    assert "if (textOf(copy) !== headerCopy) copy.textContent = headerCopy;" in source


def test_oidc_regrouping_runtime_is_in_frontend_syntax_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "node --check frontend/static/ui-settings-authentication-oidc.js" in workflow
