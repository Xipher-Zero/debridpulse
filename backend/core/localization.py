"""DebridPulse's server side of localization.

The interface's messages live in one place, the catalogs under
``frontend/static/i18n`` (``manifest.json`` names every locale; ``en.json`` is
the canonical English source, with each key's translator context). The
browser formats them (``ui-i18n.js``, ICU MessageFormat through the vendored
FormatJS formatter). The server reads the same files for three things only:

* which locale an account may hold (``locale_tags``, the one admission rule
  ``services.account_appearance`` checks every choice and every stored value
  against): English, and a target the manifest marks ``supported`` that has
  its catalog. A ``planned`` target is never admitted; a stored tag that is
  not (any more) reads as English and is left as stored;
* what a target catalog delivers (``delivered``): only its current
  translations. A translation records the hash of the English it was made
  from (``sources``); where English has changed since, the translation is
  stale and is not delivered, so that key shows the current English, like a
  missing one, while every current translation stays. The delivered catalog
  names its stale keys, so the interface counts them as debt apart from the
  missing ones, and its asset version is the hash of what is delivered: an
  English change that makes a translation stale changes the URL and ETag
  even though the target file did not change. A catalog that cannot be read
  is not delivered at all (the page stays English);
* the pages it renders itself before anyone signs in (the sign-in page), whose
  messages are restricted to plain ``{name}`` arguments (the catalog validator
  enforces it), so ``message`` needs no general formatter. Before sign-in
  there is no account and so no account language: those pages are English
  unless a caller states a locale explicitly (``pre_sign_in_locale``);
* ``DisplayError``: an error the interface shows, raised with a stable
  ``code`` (and ``params``) beside its unchanged English ``detail``, so the
  interface can show it in the reader's language while API clients keep the
  English text and status they always had.

Pseudo locales (``en-XA``, ``ar-XB``) are developer test locales, never
offered: an account may hold one only where the installation runs with
``DEBRIDPULSE_I18N_TEST_LOCALES=1`` (the isolated test instances). That
setting admits nothing else: tests of a supported target point ``STATIC_DIR``
at a synthetic catalog directory instead.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

DEFAULT_LOCALE = "en"
TEST_LOCALES_ENV = "DEBRIDPULSE_I18N_TEST_LOCALES"

logger = logging.getLogger(__name__)


def catalog_dir() -> Path:
    configured = os.environ.get("STATIC_DIR", "").strip()
    for candidate in ((Path(configured) / "i18n",) if configured else ()) + (
            Path(__file__).resolve().parents[2] / "frontend" / "static" / "i18n",
            Path("/app/frontend/static/i18n")):
        if (candidate / "manifest.json").is_file():
            return candidate
    raise RuntimeError("The localization catalogs are missing")


@lru_cache(maxsize=64)
def _parsed(path: str, _mtime_ns: int, _size: int) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read(path: Path) -> dict | None:
    """A catalog file's JSON, cached by the file's identity (path, mtime,
    size): a changed file is read again, never served from an older read."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return _parsed(str(path), stat.st_mtime_ns, stat.st_size)


def manifest() -> dict:
    return _read(catalog_dir() / "manifest.json")


def source_hash(english: str) -> str:
    """The identity of an English message a translation was made from
    (``sources[key]`` in a target catalog; the validator computes the same)."""
    return hashlib.sha256(english.encode("utf-8")).hexdigest()[:16]


def test_locales_enabled() -> bool:
    return os.environ.get(TEST_LOCALES_ENV, "").strip() == "1"


def locale_tags(*, include_test: bool | None = None) -> tuple[str, ...]:
    """Every locale an account may hold: the canonical language, each target
    the manifest marks ``supported`` that has a readable catalog, and the
    pseudo locales where test locales are enabled. Never a ``planned`` one."""
    include_test = test_locales_enabled() if include_test is None else include_test
    admitted = []
    for tag, info in manifest()["locales"].items():
        status = info["status"]
        if (status == "canonical" or status == "supported" and delivered(tag) is not None
                or status == "pseudo" and include_test):
            admitted.append(tag)
    return tuple(admitted)


def locale_info(tag: str) -> dict:
    return manifest()["locales"][tag]


