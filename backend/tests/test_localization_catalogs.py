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
    # Every target is planned. One without a catalog reports its whole debt (a
    # warning), the critical-warning keys among it listed apart.
    for tag in ("ja", "ko", "zh-Hans", "zh-Hant", "ar"):
        row = report["locales"][tag]
        assert row["status"] == "planned" and row["missing"] == len(english) and row["critical"] >= 10
    # WS5-A1 (de), WS5-A2 (fr, es, it), WS5-A3 (pt-BR, pt-PT, nl), WS5-B1 (pl, ru, uk) and
    # WS5-B2 (tr, id, hi): complete, reviewed catalogs, still planned (never admitted).
    for tag in ("de", "fr", "es", "it", "pt-BR", "pt-PT", "nl", "pl", "ru", "uk", "tr", "id", "hi"):
        row = report["locales"][tag]
        assert row["status"] == "planned" and row["catalog"] and row["termbase"] and row["ledger"], tag
        assert (row["missing"], row["stale"], row["unreviewed"], row["noBackTranslation"], len(row["openFindings"])) == (0, 0, 0, 0, 0), tag
        code, complete = _validate(I18N, META, f"--complete={tag}")
        assert code == 0, (tag, complete["errors"])
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
    ("quoted markup", {"test.case.message": "Click '<img src=x onerror=alert(1)>' here"}, {"test.case.message": _meta()}, None,
     "reads as markup"),
    ("translated markup", None, None, {"de": {"shell.nav.dashboard": "'<script>'x"}}, "reads as markup"),
    # An argument named like a tag its message uses would replace that tag's element when said.
    ("argument named like its tag", {"test.case.message": "Type <code>{code}</code>"},
     {"test.case.message": _meta(args={"code": "string: a literal"})}, None, "named like the message's rich-text tag"),
    ("plural of a plain string", {"test.case.message": "Hello {name}"}, {"test.case.message": _meta(args={"name": "string: a name"})},
     {"de": {"test.case.message": "{name, plural, one {Hallo} other {Hallo}}"}}, "is a plural"),
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


@needs_node
def test_a_count_english_says_plainly_may_be_inflected_by_a_translation(tmp_path):
    english = {"test.case.count": "{count} items"}
    meta = {"test.case.count": _meta(args={"count": "number: how many"})}
    catalogs = {"pl": {"test.case.count": "{count, plural, one {{count} element} few {{count} elementy} many {{count} elementów} other {{count} elementu}}"}}
    i18n, meta_path = _scratch(tmp_path, english=english, meta=meta, catalogs=catalogs)
    code, report = _validate(i18n, meta_path)
    assert code == 0, report["errors"]


# WS5-0: the translation checks a locale candidate passes before review. The
# fixture is synthetic ("ZZ" words): test doubles of a German catalog, termbase
# and review ledger, never translations. Each mutation breaks one rule.
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


WS5_DE = {
    "settings.test": "ZZ-Test",
    "help.notifications.statistics_reporting.test": "<b>ZZ-Test</b> zz zz.",
    "downloads.summary.downloading": "{count, plural, =0 {Zz} one {{count} Zz} other {{count} Zzz}}",
    "presentation.status.saved": "{mode, select, easy {ZZ-Leicht zz.} other {ZZ-Voll zz.}}",
    "error.validation.setting_url": "Zz http:// zz https:// zz.",
    "settings.oidc.public_base_hint_managed": "Zz PUBLIC_BASE_URL. Zz zz OIDC zz.",
    "shell.version_label": "DebridPulse-Zz",
    "role.admin": "ZZ-Admin",
    "settings.deployment.standalone_hint_shortest": "Nur ZZ-Admin.",
    "account.action.clear_password": "Zz zz zz",
}
WS5_TERMS = {"schema": "debridpulse-terms/1", "locale": "de", "register": "Sie (formal), test double",
             "terms": {"easy": {"target": "ZZ-Leicht"}, "advanced": {"target": "ZZ-Voll"},
                       "role_admin": {"target": "ZZ-Admin", "forbidden": ["ZZ-Boss"]}}}


