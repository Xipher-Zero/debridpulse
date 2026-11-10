"""WS3e: the server names each refusal the interface shows by a stable code,
beside the English ``detail`` API clients always received.

These are the compatibility contracts:

- status and English ``detail`` unchanged;
- ``code`` and ``params`` additive;
- a code comes from the condition, never from the English;
- an unnamed refusal is the explicit ``unclassified`` fallback;
- no parameter can carry a secret;
- every code the source names has its catalog words, which are exactly the
  English it sends.
"""
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "localization"))

from core.localization import (UNCLASSIFIED, CodedRuntimeError, CodedValueError, DisplayError, code_of, fact,  # noqa: E402
                               refusal, said)

MESSAGES = json.loads((ROOT / "frontend" / "static" / "i18n" / "en.json").read_text(encoding="utf-8"))["messages"]


def _app():
    """A bare application with the real handlers ``main`` registers for refusals."""
    import main

    app = FastAPI()
    app.add_exception_handler(DisplayError, main.display_error_handler)
    app.add_exception_handler(RequestValidationError, main.request_validation_error_handler)
    return app


async def _get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


# ── the mechanism ───────────────────────────────────────────────────────────

def test_a_coded_exception_keeps_its_type_its_words_and_names_its_condition():
    refused = CodedValueError("A valid BitTorrent magnet is required", code="submit.magnet_invalid")
    assert isinstance(refused, ValueError) and str(refused) == "A valid BitTorrent magnet is required"
    assert code_of(refused) == ("submit.magnet_invalid", {})
    assert code_of(ValueError("A valid BitTorrent magnet is required")) == (None, {})
    assert isinstance(CodedRuntimeError("x", code="a.b"), RuntimeError)


def test_a_boundary_keeps_the_detail_and_names_the_cause_or_says_unclassified():
    named = refusal(409, "Database wipe is already in progress", CodedValueError("x", code="maintenance.wipe_in_progress"))
    assert (named.status_code, named.detail, named.code) == (409, "Database wipe is already in progress", "maintenance.wipe_in_progress")
    # An exception that names nothing is the explicit fallback, never a guess from its words.
    fallback = refusal(502, "Database wipe is already in progress", RuntimeError("Database wipe is already in progress"))
    assert fallback.code == UNCLASSIFIED and fallback.params == {}
    assert refusal(400, "x", None, code="bulk.no_ids").code == "bulk.no_ids"


def test_no_parameter_can_carry_a_secret_or_a_structure():
    for name in ("password", "api_key", "client_secret", "token", "session_id", "authorization"):
        with pytest.raises(ValueError):
            DisplayError(400, "x", code="a.b", params={name: "x"})
    with pytest.raises(ValueError):
        CodedValueError("x", code="a.b", params={"items": ["a"]})
    with pytest.raises(ValueError):
        DisplayError(400, "x", code="Not A Code")
    # A coded cause inside a wrapper is the one structure allowed: its own code and params.
    cause = CodedValueError("x", code="aria2.configuration_refused", params={"reason": "exit 28"})
    assert said(cause) == {"$t": "error.aria2.configuration_refused", "$v": {"reason": "exit 28"}}
    assert said(ValueError("plain words")) == "plain words"
    # No catalog refusal has an argument a credential could fill.
    for key, text in MESSAGES.items():
        if key.startswith("error."):
            assert not re.search(r"\{(?:password|secret|token|api_?key|credential)\b", text, re.I), key


# ── HTTP compatibility ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_coded_refusal_keeps_status_detail_and_headers_and_adds_code_and_params():
    app = _app()

    @app.get("/named")
    async def named():
        raise DisplayError(429, "Too many tests; wait a minute and try again", code="notifications.test_rate_limited",
                           headers={"Retry-After": "60"})

    @app.get("/forwarded")
    async def forwarded():
        try:
            raise RuntimeError("upstream said no")
        except RuntimeError as exc:
            raise refusal(502, str(exc), exc) from None

    response = await _get(app, "/named")
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    assert response.json() == {"detail": "Too many tests; wait a minute and try again",
                               "code": "notifications.test_rate_limited", "params": {}}
    response = await _get(app, "/forwarded")
    assert response.status_code == 502
    assert response.json() == {"detail": "upstream said no", "code": UNCLASSIFIED, "params": {}}


