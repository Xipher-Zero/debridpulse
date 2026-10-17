"""What an operator may WRITE into a Settings value -- enforced where it is written.

A Settings value has one rule, declared once on the schema that owns it (the
model field). This module supplies the two pieces every Settings write boundary
shares, so no route re-implements either:

``validate_changes``
    Each value a request CHANGES is validated against its field exactly --
    strictly (``"5"``, ``true``, ``5.0`` or ``NaN`` is never read as a whole
    number, and a string is never read as a number), under the write context
    below. A value equal to what is stored is not re-judged: a historically
    accepted stored value that a newer rule would refuse survives startup and
    every unrelated save untouched, and only an operator CHANGING it meets the
    current rule.

Write rules (``Range``, ``Choice``, ``MaxLength``, ``HttpUrl``, ``AvatarUrl``,
``AbsolutePath``, ``HostName``)
    ``Annotated`` metadata on a field, enforced only under the write context, so
    they never refuse or rewrite a stored value at load. A ``Range`` declared
    ``clamp_at_load`` is ALSO the bound the existing load-time correction applies
    (``core.config_validator``), so a field's range is declared exactly once.

A refusal is an ordinary pydantic ``ValidationError`` whose error type and
context name the violated rule (``api.routes._validation_refusal`` words it);
the refused value is never part of the message.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic_core import PydanticCustomError, core_schema

WRITE_CONTEXT = {"settings_write": True}
# The largest whole number a browser (or any JSON client using IEEE doubles)
# can represent exactly: a larger "whole number" cannot round-trip through the
# Settings UI and is refused rather than silently rounded.
MAX_SAFE_INTEGER = 2 ** 53 - 1


def is_write(info) -> bool:
    """Whether a validator is running for a Settings WRITE (not a load)."""
    context = getattr(info, "context", None)
    return bool(context and context.get("settings_write"))


def _number(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


class _WriteRule:
    """A rule checked only when a value is written (``WRITE_CONTEXT``)."""

    def __get_pydantic_core_schema__(self, source, handler):
        return core_schema.with_info_after_validator_function(self._validate, handler(source))

    def _validate(self, value, info):
        if value is not None and is_write(info):
            self.check(value)
        return value

    def check(self, value) -> None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True)
class Range(_WriteRule):
    """A written number lies in ``[ge, le]`` and is finite. ``clamp_at_load``
    marks a range the load-time correction already applies to stored values
    (pre-existing behaviour); a range without it never touches a stored value."""
    ge: float | None = None
    le: float | None = None
    clamp_at_load: bool = False

    def check(self, value) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise PydanticCustomError("finite_number", "Input should be a finite number")
        if self.ge is not None and value < self.ge:
            raise PydanticCustomError("greater_than_equal", "Input should be greater than or equal to {ge}",
                                      {"ge": _number(self.ge)})
        if self.le is not None and value > self.le:
            raise PydanticCustomError("less_than_equal", "Input should be less than or equal to {le}",
                                      {"le": _number(self.le)})


@dataclass(frozen=True)
class Choice(_WriteRule):
    """A written value is one of ``values`` (compared case-insensitively for
    text when ``casefold``)."""
    values: tuple
    casefold: bool = False

    def check(self, value) -> None:
        probe = value.casefold() if self.casefold and isinstance(value, str) else value
        allowed = [v.casefold() if self.casefold and isinstance(v, str) else v for v in self.values]
        if probe not in allowed:
            words = [str(v) for v in self.values]
            expected = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]
            raise PydanticCustomError("setting_choice", "Input should be {expected}", {"expected": expected})


@dataclass(frozen=True)
class MaxLength(_WriteRule):
    """A written text is at most ``max_length`` characters."""
    max_length: int

    def check(self, value: str) -> None:
        if len(value) > self.max_length:
            raise PydanticCustomError("string_too_long", "String should have at most {max_length} characters",
                                      {"max_length": self.max_length})


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _plain_http_url(value: str) -> bool:
    match = re.fullmatch(r"https?://([^/?#\s]+)(?:[/?#]\S*)?", value, flags=re.IGNORECASE)
    return bool(match and not _CONTROL.search(value) and match.group(1).strip("@:"))


@dataclass(frozen=True)
class HttpUrl(_WriteRule):
    """A written address is empty or one plain ``http(s)://host...`` URL: no
    whitespace, no control characters, at most ``max_length`` characters."""
    max_length: int = 2048

    def check(self, value: str) -> None:
        if value == "":
            return
        if len(value) > self.max_length or not _plain_http_url(value):
            raise PydanticCustomError("setting_url", "Input should be an http:// or https:// address")


@dataclass(frozen=True)
class AvatarUrl(_WriteRule):
    """A sender avatar Discord itself fetches: empty, or an http(s) image
    address that is neither a ``data:`` URI nor an SVG (Discord refuses both)."""
    max_length: int = 2048

    def check(self, value: str) -> None:
        if value == "":
            return
        path = re.split(r"[?#]", value, maxsplit=1)[0].casefold()
        if len(value) > self.max_length or not _plain_http_url(value) or path.endswith(".svg"):
            raise PydanticCustomError(
                "setting_avatar", "Input should be an http:// or https:// image address that is not an SVG")


@dataclass(frozen=True)
class AbsolutePath(_WriteRule):
    """A written folder is an absolute path without control characters, at
    most ``max_length`` characters. (A relative one would resolve against the
    server process's working directory -- not a folder anyone chose.)"""
    max_length: int = 4096

    def check(self, value: str) -> None:
        if not value.startswith("/") or len(value) > self.max_length or _CONTROL.search(value):
            raise PydanticCustomError("setting_path", "Input should be an absolute folder path")


@dataclass(frozen=True)
class HostName(_WriteRule):
    """A written server address is empty or one host name or IP address: no
    scheme, path, credentials, whitespace or control characters, at most 253
    characters (the DNS limit)."""

    def check(self, value: str) -> None:
        if value == "":
            return
        if len(value) > 253 or re.search(r"[\s/@?#\\]", value) or _CONTROL.search(value) or "://" in value:
            raise PydanticCustomError(
                "setting_host", "Input should be a host name or IP address without a scheme, path or spaces")


def _same(left, right) -> bool:
    """Whether a submitted value IS what is stored -- the same JSON value of the
    same type, all the way down. ``100.0`` is not a stored ``100`` and ``true``
    is not a stored ``1``: a value of another type is a change and meets the
    field's strict rule, even where Python would call the two equal. Only a
    value identical to the stored one is left un-judged (the untouched-legacy
    contract)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return left == right or (math.isnan(left) and math.isnan(right))
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    return left == right


def _stored(previous, name: str, missing):
    if previous is None:
        return missing
    if isinstance(previous, dict):
        return previous.get(name, missing)
    return getattr(previous, name, missing)


def validate_changes(model, values: dict, previous=None, *, names: Iterable[str] | None = None) -> None:
    """Validate every value in ``values`` that differs from ``previous`` (a
    mapping or a model instance; ``None`` = everything is new) against
    ``model``'s own field -- strictly, under the write context. Raises the
    field's ``ValidationError``; returns nothing (the caller keeps its own
    construction of the accepted document).

    ``names`` limits which keys are considered (e.g. a document's flat fields
    only)."""
    missing = object()
    probe = model.model_construct()
    considered = set(names) if names is not None else None
    for name, value in values.items():
        if name not in model.model_fields or (considered is not None and name not in considered):
            continue
        if _same(value, _stored(previous, name, missing)):
            continue
        model.__pydantic_validator__.validate_assignment(
            probe, name, value, strict=True, context=WRITE_CONTEXT)


def load_clamped_ranges(model) -> dict[str, tuple[Any, Any]]:
    """The ``Range`` bounds the load-time correction applies, by field."""
    ranges = {}
    for name, field in model.model_fields.items():
        for rule in field.metadata:
            if isinstance(rule, Range) and rule.clamp_at_load:
                ranges[name] = (_number(rule.ge), _number(rule.le))
    return ranges