def _ws5(tmp_path, tag="de", messages=None, *, sources=None, terms=WS5_TERMS, review=None, entries=None):
    """A scratch catalog set: the repository's English, one synthetic target with
    current source hashes, its termbase and a review ledger of every key."""
    i18n, meta_path = _scratch(tmp_path)
    english = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    messages = dict(WS5_DE if messages is None else messages)
    hashes = {key: _sha(english[key]) for key in messages}
    hashes.update(sources or {})
    (i18n / f"{tag}.json").write_text(json.dumps({"schema": "debridpulse-catalog/1", "locale": tag, "messages": messages,
                                                  "sources": hashes}), encoding="utf-8")
    meta = json.loads(META.read_text(encoding="utf-8"))["meta"]
    if terms is not None:
        (tmp_path / "terms").mkdir()
        (tmp_path / "terms" / f"{tag}.json").write_text(json.dumps({**terms, "locale": tag}), encoding="utf-8")
    ledger = {key: {"source": _sha(english[key]), "translation": _sha(text),
                    "provenance": ["ai-generated", "ai-review"]
                    + (["ai-back-translation"] if meta[key]["criticality"] == "critical-warning" else []),
                    **({"backTranslation": "Zz back"} if meta[key]["criticality"] == "critical-warning" else {})}
              for key, text in messages.items()}
    for key, change in (entries or {}).items():
        ledger[key] = {**ledger.get(key, {}), **change}
    (tmp_path / "review").mkdir()
    (tmp_path / "review" / f"{tag}.json").write_text(json.dumps(review or {"schema": "debridpulse-review/1", "locale": tag,
                                                                           "entries": ledger}), encoding="utf-8")
    return i18n, meta_path


@needs_node
def test_ws5_a_well_formed_planned_catalog_passes_and_its_gaps_are_debt_not_errors(tmp_path):
    code, report = _validate(*_ws5(tmp_path))
    assert code == 0, report["errors"]
    row = report["locales"]["de"]
    assert row["status"] == "planned" and row["missing"] == report["keys"] - len(WS5_DE) and row["stale"] == 0
    assert row["termbase"] and row["ledger"] and row["unreviewed"] == 0 and row["noBackTranslation"] == 0
    assert row["provenance"] == {"ai-generated": len(WS5_DE), "ai-review": len(WS5_DE), "ai-back-translation": 1}
    # The English registers: the Help label map holds every <b> span, classified.
    assert report["labels"] == {"spans": 770, "control": 503, "term": 40, "emphasis": 101, "control-unmapped": 125, "unresolved": 1}


def _mutated(**change):
    return {**WS5_DE, **change}


WS5_MUTATIONS = [
    ("placeholder renamed", _mutated(**{"downloads.summary.downloading": "{n, plural, =0 {Zz} one {{n} Zz} other {{n} Zzz}}"}), None,
     "is not in the English source"),
    ("rich-text tag dropped", _mutated(**{"help.notifications.statistics_reporting.test": "ZZ-Test zz zz."}), None,
     "rich-text tags differ from the English"),
    ("rich-text tag added", _mutated(**{"help.notifications.statistics_reporting.test": "<b>ZZ-Test</b> <b>zz</b> zz."}), None,
     "rich-text tags differ from the English"),
    ("select case dropped", _mutated(**{"presentation.status.saved": "{mode, select, other {ZZ-Voll zz.}}"}), None,
     "select {mode} has the cases (other), the English (easy,other)"),
    ("select case invented", _mutated(**{"presentation.status.saved": "{mode, select, easy {ZZ-Leicht zz.} advanced {ZZ-Voll zz.} other {ZZ-Voll zz.}}"}), None,
     "select {mode} has the cases"),
    ("exact case dropped", _mutated(**{"downloads.summary.downloading": "{count, plural, one {{count} Zz} other {{count} Zzz}}"}), None,
     "lacks the English exact case =0"),
    ("plural said plainly", _mutated(**{"downloads.summary.downloading": "{count} Zzz"}), None, "is said plainly; de distinguishes"),
    ("untranslated", _mutated(**{"shell.nav.dashboard": "Dashboard"}), None, "is the English text"),
    ("protected literal changed", _mutated(**{"error.validation.setting_url": "Zz http:// zz https:/ zz."}), None,
     "the protected literal 'https://' appears 0 times, the English 1"),
    ("identifier translated", _mutated(**{"settings.oidc.public_base_hint_managed": "Zz ÖFFENTLICHE_URL. Zz zz OIDC zz."}), None,
     "the protected literal 'PUBLIC_BASE_URL' appears 0 times"),
    ("name transliterated", _mutated(**{"shell.version_label": "ДебридПульс-Zz"}), None, "the protected name 'DebridPulse' is missing"),
    ("help label differs from its control", _mutated(**{"help.notifications.statistics_reporting.test": "<b>ZZ-Prüfen</b> zz zz."}), None,
     "no <b> span says its translation 'ZZ-Test'"),
    ("concept key not its term", _mutated(**{"role.admin": "ZZ-Chef", "settings.deployment.standalone_hint_shortest": "Nur ZZ-Chef."}), None,
     "which the termbase says as 'ZZ-Admin'"),
    ("concept named without its term", _mutated(**{"settings.deployment.standalone_hint_shortest": "Nur zz."}), None,
     "names the concept 'role_admin' without the termbase's 'ZZ-Admin'"),
    ("forbidden alternative", _mutated(**{"settings.deployment.standalone_hint_shortest": "Nur ZZ-Admin (ZZ-Boss)."}), None,
     "a forbidden alternative for the concept 'role_admin'"),
]


