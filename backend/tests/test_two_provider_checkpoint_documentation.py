from pathlib import Path
import json


ROOT = Path(__file__).parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text()


def test_readme_describes_final_two_provider_state_and_current_release_examples():
    readme = _text("README.md")
    version = _text("VERSION").strip()
    assert "AllDebrid + General HTTP(S) providers" in readme
    assert "SPECIALIZED" in readme and "GENERIC" in readme
    # Source state: 1.0.12 is finalized, not an unfinished development checkpoint.
    assert "not yet a released v1.0.12 baseline" not in readme
    assert "development architecture" not in readme
    assert "development support matrix" not in readme
    # Install examples name the current release, derived from VERSION.
    assert f"ghcr.io/xipher-zero/debridpulse:v{version}" in readme
    assert "ghcr.io/xipher-zero/debridpulse:v1.0.11.1" not in readme
    # Deferred expansion belongs to 1.0.13, not to an unmet 1.0.12 requirement.
    assert "1.0.13" in readme
    assert "Deferred Items 12–16" not in readme
    assert "Settings → General" not in readme


def test_public_project_surfaces_do_not_advertise_upstream_fork_origin():
    """The public surfaces describe DebridPulse; provenance lives in legal files."""
    readme = _text("README.md")
    landing_page = _text("index.html")

    for surface_name, surface in (("README.md", readme), ("index.html", landing_page)):
        for marker in ("kroeberd", "alldebrid-client", "v1.9.9", "Derived from"):
            assert marker not in surface, (
                f"{surface_name} still advertises the upstream project: {marker}"
            )
    assert "Upstream provenance" not in readme

    # Both public surfaces still state DebridPulse's own license identity.
    assert "GPL-2.0-or-later" in readme
    assert "DebridPulse · GPL-2.0-or-later" in landing_page

    # Legal provenance remains preserved in its canonical legal surfaces.
    notice = _text("NOTICE")
    assert "kroeberd/alldebrid-client release" in notice
    assert "GNU General Public License" in notice
    assert "MIT" in _text("LICENSES/MIT.txt")


def test_runtime_state_and_input_required_docs_name_current_consumers_truthfully():
    runtime_state = _text("docs/architecture/PROVIDER_RUNTIME_STATE.md")
    input_required = _text("docs/architecture/INPUT_REQUIRED_LIFECYCLE.md")
    assert "## Current production consumer" in runtime_state
    assert "AllDebrid's dynamic supported-host state" in runtime_state
    assert "planned first production consumer" not in runtime_state
    assert "General HTTP & HTTPS path" in input_required
    assert "does **not** imply that SSH, SFTP, or SCP is a current production transport" in input_required


def test_current_regression_map_and_support_doc_preserve_deferred_stage_status():
    regression = _text("docs/architecture/REGRESSION_MAP_V112.md")
    support = _text("docs/architecture/MULTI_PROVIDER_HTTP_SLICE.md")
    assert "two-provider canonical" in regression.casefold()
    assert "Items 12–16 remain intentionally deferred" in regression
    assert "There remains no production consumer" not in regression
    assert "Still deferred after Item 4" not in regression
    assert "Current support matrix" in support
    assert "eventual full Stage 17/18" in support


def test_authoritative_development_version_surfaces_agree_on_1_0_12():
    assert _text("VERSION").strip() == "1.0.12"
    package = json.loads(_text("frontend/browser/package.json"))
    assert package["version"] == "1.0.12"
    ui_arch = _text("docs/UI_FRONTEND_ARCHITECTURE.md")
    assert "reports `1.0.12` for the current development tree" in ui_arch
    assert "remains `1.0.11.1` for this corrective release" not in ui_arch


def test_oci_metadata_describes_current_two_provider_architecture_and_retains_license_identity():
    dockerfile = _text("Dockerfile")
    workflow = _text(".github/workflows/fork-image.yml")
    description = "Universal transfer orchestration with AllDebrid and General HTTP(S) providers plus aria2 execution"
    for value in (dockerfile, workflow):
        assert description in value
        assert "DebridPulse: Universal Transfer Manager" in value
        assert "GPL-2.0-or-later" in value
    assert "Provider-independent transfer orchestration with AllDebrid resolution and aria2 execution" not in dockerfile


def test_dependency_license_inventory_is_final_1_0_12_closure_and_requires_reaudit_in_1_0_13():
    licenses = _text("docs/DEPENDENCY_LICENSES.md")
    assert "inventory for **final v1.0.12**" in licenses
    assert "v1.0.12 dependency/license closure" in licenses
    assert "not the final eventual v1.0.12 dependency/license closure" not in licenses
    assert "`1.0.13` expansion work" in licenses
    assert "must trigger a fresh third-party/license review" in licenses


def test_notice_preserves_legal_attribution_without_obsolete_single_provider_product_framing():
    notice = _text("NOTICE")
    assert notice.startswith("DebridPulse — Universal Transfer Manager\n")
    assert "GNU General Public License" in notice
    assert "version 2 or (at your option) any later version" in notice
    assert "kroeberd/alldebrid-client release" in notice
    assert "AllDebrid + aria2 Download Manager" not in notice


def test_help_page_explains_both_current_provider_paths_without_claiming_every_http_url_uses_alldebrid():
    help_page = _text("frontend/static/ui-help-page.js")
    assert "General Sources → HTTP &amp; HTTPS" in help_page
    assert "specialized provider claim wins over a generic one" in help_page
    assert "For a normal HTTP or HTTPS source, DebridPulse sends the source to AllDebrid" not in help_page