def delivered(tag: str) -> dict | None:
    """What a locale's catalog delivers: ``{"messages", "stale"}``, or None
    where it has no readable catalog. English delivers itself whole. A
    target delivers only its current translations -- a key English has,
    a string, recorded (``sources``) as made from the English text as it is
    now; ``stale`` names the keys it translated from an older English text,
    which are not delivered. (A translation's arguments and plural forms are
    the validator's to check; the interface falls back to English, key by
    key, for one that does not format.)"""
    english = _read(catalog_dir() / f"{DEFAULT_LOCALE}.json")["messages"]
    if tag == DEFAULT_LOCALE:
        return {"messages": english, "stale": []}
    try:
        data = _read(catalog_dir() / f"{tag}.json")
        if data is None:
            return None
        messages, sources = data["messages"], data.get("sources") or {}
        if not isinstance(messages, dict) or not isinstance(sources, dict):
            raise TypeError("messages and sources must be objects")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        logger.warning("Locale catalog %s is unreadable and is not delivered: %s", tag, type(exc).__name__)
        return None
    current, stale = {}, []
    for key, text in messages.items():
        if key not in english or not isinstance(text, str):
            continue
        if sources.get(key) == source_hash(english[key]):
            current[key] = text
        else:
            stale.append(key)
    return {"messages": current, "stale": sorted(stale)}


def catalog(tag: str) -> dict:
    """A locale's delivered messages (``{}`` where it has no catalog)."""
    found = delivered(tag)
    return found["messages"] if found else {}


def catalog_script(tag: str) -> str | None:
    """The catalog asset's body: ``window.DPI18nCatalog(tag, messages)``,
    with ``{"stale": [...]}`` as a third argument where a target has stale
    keys; None where the locale has no readable catalog."""
    found = delivered(tag)
    if found is None:
        return None
    parts = [json.dumps(tag), json.dumps(found["messages"], ensure_ascii=False, separators=(",", ":"))]
    if found["stale"]:
        parts.append(json.dumps({"stale": found["stale"]}, separators=(",", ":")))
    return f"window.DPI18nCatalog({','.join(parts)});"


def catalog_version(tag: str) -> str | None:
    """The catalog asset's identity, which versions its URL and ETag:
    English's is the hash of ``en.json`` (``index.html`` names it); a
    target's is the hash of what it delivers, so an English change that
    makes one of its translations stale gives it a new version."""
    if tag == DEFAULT_LOCALE:
        return hashlib.sha256((catalog_dir() / f"{DEFAULT_LOCALE}.json").read_bytes()).hexdigest()[:16]
    script = catalog_script(tag)
    return None if script is None else hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]


_ACCENTS = str.maketrans("aceinoustyzACEINOUSTYZ", "àçéîñöüšţýžÀÇÉÎÑÖÜŠŢÝŽ")
_ARGUMENT = re.compile(r"\{(\w+)\}")


def pseudo(message: str, tag: str) -> str:
    """The pseudo form of a plain message: accented letters, about 40%
    longer, bracketed (``en-XA``), or wrapped in right-to-left marks
    (``ar-XB``). Arguments are left exactly as they are."""
    parts = _ARGUMENT.split(message)
    out = []
    for index, part in enumerate(parts):
        if index % 2:
            out.append("{" + part + "}")
        else:
            out.append(part.translate(_ACCENTS) if tag == "en-XA" else part)
    body = "".join(out)
    if tag == "en-XA":
        letters = len(re.sub(r"\{\w+\}|\s", "", message))
        return "⟦" + body + " " + "·" * max(1, round(letters * 0.4)) + "⟧"
    return "‮" + body + "‬" if tag == "ar-XB" else body


def message(key: str, tag: str = DEFAULT_LOCALE, **params) -> str:
    """A plain message (``{name}`` arguments only) in ``tag``, falling back
    to English for a key the locale lacks. Used only for server-rendered
    pre-authentication pages."""
    english = catalog(DEFAULT_LOCALE)[key]
    if locale_info(tag)["status"] == "pseudo":
        text = pseudo(english, tag)
    else:
        text = catalog(tag).get(key, english)
    return _ARGUMENT.sub(lambda m: str(params.get(m.group(1), m.group(0))), text)


def pre_sign_in_locale(explicit: str | None = None) -> str:
    """The language of a page shown before sign-in. There is no account, and
    neither ``Accept-Language`` nor the browser's language is read: English,
    unless a caller explicitly states a locale the installation holds (the
    hook a later explicit pre-sign-in choice will use; today only tests)."""
    return explicit if explicit in locale_tags() else DEFAULT_LOCALE


class DisplayError(HTTPException):
    """An HTTP error the interface shows: ``detail`` stays the English text
    API clients already receive; ``code`` (a key under ``error.`` in the
    catalog) and ``params`` let the interface show it in its language."""

    def __init__(self, status_code: int, detail: str, *, code: str, params: dict | None = None,
                 headers: dict | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.params = dict(params or {})


def display_error_body(error: DisplayError) -> dict:
    return {"detail": error.detail, "code": error.code, "params": error.params}