@needs_node
@pytest.mark.parametrize("case,messages,sources,expected", WS5_MUTATIONS, ids=[case for case, *_ in WS5_MUTATIONS])
def test_ws5_each_broken_translation_is_a_hard_error(tmp_path, case, messages, sources, expected):
    code, report = _validate(*_ws5(tmp_path, messages=messages, sources=sources))
    assert code == 1, case
    assert any(expected in line for line in report["errors"]), (case, report["errors"])


@needs_node
@pytest.mark.parametrize("tag,lacking", [("pl", "few, many"), ("ru", "few, many"), ("uk", "few, many"),
                                         ("ar", "zero, two, few, many"), ("es", "many")])
def test_ws5_a_plural_lacks_no_category_its_language_reaches(tmp_path, tag, lacking):
    messages = {"downloads.summary.downloading": "{count, plural, =0 {Zz} one {{count} Zz} other {{count} Zzz}}"}
    code, report = _validate(*_ws5(tmp_path, tag, messages, terms=None))
    assert code == 1
    assert any(f"plural {{count}} lacks {lacking}" in line for line in report["errors"]), report["errors"]


@needs_node
@pytest.mark.parametrize("case,sources,entries,expected", [
    ("malformed source hash", {"settings.test": "xyz"}, None, "is not sha256[:16] hex"),
    ("hash without its translation", {"shell.nav.settings": "0123456789abcdef"}, None, "a source hash without its translation"),
    ("human review without evidence", None, {"settings.test": {"provenance": ["ai-generated", "human-review"]}},
     "names its reviewer and evidence"),
    ("unknown provenance", None, {"settings.test": {"provenance": ["ai-generated", "native-certified"]}}, "unknown provenance 'native-certified'"),
    ("two origins", None, {"settings.test": {"provenance": ["ai-generated", "human-translated", "ai-review"],
                                             "human": {"reviewer": "r", "evidence": "e"}}}, "exactly one origin"),
    ("finding without status", None, {"settings.test": {"findings": [{"kind": "meaning", "note": "zz"}]}}, "needs kind, note and status"),
])
def test_ws5_a_broken_source_hash_or_a_false_review_claim_is_a_hard_error(tmp_path, case, sources, entries, expected):
    code, report = _validate(*_ws5(tmp_path, sources=sources, entries=entries))
    assert code == 1, case
    assert any(expected in line for line in report["errors"]), (case, report["errors"])


