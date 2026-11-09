"""1.0.14 localization foundation (WS0-WS2): catalogs, validator, runtime
wiring and the server-rendered sign-in page.

* The catalog validator (``.github/localization/validate.mjs``, run with the
  vendored formatter) passes the repository's catalogs and hard-fails each
  class of broken message; missing translations are debt warnings, and a
  release check makes debt of an advertised locale an error.
* The English catalog's asset is versioned by its content where the page
  loads it, and the server reads the same catalog.
* The integrated surfaces (shell navigation, the session controls, My
  Account and its Help, the dialog defaults) hold no hard-coded English.
* The sign-in page is rendered from the catalog: English unless a caller
  states a locale, escaped, and never from an account or Accept-Language.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
I18N = ROOT / "frontend" / "static" / "i18n"
VALIDATOR = ROOT / ".github" / "localization" / "validate.mjs"
META = ROOT / "frontend" / "i18n" / "en.meta.json"
sys.path.insert(0, str(ROOT / ".github" / "localization"))

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is required to run the catalog validator")


def _validate(i18n: Path = I18N, meta: Path = META, *flags: str) -> tuple[int, dict]:
    done = subprocess.run([NODE, str(VALIDATOR), "--json", f"--i18n={i18n}", f"--meta={meta}", *flags],
                          capture_output=True, text=True, timeout=60)
    return done.returncode, json.loads(done.stdout)


def _scratch(tmp_path: Path, *, english=None, meta=None, catalogs=None, manifest=None) -> tuple[Path, Path]:
    i18n = tmp_path / "i18n"
    i18n.mkdir()
    source = json.loads((I18N / "en.json").read_text(encoding="utf-8"))
    source_meta = json.loads(META.read_text(encoding="utf-8"))
    source_manifest = json.loads((I18N / "manifest.json").read_text(encoding="utf-8"))
    if english:
        source["messages"].update(english)
    if meta:
        source_meta["meta"].update(meta)
    if manifest:
        manifest(source_manifest)
    (i18n / "en.json").write_text(json.dumps(source), encoding="utf-8")
    (i18n / "manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
    for tag, messages in (catalogs or {}).items():
        (i18n / f"{tag}.json").write_text(json.dumps({"schema": "debridpulse-catalog/1", "locale": tag,
                                                      "messages": messages, "sources": {}}), encoding="utf-8")
    meta_path = tmp_path / "en.meta.json"
    meta_path.write_text(json.dumps(source_meta), encoding="utf-8")
    return i18n, meta_path


def _meta(**extra) -> dict:
    return {"category": "label", "context": "A test message for the validator.", "owner": "frontend/static/auth.js",
            "criticality": "normal", **extra}


@needs_node
def test_the_repository_catalogs_validate_and_report_their_translation_debt():
    code, report = _validate()
    assert code == 0, report["errors"]
    english = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    assert report["keys"] == len(english) >= 150
    # Every target is planned with its whole debt reported (a warning), the
    # critical-warning keys among it listed apart.
    for tag in ("es", "de", "pl", "ru", "uk", "hi", "ja", "zh-Hans", "zh-Hant", "ar", "pt-PT", "pt-BR"):
        row = report["locales"][tag]
        assert row["status"] == "planned" and row["missing"] == len(english) and row["critical"] >= 10
    assert "en-XA" not in report["locales"]                   # pseudo locales carry no debt


@needs_node
@pytest.mark.parametrize("case,english,meta,catalogs,expected", [
    ("positional", {"test.case.message": "Hello {0}"}, {"test.case.message": _meta(args={"0": "string"})}, None, "positional"),
    ("unparsable", {"test.case.message": "Hello {name"}, {"test.case.message": _meta(args={"name": "string"})}, None, "does not parse"),
    ("unsafe tag", {"test.case.message": "Hello <script>x</script>"}, {"test.case.message": _meta()}, None, "not allowed"),
    ("undeclared argument", {"test.case.message": "Hello {name}"}, {"test.case.message": _meta()}, None, "differ from the declared"),
    ("no metadata", {"test.case.message": "Hello"}, None, None, "without metadata"),
    ("not a semantic key", {"Hello there": "Hello"}, {"Hello there": _meta()}, None, "semantic dotted key"),
    ("plural without other", {"test.case.count": "{n, plural, one {# file}}"}, {"test.case.count": _meta(args={"n": "plural"})}, None, "MISSING_OTHER_CLAUSE"),
    ("sign-in page plural", {"login.test": "{n, plural, one {# try} other {# tries}}"}, {"login.test": _meta(args={"n": "plural"})}, None,
     "plain {name} arguments only"),
    ("foreign key", None, None, {"de": {"no.such.key": "Hallo"}}, "not an English key"),
    ("argument mismatch", None, None, {"de": {"shell.dock.more_current": "Mehr, aktuelle Seite: {seite}"}}, "is not in the English source"),
    ("polish category", {"test.case.count": "{n, plural, one {# file} other {# files}}"}, {"test.case.count": _meta(args={"n": "plural"})},
     {"pl": {"test.case.count": "{n, plural, one {# plik} two {# pliki} other {# plików}}"}}, "is not one of pl's"),
])
def test_each_class_of_broken_message_is_a_hard_error(tmp_path, case, english, meta, catalogs, expected):
    i18n, meta_path = _scratch(tmp_path, english=english, meta=meta, catalogs=catalogs)
    code, report = _validate(i18n, meta_path)
    assert code == 1, case
    assert any(expected in line for line in report["errors"]), (case, report["errors"])


@needs_node
def test_cldr_categories_each_locale_has_are_accepted_and_a_missing_translation_is_debt_not_an_error(tmp_path):
    english = {"test.case.count": "{n, plural, one {# file} other {# files}}"}
    meta = {"test.case.count": _meta(args={"n": "plural"})}
    catalogs = {
        "pl": {"test.case.count": "{n, plural, one {# plik} few {# pliki} many {# plików} other {# pliku}}"},
        "ru": {"test.case.count": "{n, plural, one {# файл} few {# файла} many {# файлов} other {# файла}}"},
        "ar": {"test.case.count": "{n, plural, zero {لا ملفات} one {ملف واحد} two {ملفان} few {# ملفات} many {# ملفًا} other {# ملف}}"},
    }
    i18n, meta_path = _scratch(tmp_path, english=english, meta=meta, catalogs=catalogs)
    code, report = _validate(i18n, meta_path)
    assert code == 0, report["errors"]
    assert report["locales"]["pl"]["missing"] == report["keys"] - 1       # the rest of English: debt
    assert "test.case.count" not in report["locales"]["pl"]["missingKeys"]
    # The same debt in a locale advertised as supported fails a release check.
    release = tmp_path / "release"
    release.mkdir()
    i18n, meta_path = _scratch(release, english=english, meta=meta, catalogs=catalogs,
                               manifest=lambda m: m["locales"]["pl"].update(status="supported"))
    code, report = _validate(i18n, meta_path, "--release")
    assert code == 1 and any("advertised as supported" in line for line in report["errors"])
    code, _report = _validate(i18n, meta_path)
    assert code == 0                                                       # ordinary development: a warning


@needs_node
def test_stale_and_missing_debt_are_told_apart_critical_staleness_on_its_own_and_release_fails_on_either(tmp_path):
    from core import localization

    english = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    meta = json.loads(META.read_text(encoding="utf-8"))["meta"]
    critical = next(key for key, m in meta.items() if m["criticality"] == "critical-warning" and not m.get("args"))
    current, stale = "shell.nav.dashboard", "shell.nav.settings"
    messages = {current: "[de-test] Dashboard", stale: "[de-test] Settings", critical: "[de-test] critical"}
    sources = {current: localization.source_hash(english[current]),                 # the validator's hash too
               stale: localization.source_hash("An older English text"),
               critical: localization.source_hash("An older critical text")}

    def scratch(where, status):
        where.mkdir()
        i18n, meta_path = _scratch(where, manifest=lambda m: m["locales"]["de"].update(status=status))
        (i18n / "de.json").write_text(json.dumps({"schema": "debridpulse-catalog/1", "locale": "de",
                                                  "messages": messages, "sources": sources}), encoding="utf-8")
        return i18n, meta_path

    code, report = _validate(*scratch(tmp_path / "planned", "planned"))
    assert code == 0, report["errors"]                                     # ordinary development: a warning
    row = report["locales"]["de"]
    assert row["staleKeys"] == [stale, critical] and row["stale"] == 2
    assert current not in row["missingKeys"] + row["staleKeys"] and stale not in row["missingKeys"]
    assert row["missing"] == len(english) - 3
    assert row["criticalStaleKeys"] == [critical] and row["criticalStale"] == 1
    assert critical not in row["criticalMissingKeys"] and row["criticalMissing"] == row["critical"] - 1
    # Debt, its stale keys counted, blocks a release cut of a locale advertised as supported.
    i18n, meta_path = scratch(tmp_path / "supported", "supported")
    code, report = _validate(i18n, meta_path, "--release")
    assert code == 1 and any("advertised as supported" in line and "2 stale" in line for line in report["errors"])
    assert _validate(i18n, meta_path)[0] == 0


def test_the_english_catalog_asset_is_versioned_by_its_content_and_the_server_reads_the_same_catalog():
    from core import localization

    english = (I18N / "en.json").read_bytes()
    version = hashlib.sha256(english).hexdigest()[:16]
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    assert f'<script src="/i18n/en.js?v={version}" defer></script>' in index
    assert localization.catalog_version("en") == version
    assert localization.catalog("en") == json.loads(english)["messages"]
    # The runtime runs before the body; the formatter and English before /app.js.
    assert index.index('/ui-theme-bootstrap.js') < index.index('/ui-i18n.js') < index.index('<aside id="sidebar"')
    assert index.index('/ui-i18n-formatter.js') < index.index(f"/i18n/en.js?v={version}") < index.index('/app.js?v=')
    assert set(localization.manifest()["locales"]) >= {"en", "es", "fr", "de", "it", "pt-BR", "nl", "pt-PT", "pl", "tr",
                                                        "ru", "uk", "id", "hi", "ja", "ko", "zh-Hans", "zh-Hant", "ar"}
    assert all(info["status"] in ("canonical", "planned", "pseudo") for info in localization.manifest()["locales"].values())


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
        if depth == 0:
            return source[start:i]


@pytest.mark.parametrize("owner,functions", [
    ("auth.js", ["roleLabel", "standaloneSidebarRow", "syncSidebarSessionUi", "syncHeaderSessionUi", "showHeaderAccount",
                 "errorMessage", "emailMarkup", "openAccountDialog", "savePresentation", "presentationSelector", "bindLogout"]),
    ("app.js", ["showPageHeading", "renderShellDock", "syncShellDock", "setSidebarOverlay"]),
    ("ui-settings-modal.js", ["confirm", "prompt"]),
])
def test_the_integrated_surfaces_hold_no_hard_coded_english(owner, functions):
    """The census's own judgement of what is a human message, applied to the
    functions this foundation integrated: none is left as a literal."""
    import inventory

    source = (ROOT / "frontend" / "static" / owner).read_text(encoding="utf-8")
    # A thrown error that is caught and never shown is a diagnostic: bindLogout
    # replaces it with session.log_out.failed.
    diagnostics = {("auth.js", "bindLogout"): ["Logout failed"]}
    for name in functions:
        body = _function(source, name)
        found = [entry["english"] for entry in inventory.frontend_entries_from(body, owner)]
        assert found == diagnostics.get((owner, name), []), (owner, name, found)


def test_the_shell_markup_names_its_keys():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    nav = index[index.index("<nav>"):index.index("</nav>")]
    labels = re.findall(r'<span class="nav-label"([^>]*)>', nav)
    assert len(labels) == 7 and all('data-i18n="shell.nav.' in attrs for attrs in labels)
    groups = re.findall(r'<div class="nav-group"([^>]*)>', nav)
    assert len(groups) == 3 and all("data-i18n=" in attrs for attrs in groups)
    english = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    for key in re.findall(r'data-i18n="([\w.]+)"', index) + [k for pair in re.findall(r'data-i18n-attr="([^"]+)"', index)
                                                             for k in re.findall(r":([\w.]+)", pair)]:
        assert key in english, key


def test_the_sign_in_page_is_rendered_from_the_catalog_escaped_and_never_from_an_account():
    from api import auth_routes
    from core import localization

    english = localization.catalog("en")
    assert auth_routes._text("login.sign_in_with", "en", provider="&lt;Corp&gt;") == "Sign in with &lt;Corp&gt;"
    assert auth_routes._text("login.foot.https", "en", https='<span class="https">HTTPS</span>') == \
        'OpenID Connect requires a canonical <span class="https">HTTPS</span> external URL.'
    assert auth_routes._text("login.error.invalid_credentials", "en") == english["login.error.invalid_credentials"]
    pseudo = auth_routes._text("login.sign_in_with", "en-XA", provider="Corp")
    assert pseudo.startswith("⟦") and "Corp" in pseudo and "Šîgñ" in pseudo           # the argument is untouched
    assert localization.preauth_locale(None) == "en" and localization.preauth_locale("xx") == "en"
    source = (ROOT / "backend" / "api" / "auth_routes.py").read_text(encoding="utf-8")
    login = source[source.index("async def _login_page("):source.index("async def _state_free_auth_page(")]
    assert "accept-language" not in login.lower() and "load_locale" not in login
    assert "Release to hide password" not in source and "dataset.hideLabel" in source


def test_server_pseudo_locales_keep_arguments_and_mark_direction():
    from core import localization

    expanded = localization.pseudo("Sign in with {provider}", "en-XA")
    assert "{provider}" in expanded and expanded.startswith("⟦") and len(expanded) > len("Sign in with {provider}") * 1.3
    rtl = localization.pseudo("Sign in with {provider}", "ar-XB")
    assert rtl.startswith("‮") and rtl.endswith("‬") and "{provider}" in rtl
