import json
import re
from importlib.metadata import distribution
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _locked_runtime_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for raw_line in (REPO_ROOT / "backend/requirements.txt").read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "--")):
            continue
        name, version = line.split("==", 1)
        packages[_normalized_name(name)] = version.split(";", 1)[0].strip()
    return packages


def _runtime_license_manifest() -> dict:
    return json.loads(
        (REPO_ROOT / "licenses/python-runtime.json").read_text()
    )


def test_runtime_license_inventory_matches_lock_exactly():
    manifest = _runtime_license_manifest()
    inventoried = {
        _normalized_name(item["name"]): item["version"]
        for item in manifest["packages"]
    }
    assert inventoried == _locked_runtime_packages()


def test_human_runtime_license_inventory_covers_machine_manifest_exact_versions():
    manifest = _runtime_license_manifest()
    human_inventory = (REPO_ROOT / "docs/DEPENDENCY_LICENSES.md").read_text()
    for item in manifest["packages"]:
        expected_row_prefix = f'| {item["name"]} | {item["version"]} |'
        assert expected_row_prefix in human_inventory, (
            f"Human dependency inventory missing {item['name']} {item['version']}"
        )


def test_runtime_inventory_has_no_unknown_or_unreviewed_copyleft_license():
    manifest = _runtime_license_manifest()
    for item in manifest["packages"]:
        license_id = item["license"].upper()
        assert "UNKNOWN" not in license_id
        assert "AGPL" not in license_id
        assert "GPL" not in license_id


def test_v1_license_and_upstream_notice_are_present():
    license_text = (REPO_ROOT / "LICENSE").read_text()
    notice = (REPO_ROOT / "NOTICE").read_text()
    upstream_mit = (REPO_ROOT / "LICENSES/MIT.txt").read_text()

    assert "GNU GENERAL PUBLIC LICENSE" in license_text
    assert "Version 2, June 1991" in license_text
    assert "Copyright (C) 2026 Chris Moore" in notice
    assert "c0f7a5bfeba4f259fb2acc62ac6eed27e8ac4d5c" in notice
    assert "Copyright (c) 2026 kroeberd" in upstream_mit


def test_bencode2_notice_and_machine_readable_inventory_ship_in_image():
    bencode_notice = (REPO_ROOT / "licenses/bencode2-MIT.txt").read_text()
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert "Copyright (c) 2024 trim21" in bencode_notice
    assert "Permission is hereby granted, free of charge" in bencode_notice
    assert "COPY licenses/ /app/licenses/" in dockerfile


def test_installed_runtime_packages_retain_license_or_notice_files():
    manifest = _runtime_license_manifest()
    explicit_notices = {
        "bencode2": REPO_ROOT / "licenses/bencode2-MIT.txt",
    }

    for item in manifest["packages"]:
        name = _normalized_name(item["name"])
        if name in explicit_notices:
            assert explicit_notices[name].is_file()
            continue
        files = distribution(item["name"]).files or ()
        license_files = [
            path
            for path in files
            if any(
                part.casefold().startswith(("license", "copying", "notice"))
                for part in Path(str(path)).parts
            )
        ]
        assert license_files, f"{item['name']} {item['version']} has no packaged license notice"


def test_current_project_surfaces_state_the_debridpulse_gpl_identity():
    readme = (REPO_ROOT / "README.md").read_text()
    landing_page = (REPO_ROOT / "index.html").read_text()
    notice = (REPO_ROOT / "NOTICE").read_text()
    source_offer = (REPO_ROOT / "SOURCE_OFFER.md").read_text()

    assert "DebridPulse" in readme
    assert "GPL-2.0-or-later" in readme
    assert "DebridPulse · GPL-2.0-or-later" in landing_page
    assert "MIT License" not in landing_page
    assert notice.startswith("DebridPulse — AllDebrid + aria2 Download Manager")
    assert "issues/new?template=source_request.yml" in source_offer




def test_authentication_docs_cover_new_runtime_security_dependencies():
    readme = (REPO_ROOT / "README.md").read_text()
    auth_docs = (REPO_ROOT / "docs/authentication.md").read_text()
    dependency_licenses = (REPO_ROOT / "docs/DEPENDENCY_LICENSES.md").read_text()

    assert "Settings → Authentication" in readme
    assert "Argon2id" in auth_docs
    assert "Authorization Code" in auth_docs
    assert "PKCE" in auth_docs
    assert "API bearer token" in auth_docs
    assert "authlib" in dependency_licenses
    assert "cryptography" in dependency_licenses
    assert "httpx" in dependency_licenses
    assert "joserfc" in dependency_licenses


def test_container_runtime_declares_trixie_and_rar_codec_notices():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    dependency_licenses = (REPO_ROOT / "docs/DEPENDENCY_LICENSES.md").read_text()

    assert dockerfile.startswith("FROM python:3.12.14-slim-trixie\n")
    assert "Components: main non-free" in dockerfile
    assert "    7zip" in dockerfile
    assert "    7zip-rar" in dockerfile
    assert "path-include=/usr/share/doc/7zip-rar/copyright" in dockerfile
    assert "path-include=/usr/share/doc/7zip-rar/unRarLicense.txt" in dockerfile
    assert "7zip-rar" in dependency_licenses
    assert "UnRAR restricted freeware" in dependency_licenses
    assert "/usr/share/doc/7zip-rar/unRarLicense.txt" in dependency_licenses