@needs_node
def test_ws5_stale_translations_unreviewed_text_and_open_findings_are_debt_and_exemptions_are_recorded(tmp_path):
    english = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    messages = _mutated(**{"shell.nav.dashboard": "Dashboard", "shell.nav.settings": "Zz the zz",
                           "help.notifications.statistics_reporting.test": "<b>ZZ-Prüfen</b> zz zz."})
    entries = {
        "shell.nav.dashboard": {"identical": "The German interface word is Dashboard."},
        "help.notifications.statistics_reporting.test": {"labels": {"0": "Test double: the control is named otherwise here."}},
        "settings.test": {"translation": _sha("an earlier translation")},        # the review was of other text
        "role.admin": {"findings": [{"kind": "ambiguity", "note": "zz", "status": "open"}]},
        "account.action.clear_password": {"provenance": ["ai-generated", "ai-review"]},    # critical, not back-translated
    }
    code, report = _validate(*_ws5(tmp_path, messages=messages, sources={"shell.version_label": _sha("An older English text")},
                                   entries=entries))
    assert code == 0, report["errors"]
    row = report["locales"]["de"]
    assert row["staleKeys"] == ["shell.version_label"]                       # stale: debt, not delivered, not an error
    assert english["shell.version_label"] and "shell.version_label" not in row["missingKeys"]
    assert "settings.test" in row["unreviewedKeys"] and "shell.version_label" in row["unreviewedKeys"]
    assert row["noBackTranslationKeys"] == ["account.action.clear_password"]
    assert row["openFindings"] == [{"key": "role.admin", "kind": "ambiguity", "note": "zz"}]
    assert row["englishFragmentKeys"] == ["shell.nav.settings"]              # a warning for review
    # The same set as a candidate: none of it passes as complete.
    again = tmp_path / "again"
    again.mkdir()
    i18n, meta_path = _ws5(again, messages=messages, sources={"shell.version_label": _sha("An older English text")}, entries=entries)
    code, report = _validate(i18n, meta_path, "--complete=de")
    assert code == 1
    for expected in ("is not complete: ", "translations without a current independent review",
                     "critical keys without a current back-translation", "1 open review findings", "its termbase has no term for"):
        assert any(expected in line for line in report["errors"]), (expected, report["errors"])


