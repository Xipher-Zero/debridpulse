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

Error codes (WS3e). A refusal is named where its condition is known: at the
raise, by ``DisplayError(status, detail, code=…)`` or by a ``Coded``
exception (``CodedValueError`` and the domain refusals that mix ``Coded`` in),
whose type is unchanged for every ``except`` that catches it. A boundary that
turns an exception into a response (``refusal``) keeps the English ``detail``
exactly and carries the exception's own code; an exception that names none is
marked ``unclassified`` -- the explicit fallback, claiming nothing more -- and
the interface shows its English ``detail``. A code is never derived from the
English text. ``params`` are the values the English already shows, never a
secret (``_safe_params`` refuses secret-named or non-scalar values).

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

from core.logging_utils import sanitize_log_value

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
        # The params it sends never disclose more than ``detail`` (``disclosed``).
        self.code, self.params = disclosed(detail, _checked_code(code), params)
        # A boundary that wraps this refusal keeps its code (``code_of``).
        self.display_code, self.display_params = self.code, self.params


def display_error_body(error: DisplayError) -> dict:
    return {"detail": error.detail, "code": error.code, "params": error.params}


# ── error codes (WS3e) ─────────────────────────────────────────────────────

# The explicit fallback: a refusal no condition names. The interface shows its
# English ``detail``; it is never a claim about what went wrong.
UNCLASSIFIED = "unclassified"
_CODE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
# Parameter names that could carry a credential are refused outright.
_SECRET_NAME = re.compile(r"(?:password|passwd|secret|token|api_?key|credential|authorization|cookie|session)", re.I)
_PARAM_MAX = 500


def _checked_code(code: str) -> str:
    if code != UNCLASSIFIED and not _CODE.fullmatch(str(code or "")):
        raise ValueError(f"not an error code: {code!r}")
    return str(code)


class Fact(str):
    """Text a refusal may interpolate: a value the server itself establishes --
    a figure it computed, an identifier from a closed set it recognises
    (``known``), a schema's own field or bound -- never caller- or
    service-controlled text. Only a producer that knows the value's origin
    makes one (``fact``); every other text parameter is not admitted
    (``disclosed``), whatever it contains."""


def fact(value) -> Fact:
    """A server-established value (a figure, a schema name or bound, an
    identifier the producer itself validated) as admissible text."""
    return Fact(str(value))


def known(value, allowed) -> Fact | None:
    """``value`` as admissible text only when it is one of the closed set
    ``allowed`` the producer recognises; ``None`` (not admitted) otherwise."""
    text = str(value)
    return Fact(text) if text in {str(getattr(item, "value", item)) for item in allowed} else None


def _safe_params(params: dict | None) -> dict:
    """The values an English refusal already shows, as plain scalars: never a
    secret-named value, never a nested structure, strings bounded."""
    safe = {}
    for name, value in dict(params or {}).items():
        if _SECRET_NAME.search(str(name)):
            raise ValueError(f"error parameter {name!r} could carry a secret")
        if isinstance(value, dict):
            # A coded cause inside a wrapping refusal (``said``): the interface
            # says it from its own code; never any other structure.
            if set(value) != {"$t", "$v"} or not str(value["$t"]).startswith("error."):
                raise ValueError(f"error parameter {name!r} is not a scalar")
            _checked_code(str(value["$t"])[len("error."):])
            safe[str(name)] = {"$t": str(value["$t"]), "$v": _safe_params(value["$v"])}
            continue
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"error parameter {name!r} is not a scalar")
        # Text passes the canonical sanitizer every API detail uses; a fact the
        # sanitizer would change is no clean fact and loses its authority.
        # Numbers and booleans keep their types for ICU formatting.
        if isinstance(value, str):
            clean = sanitize_log_value(value, max_length=_PARAM_MAX)
            safe[str(name)] = Fact(clean) if isinstance(value, Fact) and clean == value else clean
        else:
            safe[str(name)] = value
    return safe


def _texts(params: dict):
    """Every text value in ``params``, a coded cause's own included."""
    for value in params.values():
        if isinstance(value, dict):
            yield from _texts(value["$v"])
        elif isinstance(value, str):
            yield value


def disclosed(detail, code: str | None, params: dict | None) -> tuple[str, dict]:
    """The code and params a refusal may send beside ``detail``.

    Safe interpolation is established by authority, never by content: a text
    parameter (a coded cause's included) is sent only when its producer made it
    a ``Fact`` -- a server-established value -- that the canonical sanitizer
    leaves unchanged and the detail as sent shows. Caller- or service-
    controlled text is never admitted, however it is spelled or encoded:
    the refusal is ``unclassified`` with no params -- it fails closed -- and the
    interface shows the English detail, which is never altered here. Numbers
    and booleans carry no text and keep their types."""
    if not code or code == UNCLASSIFIED:
        return UNCLASSIFIED, {}
    safe = _safe_params(params)
    shown = str(detail if isinstance(detail, str) else "").casefold()
    for text in _texts(safe):
        if not isinstance(text, Fact) or text.casefold() not in shown:
            return UNCLASSIFIED, {}
    return _checked_code(code), safe


def said(exc: BaseException, english: str | None = None):
    """A cause as a parameter of the refusal that wraps it: its own code
    (``{"$t", "$v"}``, said by the interface) where it names one, else its
    English text (``english``, or the exception's own words)."""
    code, params = code_of(exc)
    return {"$t": f"error.{code}", "$v": params} if code else (str(exc) if english is None else english)


class Coded:
    """An exception that names its refusal for the interface: ``code`` (a key
    under ``error.`` in the catalog) and ``params``, beside its unchanged
    English message. Mixed into an existing exception type, so every
    ``except`` that caught it still does."""

    display_code: str | None = None
    display_params: dict = {}

    def __init__(self, *args, code: str | None = None, params: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.display_code = _checked_code(code) if code else None
        self.display_params = _safe_params(params)


class CodedValueError(Coded, ValueError):
    """A ``ValueError`` that names its refusal."""


class CodedRuntimeError(Coded, RuntimeError):
    """A ``RuntimeError`` that names its refusal."""


def code_of(exc: BaseException | None) -> tuple[str | None, dict]:
    """The code and params an exception names, if it names one."""
    code = getattr(exc, "display_code", None) if exc is not None else None
    return (code, dict(getattr(exc, "display_params", {}) or {})) if code else (None, {})


def refusal(status_code: int, detail: str, cause: BaseException | None = None, *, code: str | None = None,
            params: dict | None = None, headers: dict | None = None) -> DisplayError:
    """The response a boundary raises: ``detail`` exactly the English it always
    sent; named by the cause's own code where it has one, else by ``code``,
    else ``unclassified`` (the interface then shows the English ``detail``)."""
    named, named_params = code_of(cause)
    if named:
        return DisplayError(status_code, detail, code=named, params=named_params, headers=headers)
    if code:
        return DisplayError(status_code, detail, code=_checked_code(code), params=_safe_params(params), headers=headers)
    return DisplayError(status_code, detail, code=UNCLASSIFIED, headers=headers)
