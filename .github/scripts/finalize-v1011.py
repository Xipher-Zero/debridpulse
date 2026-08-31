from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RC = "1.0.11rc1"
FINAL = "1.0.11"
RC_IMAGE = f"ghcr.io/xipher-zero/debridpulse:v{RC}"
FINAL_IMAGE = f"ghcr.io/xipher-zero/debridpulse:v{FINAL}"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_exact(path: str, old: str, new: str, count: int) -> None:
    text = read(path)
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(f"{path}: expected {count} occurrences of {old!r}, found {actual}")
    write(path, text.replace(old, new))


# Authoritative release version and user/deployment presentation surfaces.
if read("VERSION") != RC + "\n":
    raise RuntimeError("VERSION is not the expected qualified RC boundary")
write("VERSION", FINAL + "\n")
replace_exact("docker-compose.yml", RC_IMAGE, FINAL_IMAGE, 1)
replace_exact("README.md", RC_IMAGE, FINAL_IMAGE, 2)
replace_exact("index.html", RC_IMAGE, FINAL_IMAGE, 2)
replace_exact(
    "README.md",
    "- download filtering and limits.",
    "- download concurrency, limits, and recovery behavior.",
    1,
)

# Final release contract replaces the RC-specific owner.
src = ROOT / "backend/tests/test_rc1_version_surface.py"
dst = ROOT / "backend/tests/test_release_version_surface.py"
if not src.exists() or dst.exists():
    raise RuntimeError("release version contract rename precondition failed")
text = src.read_text(encoding="utf-8")
text = text.replace('"""Release-candidate version surface contract."""', '"""Final release version surface contract."""')
text = text.replace("def test_rc1_version_is_authoritative_and_user_visible_surfaces_follow_it()", "def test_release_version_is_authoritative_and_user_visible_surfaces_follow_it()")
text = text.replace('expected = "1.0.11rc1"', 'expected = "1.0.11"')
text = text.replace("# Release-facing deployment examples must advance with the candidate too.", "# Release-facing deployment examples must match the final release.")
text = text.replace("def test_rc1_container_metadata_uses_authoritative_version_file()", "def test_release_container_metadata_uses_authoritative_version_file()")
if "rc1" in text.lower():
    raise RuntimeError("RC-specific naming remains in final release contract")
# Prove the new release branch itself is qualified/published by all release workflows.
insert = '''\n    release_branch = "1.0.11"\n    for workflow_name in ("tests.yml", "container-security.yml", "fork-image.yml", "codeql.yml"):\n        workflow_text = read(ROOT / ".github" / "workflows" / workflow_name)\n        assert release_branch in workflow_text\n'''
needle = "    assert 'org.opencontainers.image.version=\"${APP_VERSION}\"' in dockerfile\n"
if text.count(needle) != 1:
    raise RuntimeError("release contract insertion point changed")
text = text.replace(needle, needle + insert)
dst.write_text(text, encoding="utf-8")
src.unlink()

# The deep UI audit should identify the final track, not the prerelease track.
replace_exact(
    "backend/tests/test_ui_frontend_deep_audit_contract.py",
    "def test_ui_track_is_the_1_0_11_rc1_candidate() -> None:\n    assert read(VERSION).strip() == \"1.0.11rc1\"",
    "def test_ui_track_is_the_1_0_11_release() -> None:\n    assert read(VERSION).strip() == \"1.0.11\"",
    1,
)

# Release documentation: retain historical meaning without stale RC1 branding.
replace_exact("docs/UI_FRONTEND_ARCHITECTURE.md", "## RC1 browser validation", "## 1.0.11 release browser validation", 1)
replace_exact("docs/UI_FRONTEND_ARCHITECTURE.md", "## RC1 version ownership", "## 1.0.11 version ownership", 1)
replace_exact(
    "docs/UI_FRONTEND_ARCHITECTURE.md",
    "used for RC1.",
    "used for the 1.0.11 release candidate.",
    1,
)
replace_exact(
    "docs/UI_DESIGN_TOKENS.md",
    "The consolidated `1.0.11rc1` candidate was browser-compared",
    "The consolidated pre-release candidate for `1.0.11` was browser-compared",
    1,
)
replace_exact(
    "docs/UI_COMPONENT_LANGUAGE.md",
    "The browser-validated RC1 output is the acceptance boundary.",
    "The browser-validated 1.0.11 pre-release output is the acceptance boundary.",
    1,
)