@pytest.mark.asyncio
async def test_a_client_without_the_handlers_still_reads_exactly_the_old_body():
    # A DisplayError is an HTTPException: an application without the refusal
    # handler (or a client reading only ``detail``) sees exactly what it always did.
    app = FastAPI()

    @app.get("/old")
    async def old():
        raise DisplayError(404, "Transfer not found", code="transfer.not_found")

    response = await _get(app, "/old")
    assert response.status_code == 404 and response.json() == {"detail": "Transfer not found"}


@pytest.mark.asyncio
async def test_a_refused_field_names_its_rule_and_bounds_never_the_value():
    app = _app()

    class Body(BaseModel):
        count: int = Field(le=8)

    @app.post("/field")
    async def field(body: Body):
        return {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/field", json={"count": 99})
    assert response.status_code == 422
    (item,) = response.json()["detail"]
    assert item["msg"] == "Value must be 8 or less." and item["requirement"] == "must be 8 or less"
    assert item["code"] == "validation.less_than_equal" and item["params"] == {"le": 8}
    assert "99" not in json.dumps(item)
    assert MESSAGES["error.requirement.less_than_equal"] == "must be {le} or less"


def test_structured_refusals_keep_their_machine_code_and_name_their_words():
    from fastapi import HTTPException

    import api.operational_downloads as downloads

    with pytest.raises(HTTPException) as refused:
        downloads._search_plan("x" * 5000)
    detail = refused.value.detail
    assert refused.value.status_code == 400 and detail["code"] == "invalid_query"
    assert detail["message"].startswith("Search is limited to ")
    assert detail["message_code"] == "search.too_long" and set(detail["params"]) == {"count"}


def test_wrapped_refusals_name_their_cause():
    from application.lifetime import RestoreFailed
    from services import backup

    rejected = backup.BackupRejected("add", backup.INVALID, detail_code="invalid_file")
    assert rejected.message == "Backup could not be added. The selected file is not a valid DebridPulse backup."
    assert code_of(rejected) == ("backup.add_detail", {"detail": {"$t": "error.backup.detail.invalid_file", "$v": {}}})
    assert code_of(backup.BackupRejected("save")) == ("backup.save", {})
    restore = RestoreFailed(400, rejected.detail, reason_said=rejected.detail_said)
    assert code_of(restore)[0] == "restore.failed_reason"
    # A reason no code names leaves the whole refusal unnamed (``unclassified`` at the boundary).
    assert code_of(RestoreFailed(400, "some words"))[0] is None
    assert code_of(RestoreFailed(400))[0] == "restore.failed"


def test_a_link_file_refusal_names_where_from_facts_not_from_its_words():
    from transfers.requests import link_file_entries

    with pytest.raises(ValueError) as refused:
        link_file_entries(b'[""]')
    assert str(refused.value) == "Entry 1 has no link"
    assert code_of(refused.value) == ("linkfile.location_no_link", {"kind": "entry", "number": 1})
    assert link_file_entries(b"https://a.example/x\n") == (("Line 1", "https://a.example/x"),)


# ── the catalog says exactly the English the server sends ───────────────────

# Messages that state one English refusal in ICU terms (a plural or a select
# standing for an f-string's own branches); their English is checked by case.
_STRUCTURAL = {"linkfile.location_no_link", "linkfile.location_too_long", "deployment.unchanged", "profiles.in_use",
               "queue.restart_required", "aria2.size_range", "aria2.size_range_capped"}


def _shape(text):
    text = re.sub(r"\{\d+\}|\{\w+(?:, (?:number|date|time))?\}", "{}", text)
    return text.replace("''", "'")


def test_every_code_the_source_names_has_its_words_and_they_are_the_english_it_sends():
    import inventory

    named = 0
    for entry in inventory.census()["server"]:
        if not entry["coded"] or entry["exception"] == "structured":
            continue
        source = (ROOT / entry["owner"]).read_text(encoding="utf-8")
        tail = source.split("\n")[entry["line"] - 1:entry["line"] + 6]
        match = re.search(r'(?<!\w)code\s*=\s*"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)"', "\n".join(tail))
        if not match:
            continue
        code = match.group(1)
        assert f"error.{code}" in MESSAGES, (entry["owner"], entry["line"], code)
        if code not in _STRUCTURAL:
            assert _shape(MESSAGES[f"error.{code}"]) == _shape(entry["english"]), (code, entry["english"])
        named += 1
    assert named > 200
    assert MESSAGES["error.queue.restart_required"].startswith("{count, plural, one {Reordering the queue interrupts {count}")


def test_a_transfer_refusal_by_category_says_the_servers_own_sentence():
    from transfers.errors import Category, NormalizedError, Domain, Stage

    for category in Category:
        error = NormalizedError(domain=Domain.EXECUTOR, category=category, stage=Stage.EXECUTION)
        assert MESSAGES[f"error.category.{category.value}"] == error.message, category


# ── no parameter discloses more than the sanitized detail (Gate 9 r1 blocker) ──

# Synthetic credentials only: none of these is a real secret.
_SECRETS = ("SYNTHETIC-PASS-1", "SYNTHETIC-KEY-2", "SYNTHETIC-TOKEN-3", "SYNTHETIC-PASSKEY-4")
# Sensitive values the canonical sanitizer does NOT recognise (r3): they stay in the
# English detail, as before WS3e (a separate, recorded defect of its owner), but no
# structured parameter may carry them.
_UNRECOGNISED = ("SYNTHETIC-ONLY-PASSKEY-5", "SYNTHETIC-ONLY-AUTHKEY-6", "SYNTHETIC-PCT-7", "SYNTHETIC-ARBITRARY-8")
_SCOPES = (
    "https://alice:SYNTHETIC-PASS-1@tracker.example/announce",
    "https://tracker.example/announce?apikey=SYNTHETIC-KEY-2&x=1",
    "https://tracker.example/announce?token=SYNTHETIC-TOKEN-3",
    "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=https%3A%2F%2Ft.example%2Fannounce%2FSYNTHETIC-PASSKEY-4",
    "https://tracker.example/announce?passkey=SYNTHETIC-ONLY-PASSKEY-5",
    "https://tracker.example/rss?authkey=SYNTHETIC-ONLY-AUTHKEY-6&type=1",
    # r4: no shape or encoding rule can establish safety -- authority does.
    "https%3A%2F%2Falice%3ASYNTHETIC-PCT-7%40tracker.example%2Fannounce",
    "SYNTHETIC-ARBITRARY-8",
)


def _links_app(choices=(("item", "Just this item"), ("collection", "The whole collection"))):
    """The real submission route and refusal handler. Its service is a stand-in whose
    ``submit_links`` runs the real scope admission (``ApplicationService._acquisition_scopes``)
    against a synthetic registry offering ``item``/``collection``: no provider, no transfer."""
    from types import SimpleNamespace

    import api.routes as routes
    from application.dependencies import get_application
    from application.service import ApplicationService
    from auth.models import Principal

    class Registry:
        @staticmethod
        def scope_choices(_request):
            return choices

    class Service:
        engine = SimpleNamespace(registry=Registry())

        async def submit_links(self, links, *, acquisition_scopes=None, **_kwargs):
            ApplicationService._acquisition_scopes(self, list(links), None, acquisition_scopes)
            raise AssertionError("an invalid scope must be refused before anything is admitted")

    app = _app()

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.principal = Principal.anonymous()
        return await call_next(request)

    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[get_application] = lambda: Service()
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", _SCOPES)
async def test_an_invalid_scope_never_leaks_a_credential_through_its_params(scope):
    from core.logging_utils import sanitize_exception

    app = _links_app()
    link = "https://files.example/item"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/links/add", json={"links": [link], "acquisition_scopes": {link: scope}})
    assert response.status_code == 400
    payload = response.json()
    # The detail is exactly the sanitized English it always was (byte for byte).
    expected = sanitize_exception(ValueError(f"Link 1 has no acquisition scope {scope!r}"), max_length=200)
    assert payload["detail"] == expected and set(payload) == {"detail", "code", "params"}
    # A credential-shaped scope is never interpolated: the explicit fallback, no params.
    assert payload["code"] == "unclassified" and payload["params"] == {}
    # No synthetic credential in any structured field, recognised by the sanitizer or not.
    structured = json.dumps({key: value for key, value in payload.items() if key != "detail"})
    assert not [secret for secret in _SECRETS + _UNRECOGNISED if secret in structured]
    # Sanitizer-recognised credentials are absent from the detail too; unrecognised ones
    # are in it exactly as before WS3e (recorded for the sanitizer's owner, not changed here).
    assert not [secret for secret in _SECRETS if secret in payload["detail"]]


@pytest.mark.asyncio
async def test_a_recognised_scope_keeps_its_code_and_free_form_text_falls_back():
    link = "https://files.example/item"
    # A scope DebridPulse defines (AcquisitionScope), refused because this link offers
    # only the item: its producer recognises it, so the code and params are kept.
    app = _links_app(choices=(("item", "Just this item"),))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        known_scope = await client.post("/api/links/add", json={"links": [link], "acquisition_scopes": {link: "collection"}})
    assert known_scope.status_code == 400
    assert known_scope.json() == {"detail": "Link 1 has no acquisition scope 'collection'",
                                  "code": "submit.link_scope_missing", "params": {"number": "1", "scope": "'collection'"}}
    # "folder" is not a DebridPulse scope: free-form caller text, never a parameter.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_links_app()), base_url="http://test") as client:
        free_form = await client.post("/api/links/add", json={"links": [link], "acquisition_scopes": {link: "folder"}})
    assert free_form.status_code == 400
    assert free_form.json() == {"detail": "Link 1 has no acquisition scope 'folder'", "code": "unclassified", "params": {}}


def test_a_nested_cause_and_an_innocent_name_disclose_nothing_the_detail_does_not():
    from core.localization import disclosed
    from core.logging_utils import sanitize_log_value

    def wrapped_refusal(reason):
        cause = CodedValueError(f"aria2 refused this configuration: {reason}", code="aria2.configuration_refused",
                                params={"reason": reason})
        wrapped = CodedValueError(f"{cause}. The previous configuration was kept.",
                                  code="integration.configuration_kept", params={"reason": said(cause)})
        detail = sanitize_log_value(str(wrapped), max_length=200)
        return detail, refusal(409, detail, wrapped)

    # A nested cause whose producer vouches for its text (a ``fact``) keeps both codes and its params.
    detail, refused = wrapped_refusal(fact("exit 28"))
    assert refused.detail == detail and refused.code == "integration.configuration_kept"
    assert refused.params == {"reason": {"$t": "error.aria2.configuration_refused", "$v": {"reason": "exit 28"}}}
    # The same words as service text (aria2's own) have no authority: the whole refusal falls back.
    detail, refused = wrapped_refusal("exit 28")
    assert refused.detail == detail and (refused.code, refused.params) == (UNCLASSIFIED, {})
    # A nested cause carrying a credential -- one the sanitizer redacts, or one it does not
    # recognise -- fails closed: the detail is unchanged, no structured field carries it.
    for reason in ("https://bob:SYNTHETIC-PASS-1@host.example/x?token=SYNTHETIC-TOKEN-3",
                   "https://host.example/rss?authkey=SYNTHETIC-ONLY-AUTHKEY-6"):
        detail, refused = wrapped_refusal(reason)
        assert refused.detail == detail and (refused.code, refused.params) == (UNCLASSIFIED, {})
    # A value the detail does not show is never sent, whatever the parameter is called.
    for name in ("scope", "reason", "link", "value"):
        assert disclosed("Backup could not be created.", "backup.create", {name: "SYNTHETIC-PASS-1"}) == (UNCLASSIFIED, {})
    # Numbers and booleans keep their types for ICU.
    assert disclosed("Search is limited to 200 characters.", "search.too_long", {"count": 200, "flag": True}) == (
        "search.too_long", {"count": 200, "flag": True})
    # Text is admitted by authority only: the same words, as a fact and as plain text.
    assert disclosed("Backup could not be created by tv", "a.b", {"who": fact("tv")}) == ("a.b", {"who": "tv"})
    assert disclosed("Backup could not be created by tv", "a.b", {"who": "tv"}) == (UNCLASSIFIED, {})


def test_an_aria2_size_refusal_names_its_field_by_identifier_not_by_its_english():
    from pydantic import ValidationError

    from api.routes import _validation_refusal
    from executors.aria2.definition import Aria2Options

    with pytest.raises(ValidationError) as refused:
        Aria2Options(min_split_size="5000M")
    refusal_ = _validation_refusal(refused.value.errors(include_url=False)[0])
    assert refusal_["message"] == ("Minimum split size must be a whole number with an optional K or M suffix "
                                   "(for example 64M), from 1M to 1024M")
    assert refusal_["code"] == "aria2.size_range"
    assert refusal_["params"] == {"field": {"$t": "error.aria2.field.min_split_size", "$v": {}},
                                  "lowest": "1M", "highest": "1024"}
    assert MESSAGES["error.aria2.field.min_split_size"] == "Minimum split size"
    for field in ("min_split_size", "disk_cache", "lowest_speed_limit"):
        assert f"error.aria2.field.{field}" in MESSAGES
