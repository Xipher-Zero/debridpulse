"""Settings Batch 1, Item 3: a Settings number control and the write boundary
that owns its value agree on the value's range.

The persistence owner refuses a number outside a control's declared
``min``/``max`` before any request (``ui-settings-persistence.js``
``numberDraft``), and the server refuses it again at its write boundary
(``core.settings_constraints``). They are two enforcements of ONE rule, so this
pins them together: every numeric Settings control -- every ``COMMIT_FIELDS``
number field and every Usenet server number field -- declares exactly the
bounds its canonical field declares. A control tighter than the server would
refuse a value the server accepts; a looser one would send what the server
refuses.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from annotated_types import Ge, Le

from api.auth_config_routes import AuthenticationConfigUpdate
from core.config import AppSettings
from core.settings_constraints import Range
from integrations.catalog import definitions
from integrations.usenet.definition import UsenetServer
from transfers.settings import TransferSettings

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
PAGE = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")


def _commit_fields() -> dict[str, tuple[str, str]]:
    table = PAGE[PAGE.index("const COMMIT_FIELDS = Object.freeze({"):]
    table = table[:table.index("\n  });\n")]
    return {key: (scope, option) for key, scope, option in re.findall(
        r"^\s*(\w+): \{scope: '([^']+)',\s*option: '([^']+)'", table, flags=re.MULTILINE)}


def _number_controls() -> dict[str, dict]:
    """Every ``input('<key>', ...)`` the page renders as a number, with the
    bounds its options declare."""
    controls = {}
    for match in re.finditer(r"\binput\('(\w+)',", PAGE):
        body = PAGE[match.end():match.end() + 900]
        options = body[:body.index("})") + 1] if "})" in body else body
        if "type: 'number'" not in options:
            continue
        bounds = {name: float(value) for name, value in re.findall(r"\b(min|max): (-?[\d.]+)", options)}
        controls[match.group(1)] = bounds
    return controls


def _bounds(model, name: str) -> dict:
    field = model.model_fields[name]
    found = {}
    for rule in field.metadata:
        if isinstance(rule, Ge):
            found["min"] = float(rule.ge)
        elif isinstance(rule, Le):
            found["max"] = float(rule.le)
        elif isinstance(rule, Range):
            if rule.ge is not None:
                found["min"] = float(rule.ge)
            if rule.le is not None:
                found["max"] = float(rule.le)
    return found


def _canonical_model(scope: str):
    if scope == "transfer-policy":
        return TransferSettings
    if scope == "settings-document":
        return AppSettings
    if scope == "auth-config":
        return AuthenticationConfigUpdate
    if scope.startswith("integration:"):
        identity = scope.split(":", 1)[1]
        return next(d.options_model for d in definitions if d.id == identity)
    raise AssertionError(f"unknown scope {scope}")


# A control may omit a bound only where the canonical field's bound is not a
# number an operator could reach (a representability ceiling).
_REPRESENTABILITY_CEILING = 2 ** 53 - 1


def test_every_settings_number_control_is_a_declared_commit_field():
    controls, fields = _number_controls(), _commit_fields()
    assert len(controls) >= 45
    assert set(controls) <= set(fields), sorted(set(controls) - set(fields))


@pytest.mark.parametrize("key", sorted(_number_controls()))
def test_number_control_bounds_equal_the_canonical_field(key):
    scope, option = _commit_fields()[key]
    declared = _number_controls()[key]
    canonical = _bounds(_canonical_model(scope), option)
    if canonical.get("max") == _REPRESENTABILITY_CEILING:
        canonical.pop("max")
    assert declared == canonical, f"{key} ({scope} {option}): control {declared} != canonical {canonical}"


def test_usenet_server_number_controls_equal_the_server_record():
    for name in ("port", "connections", "priority", "articles_per_request", "timeout_seconds"):
        match = re.search(
            r'<input class="input" type="number" min="(\d+)" max="(\d+)" data-usenet-field="' + name + '"', PAGE)
        assert match, name
        assert {"min": float(match.group(1)), "max": float(match.group(2))} == _bounds(UsenetServer, name), name