def _minimal(tmp_path, *, entries=None, terms=None):
    """A whole (small) English source with a complete synthetic target, for the --complete gate."""
    i18n = tmp_path / "i18n"
    i18n.mkdir(parents=True)
    english = {"shell.nav.settings": "Settings", "help.minimal.open": "Open <b>Settings</b> now.",
               "account.clear.warning": "Anyone who can reach DebridPulse can sign in."}
    meta = {key: _meta(owner="frontend/static/auth.js", criticality="critical-warning" if key == "account.clear.warning" else "normal")
            for key in english}
    manifest = json.loads((I18N / "manifest.json").read_text(encoding="utf-8"))
    (i18n / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (i18n / "en.json").write_text(json.dumps({"schema": "debridpulse-catalog/1", "locale": "en", "messages": english}), encoding="utf-8")
    (tmp_path / "en.meta.json").write_text(json.dumps({"schema": "debridpulse-catalog-meta/1", "locale": "en", "meta": meta}), encoding="utf-8")
    (tmp_path / "labels.json").write_text(json.dumps({"schema": "debridpulse-help-labels/1", "spans": {"help.minimal.open": [
        {"text": "Settings", "kind": "control", "key": "shell.nav.settings", "reason": "The sidebar entry."}]}}), encoding="utf-8")
    (tmp_path / "concepts.json").write_text(json.dumps({"schema": "debridpulse-terms/1", "locale": "en",
        "protected": [{"text": "DebridPulse", "kind": "name", "reason": "Product name."}],
        "concepts": {"settings": {"en": "Settings", "key": "shell.nav.settings", "note": "Page name."}}}), encoding="utf-8")
    target = {"shell.nav.settings": "ZZ-Einstellungen", "help.minimal.open": "Zz <b>ZZ-Einstellungen</b> zz.",
              "account.clear.warning": "Zz DebridPulse zz zz."}
    (i18n / "de.json").write_text(json.dumps({"schema": "debridpulse-catalog/1", "locale": "de", "messages": target,
                                              "sources": {key: _sha(english[key]) for key in target}}), encoding="utf-8")
    (tmp_path / "terms").mkdir()
    (tmp_path / "terms" / "de.json").write_text(json.dumps(terms or {"schema": "debridpulse-terms/1", "locale": "de", "register": "test double",
                                                                    "terms": {"settings": {"target": "ZZ-Einstellungen"}}}), encoding="utf-8")
    ledger = {key: {"source": _sha(english[key]), "translation": _sha(text), "provenance": ["ai-generated", "ai-review"]}
              for key, text in target.items()}
    ledger["account.clear.warning"].update(provenance=["ai-generated", "ai-review", "ai-back-translation"],
                                           backTranslation="Anyone who can reach DebridPulse can sign in.")
    for key, change in (entries or {}).items():
        ledger[key].update(change)
    (tmp_path / "review").mkdir()
    (tmp_path / "review" / "de.json").write_text(json.dumps({"schema": "debridpulse-review/1", "locale": "de", "entries": ledger}), encoding="utf-8")
    return [f"--i18n={i18n}", f"--meta={tmp_path / 'en.meta.json'}", f"--labels={tmp_path / 'labels.json'}",
            f"--terms={tmp_path / 'concepts.json'}", "--complete=de"]


def _run(flags):
    done = subprocess.run([NODE, str(VALIDATOR), "--json", *flags], capture_output=True, text=True, timeout=60)
    return done.returncode, json.loads(done.stdout)


@needs_node
def test_ws5_the_candidate_check_passes_a_complete_reviewed_locale_and_nothing_less(tmp_path):
    code, report = _run(_minimal(tmp_path / "whole"))
    assert code == 0, report["errors"]
    for case, entries, expected in [
        ("unreviewed", {"shell.nav.settings": {"provenance": ["ai-generated"]}}, "1 translations without a current independent review"),
        ("critical not back-translated", {"account.clear.warning": {"provenance": ["ai-generated", "ai-review"]}},
         "1 critical keys without a current back-translation"),
        ("open critical concern", {"account.clear.warning": {"findings": [{"kind": "critical", "note": "Consequence weakened?", "status": "open"}]}},
         "1 open review findings (1 critical)"),
    ]:
        code, report = _run(_minimal(tmp_path / case, entries=entries))
        assert code == 1 and any(expected in line for line in report["errors"]), (case, report["errors"])


@needs_node
def test_ws5_the_review_sheet_lays_out_each_key_with_its_samples_and_escapes_text(tmp_path):
    flags = _minimal(tmp_path)
    out = tmp_path / "sheet.html"
    done = subprocess.run([NODE, str(ROOT / ".github" / "localization" / "review_sheet.mjs"), "--locale=de", f"--out={out}",
                           *[flag for flag in flags if not flag.startswith("--complete")]], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    sheet = out.read_text(encoding="utf-8")
    assert "account.clear.warning" in sheet and "critical-warning" in sheet and "ai-back-translation" in sheet
    assert "Zz &lt;b&gt;ZZ-Einstellungen&lt;/b&gt; zz." in sheet           # text, never markup
    assert "shell.nav.settings → ZZ-Einstellungen" in sheet                  # the Help's control references
    assert (tmp_path / "sheet.csv").read_text(encoding="utf-8").startswith("key,category,criticality,")



# WS5-0 r2: the sample values (messages.mjs samples) reach every value of every
# count and every case, nested branches included, without the full product.
def _samples(message: str) -> list[dict]:
    script = ("import {samples} from '%s';"
              "process.stdout.write(JSON.stringify(samples(process.argv[1]).map(row => Object.fromEntries("
              "Object.entries(row).map(([k, v]) => [k, String(v).replace(/[\\uE002\\u27e8\\u27e9]/g, '')])))));"
              % (ROOT / ".github" / "localization" / "messages.mjs").as_uri())
    done = subprocess.run([NODE, "--input-type=module", "-e", script, message], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


COUNTS = {"0", "1", "2", "3", "5", "11", "21", "101", "1.5"}


@needs_node
def test_ws5_samples_show_each_of_several_counts_at_every_value():
    english = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    rows = _samples(english["consolidation.files.body"])           # matched > targets (nested), unmatched
    for name in ("matched", "targets", "unmatched"):
        assert {row[name] for row in rows} == COUNTS, name
    # The nested plural is shown at every count under each of its parent's counts (so in each parent branch).
    assert {(row["matched"], row["targets"]) for row in rows} >= {(m, t) for m in COUNTS for t in COUNTS}
    assert len(rows) <= 100                                           # bounded: not 9 x 9 x 9


@needs_node
def test_ws5_samples_reach_an_argument_inside_each_select_case_with_that_case_chosen():
    rows = _samples("{mode, select, x {{n, plural, one {# x} other {# xs}}} y {{m, plural, one {# y} other {# ys}}} other {z}}")
    assert {row["mode"] for row in rows} == {"x", "y", "other"}
    assert {row["n"] for row in rows if row["mode"] == "x"} == COUNTS
    assert {row["m"] for row in rows if row["mode"] == "y"} == COUNTS


@needs_node
def test_ws5_a_defect_only_a_later_count_reaches_is_found(tmp_path):
    # The first of three counts selects its broken branch only at 101: the 48-row
    # odometer left it at 0, so the dropped <b> passed.
    english = {"test.case.counts": "{p, plural, =101 {<b>p</b>} other {p}} {q, plural, one {q} other {qs}} {r, plural, one {r} other {rs}}"}
    meta = {"test.case.counts": _meta(args={"p": "plural", "q": "plural", "r": "plural"})}
    i18n, meta_path = _scratch(tmp_path, english=english, meta=meta)
    (i18n / "de.json").write_text(json.dumps({"schema": "debridpulse-catalog/1", "locale": "de", "messages": {
        "test.case.counts": "{p, plural, =101 {zp} one {zp} other {zp}} {q, plural, one {zq} other {zqs}} {r, plural, one {zr} other {zrs}}"},
        "sources": {"test.case.counts": _sha(english["test.case.counts"])}}), encoding="utf-8")
    code, report = _validate(i18n, meta_path)
    assert code == 1 and report["errors"] == [report["errors"][0]] and "rich-text tags differ from the English at p=101" in report["errors"][0], report["errors"]


# WS3a: the shell and shared owners say every word through the catalog. The
# owners are whole, and index.html's shell, Dashboard, Activity Log and
# Statistics regions (Downloads, Details and the download operations are WS3b).
WS3A_OWNERS = ("operator-title.js", "ui-statistics.js", "ui-activity-log-runtime.js", "ui-error-semantics.js",
               "ui-dashboard-transfer-presentation.js", "ui-provider-status.js", "ui-premium-account-status.js",
               "ui-storage-health.js", "ui-pager.js", "ui-search.js", "ui-toast-contract.js", "ui-tooltip.js",
               "ui-accessibility-runtime.js", "ui-theme-picker.js", "ui-theme-bootstrap.js",
               "ui-transfer-source-presentation.js", "ui-i18n.js", "auth.js")
# WS3b: the download operations -- Downloads, Details, file selection,
# provider and source switching, authentication prompts, the folder picker.
# app.js is whole from WS3b on (its Details, route history and upload owners).
WS3B_OWNERS = ("app.js", "ui-downloads.js", "ui-detail-candidates.js", "ui-file-selection.js", "ui-root-provider.js",
               "ui-group-candidates.js", "ui-auth-required.js", "ui-settings-directory-picker.js", "ui-settings-modal.js")


# WS3c: Settings services and engines -- the Services, Downloads and Extraction
# sections and the shared Settings shell of ui-settings-page.js (WS3d keeps the
# rest of that file), and three whole owners.
WS3C_OWNERS = ("ui-settings-usenet-servers.js", "ui-settings-executor-work.js", "ui-settings-archive-passwords.js")
WS3C_PAGE_REGION = ("  function input(", "  function notificationStatus(")
WS3C_PAGE_FUNCTIONS = ("render", "selectTabControl", "setDisclosureExpanded", "writeSettingsDocument", "load",
                       "headerEnableToggle", "confirmLiveTuning", "reportLiveTuning", "clearAllDebridKey",
                       "clearArchivePasswords", "testConnection", "testUsenet", "setBusy")


# WS3d: Settings administration and integrations -- Authentication, Users &
# Access, Notifications and Data & Maintenance, the rest of ui-settings-page.js
# (which is whole from WS3d on), and the owners they speak through.
WS3D_OWNERS = ("ui-settings-page.js", "ui-settings-backup-manager.js", "ui-settings-persistence.js",
               "oidc-verify-complete.html")


def ws3c_page_lines():
    """The ui-settings-page.js lines WS3c owns: its Services/Downloads/Extraction region,
    the TABS table and the named shell and engine functions (each to its closing ``  }``)."""
    page = (ROOT / "frontend" / "static" / "ui-settings-page.js").read_text(encoding="utf-8").splitlines()
    first = lambda marker: next(i for i, line in enumerate(page, 1) if line.startswith(marker))
    owned = set(range(first(WS3C_PAGE_REGION[0]), first(WS3C_PAGE_REGION[1])))
    owned |= set(range(first("  const TABS = "), first("  const state = {")))
    for name in WS3C_PAGE_FUNCTIONS:
        start = next(i for i, line in enumerate(page, 1) if re.match(rf"  (async )?function {name}\(", line))
        end = next(i for i, line in enumerate(page, 1) if i > start and line == "  }")
        owned |= set(range(start, end + 1))
    return owned


def test_the_ws3a_to_ws3d_owners_hold_no_literal_outside_the_catalog_or_the_reviewed_exemptions():
    import inventory

    data = inventory.census()
    left = [(entry["owner"], entry["line"], entry["english"]) for entry in data["entries"]
            if entry["owner"].rsplit("/", 1)[-1] in WS3A_OWNERS + WS3B_OWNERS + WS3C_OWNERS + WS3D_OWNERS]
    assert left == []
    # WS3c's region on its own as well (still its owner's, with WS3d whole beside it).
    owned = ws3c_page_lines()
    assert [(entry["line"], entry["english"]) for entry in data["entries"]
            if entry["owner"].endswith("ui-settings-page.js") and entry["line"] in owned] == []
    # index.html is whole: its shell and pages (WS3a) and Downloads and the dialogs (WS3b).
    assert [(entry["line"], entry["english"]) for entry in data["entries"] if entry["owner"].endswith("index.html")] == []
    # Every exemption names its reason, and still matches something (no stale entry).
    reviewed = json.loads((ROOT / ".github" / "localization" / "nonlocalizable.json").read_text(encoding="utf-8"))["exempt"]
    assert all(item["reason"].strip() for item in reviewed)
    matched = {(entry["owner"], entry["english"]) for entry in data["exempt"]}
    matched |= {(entry["owner"], entry["english"]) for entry in data["server"] if entry.get("reason")}
    assert [item for item in reviewed if (item["owner"], item["english"]) not in matched] == []
    # WS3e: every server message is coded at its raise or exempt with a reason.
    assert [(entry["owner"], entry["line"], entry["english"]) for entry in data["server"]
            if not entry["coded"] and not entry.get("reason")] == []


# WS4a: the Field Manual (the Help page), the License viewer's own words and
# the shared section navigator it draws its header with. ui-settings-help.js
# (Settings Help, WS4b) is the census's only owner left.
WS4A_OWNERS = ("ui-help-page.js", "ui-help-license-documents.js", "ui-section-nav.js")


def test_the_ws4a_help_owners_hold_no_literal_outside_the_catalog_or_the_reviewed_exemptions():
    import inventory

    data = inventory.census()
    assert [(entry["owner"], entry["line"], entry["english"]) for entry in data["entries"]
            if entry["owner"].rsplit("/", 1)[-1] in WS4A_OWNERS] == []
    assert {entry["owner"].rsplit("/", 1)[-1] for entry in data["entries"]} <= {"ui-settings-help.js"}


# WS4b: Settings Help (ui-settings-help.js) -- every topic and the role table.
# With it, the census holds no first-party interface literal outside the
# catalog or the reviewed exemptions.
def test_the_census_holds_no_first_party_literal_outside_the_catalog_from_ws4b_on():
    import inventory

    data = inventory.census()
    assert [(entry["owner"], entry["line"], entry["english"]) for entry in data["entries"]] == []
    # Every key Settings Help names is English's, and the role table's plain
    # answers are facts (data-answer), never read from their words.
    source = (ROOT / "frontend" / "static" / "ui-settings-help.js").read_text(encoding="utf-8")
    messages = json.loads((I18N / "en.json").read_text(encoding="utf-8"))["messages"]
    named = set(re.findall(r"'((?:help|settings|integration|account)\.[a-z0-9_.]+)'", source))
    assert len(named) > 300 and named <= set(messages), sorted(named - set(messages))
    assert "const YES = Object.freeze({key: 'help.roles.yes', answer: 'yes'});" in source
    assert "const NO = Object.freeze({key: 'help.roles.no', answer: 'no'});" in source
    assert source.count("dataset.answer = value.answer") == 1 and source.count("dataset.answer = values[index].answer") == 1
    assert "toLowerCase" not in source


def test_the_census_counts_keyed_markup_as_keyed_and_a_sentence_as_a_sentence():
    import inventory

    markup = inventory.markup_messages('<p data-i18n="a.b">Keyed words</p><p title="Plain tip" data-i18n-attr="title:a.c">Shown words</p>'
                                       '<div>Literal words</div>')
    # The keyed text and the keyed title are not literals; the rest are.
    assert [(kind, text) for kind, text, _rich, _line in markup] == [("markup-text", "Shown words"), ("markup-text", "Literal words")]
    found = inventory.frontend_entries_from("const a = 'Download Storage is full. New downloads are paused.';\n"
                                            "const b = '.row > .cell';\nconst c = 'status.completed';\n"
                                            "const d = 'dp-row dp-row--compact';\n", "frontend/static/x.js")
    assert [entry["english"] for entry in found] == ["Download Storage is full. New downloads are paused."]


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
    assert localization.pre_sign_in_locale(None) == "en" and localization.pre_sign_in_locale("xx") == "en"
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