# The official release branch must run every release qualification/publish gate.
def add_branch(path: str, old: str, new: str) -> None:
    replace_exact(path, old, new, 1)

add_branch(
    ".github/workflows/tests.yml",
    "  push:\n    branches: [ \"main\", 'staging/**' ]",
    "  push:\n    branches: [ \"main\", \"1.0.11\", 'staging/**' ]",
)
add_branch(
    ".github/workflows/tests.yml",
    "  pull_request:\n    branches: [ \"main\", 'staging/**' ]",
    "  pull_request:\n    branches: [ \"main\", \"1.0.11\", 'staging/**' ]",
)
add_branch(
    ".github/workflows/container-security.yml",
    "  push:\n    branches: [ \"main\", 'staging/**' ]",
    "  push:\n    branches: [ \"main\", \"1.0.11\", 'staging/**' ]",
)
add_branch(
    ".github/workflows/container-security.yml",
    "  pull_request:\n    branches: [ \"main\", 'staging/**' ]",
    "  pull_request:\n    branches: [ \"main\", \"1.0.11\", 'staging/**' ]",
)
add_branch(
    ".github/workflows/fork-image.yml",
    "  push:\n    branches: [ \"main\", 'staging/**' ]",
    "  push:\n    branches: [ \"main\", \"1.0.11\", 'staging/**' ]",
)
add_branch(
    ".github/workflows/codeql.yml",
    '  push:\n    branches: [ "main" ]',
    '  push:\n    branches: [ "main", "1.0.11" ]',
)
add_branch(
    ".github/workflows/codeql.yml",
    "      - main\n      - 'staging/**'",
    "      - main\n      - '1.0.11'\n      - 'staging/**'",
)

# Final release changelog entry.
changelog = read("CHANGELOG.md")
header = "# Changelog\n"
if not changelog.startswith(header) or "## [1.0.11]" in changelog:
    raise RuntimeError("CHANGELOG release insertion precondition failed")
entry = '''\n## [1.0.11] — 2026-08-31\n\n### UI overhaul and release consolidation\n\n- Completed the application-wide UI overhaul across Dashboard, Downloads, Statistics, Activity Log, Help, Login, and Settings while preserving the accepted dark/light presentation and responsive behavior through consolidation.\n- Consolidated frontend ownership, removed unreachable duplicate/dead presentation code, and retained only browser-validated live calibration layers where folding them would create unnecessary release risk.\n- Completed an exhaustive Settings UI-to-runtime census, corrected UI/backend bounds and zero-value retry semantics, and physically retired the hidden automatic File Filters policy while preserving explicit per-file blocking and download labels.\n\n### Adversarial release hardening\n\n- Corrected aria2 error-recovery accounting and delay enforcement so retry claims are persisted before restart attempts, failed restart attempts consume budget, and configured retry ceilings cannot be bypassed.\n- Added prerelease-aware version ordering and centralized release-version ownership so backend APIs, login/sidebar UI, deployment examples, and OCI metadata derive from the authoritative `VERSION` file.\n- Removed broad 7-Zip parser selection from automatic `.tar.zst`, `.tzst`, and `.tar.lzma` composite extraction; exact outer decoders now feed the validated TAR safety path with decompression budgets.\n- Preserved non-root runtime validation, external-aria2 ownership boundaries, transfer-integrity protections, extraction limits, and fail-closed maintenance/backup behavior through the final security and adversarial qualification passes.\n\n'''
write("CHANGELOG.md", header + entry + changelog[len(header):])

# Final invariant: no stale RC1 naming survives outside intentional version-order tests.
allowed = {ROOT / "backend/tests/test_version_utils.py"}
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts or path in allowed:
        continue
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    if "rc1" in body.lower():
        raise RuntimeError(f"stale RC1 identifier remains in {path.relative_to(ROOT)}")

print("Final v1.0.11 release-surface transformation complete")
