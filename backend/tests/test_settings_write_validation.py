"""Settings Batch 1, Item 3: every Settings write boundary validates what it is
asked to CHANGE -- exactly, strictly and in words -- and leaves untouched
stored values alone.

The 18EGGS remediation made ``Aria2Options`` refuse instead of coerce. This
extends the same contract to every Settings write path
(``core.settings_constraints``):

* a changed value meets its field's rule strictly: ``"5"``, ``true``, ``5.0``,
  ``NaN``/``Infinity`` and exponent forms are never read as some other number,
  an unknown choice is never read as the default, and an out-of-range value is
  refused (400/422), never silently clamped;
* a stored value that a newer rule would refuse survives startup and every
  unrelated save untouched, and is re-judged only when an operator changes it;
* a refusal names the field and the rule, never the submitted value -- in the
  response and in the log.
"""
from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import core.config as config
from api import routes
from api.auth_config_routes import AuthenticationConfigUpdate
from core.config import AppSettings
from core.settings_constraints import validate_changes
from external.profiles import DeliveryProfile
from integrations.media.definition import MediaOptions
from integrations.usenet.definition import UsenetServer
from services.account_notifications import AccountNotificationUpdate

SECRET = "hooks-SECRET-9f3a"


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A real settings file and the real settings owner; only the running
    application around it is a double."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    (tmp_path / "dl").mkdir()

    def write(document: dict) -> AppSettings:
        path.write_text(json.dumps({"download_folder": str(tmp_path / "dl"), **document}))
        loaded = config.load_settings()
        config.apply_settings(loaded)
        return loaded

    write.path = path
    return write


def _world(stack: ExitStack):
    from test_settings_namespace_mutation import _application, _noop
    application = _application(config.get_settings())
    application.configuration_admission = lambda: _noop()
    stack.enter_context(patch.object(routes.aria2_runtime, "ensure_started",
                                     AsyncMock(return_value={"process_running": False})))
    stack.enter_context(patch("api.routes.journal_configuration_change", AsyncMock()))
    return application


async def _put(document: dict):
    with ExitStack() as stack:
        application = _world(stack)
        try:
            return await routes.update_settings(routes.SettingsUpdate.model_validate(document),
                                                application=application)
        except HTTPException as exc:
            return exc


def _echo(**changes) -> dict:
    """What the Settings page sends: the freshly read document, one value changed."""
    document = config.get_settings().model_dump()
    for name in ("integrations", "integration_groups", "transfer_policy", "execution_runtime_limits",
                 "external_delivery", "notification_verification"):
        document.pop(name, None)
    return {**document, **changes}


def _on_disk(path) -> dict:
    return json.loads(path.read_text())


# ── the whole-settings document ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,requirement", [
    ("full_sync_interval_minutes", 5000, "must be 1440 or less"),
    ("full_sync_interval_minutes", -1, "must be 0 or more"),
    ("backup_keep_days", 0, "must be 1 or more"),
    ("auth_session_lifetime_hours", 9999, "must be 168 or less"),
    ("extract_max_concurrent", 9, "must be 8 or less"),
    ("extract_max_concurrent", 0, "must be 1 or more"),
    ("update_check_interval_hours", 169, "must be 168 or less"),
    ("min_free_disk_gb", -0.5, "must be 0 or more"),
    ("min_free_disk_gb", float("nan"), "must be a finite number"),
    ("disk_guard_resume_hysteresis_gb", float("inf"), "must be a finite number"),
    ("extract_max_expanded_gb", float("nan"), "must be a finite number"),
    ("disk_guard_interval_seconds", 5, "must be 10 or more"),
    ("activity_log_page_size", 75, "must be 50, 100 or 250"),
    ("log_level", "LOUD", "must be DEBUG, INFO, WARNING, ERROR or CRITICAL"),
    ("backup_folder", "relative/backups", "must be an absolute folder path"),
    ("discord_webhook_url", f"ftp://example.invalid/{SECRET}", "must be an http:// or https:// address"),
    ("stats_report_webhook_url", f"https://example.invalid/{SECRET} x", "must be an http:// or https:// address"),
    ("discord_avatar_url", "https://cdn.example/avatar.SVG", "must be an http:// or https:// image address that is not an SVG"),
    ("discord_avatar_url", "data:image/png;base64,AAAA", "must be an http:// or https:// image address that is not an SVG"),
    ("discord_username", "x" * 81, "must be at most 80 characters"),
])
async def test_a_changed_document_value_outside_its_rule_is_refused_not_clamped(stored, field, value, requirement):
    stored({})
    before = _on_disk(stored.path)
    result = await _put(_echo(**{field: value}))
    assert isinstance(result, HTTPException) and result.status_code == 400
    assert result.detail["field"] == field and result.detail["requirement"] == requirement
    assert SECRET not in json.dumps(result.detail)
    assert _on_disk(stored.path) == before  # nothing was written, nothing clamped


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("full_sync_interval_minutes", 0), ("full_sync_interval_minutes", 1440), ("backup_keep_days", 365),
    ("extract_max_concurrent", 8), ("update_check_interval_hours", 0), ("min_free_disk_gb", 12.5),
    ("disk_guard_resume_hysteresis_gb", 0), ("activity_log_page_size", 250), ("log_level", "debug"),
    ("backup_folder", "/srv/backups/"), ("discord_webhook_url", "https://discord.com/api/webhooks/1/abc"),
    ("discord_avatar_url", "https://cdn.example/avatar.png?size=64"), ("discord_username", "x" * 80),
])
async def test_boundary_and_ordinary_values_are_accepted_exactly(stored, field, value):
    stored({})
    result = await _put(_echo(**{field: value}))
    assert not isinstance(result, HTTPException), getattr(result, "detail", None)
    saved = _on_disk(stored.path)
    secret = field in routes._SECRET_SETTINGS
    assert secret or saved[field] == value


@pytest.mark.parametrize("field,value", [
    ("full_sync_interval_minutes", "5"), ("full_sync_interval_minutes", True), ("full_sync_interval_minutes", 5.0),
    ("full_sync_interval_minutes", "18EGGS"), ("full_sync_interval_minutes", "1e3"),
    ("min_free_disk_gb", "1.5"), ("extract_enabled", "yes"), ("download_folder", 5),
])
def test_the_document_request_is_strict_about_json_types(field, value):
    """``PUT /settings``'s body model: a value of the wrong JSON type is a 422
    from the request boundary, never coerced into another value."""
    with pytest.raises(ValidationError) as caught:
        routes.SettingsUpdate.model_validate({field: value})
    assert caught.value.errors()[0]["loc"] == (field,)


@pytest.mark.asyncio
async def test_an_authentication_value_changed_through_the_document_meets_the_authentication_rule(stored):
    stored({})
    for field, value in (("public_base_url", "http://insecure.example"),
                         ("oidc_provider_name", "p" * 257),
                         ("auth_session_lifetime_hours", 0)):
        result = await _put(_echo(**{field: value}))
        assert isinstance(result, HTTPException) and result.status_code == 400, field
        assert result.detail["field"] == field
    assert _on_disk(stored.path).get("public_base_url", "") == ""
    accepted = await _put(_echo(public_base_url="https://dp.example"))
    assert not isinstance(accepted, HTTPException)
    assert _on_disk(stored.path)["public_base_url"] == "https://dp.example"


# ── legacy stored values ─────────────────────────────────────────────────────

LEGACY = {"extract_max_concurrent": 20, "update_check_interval_hours": 500, "min_free_disk_gb": -1.0,
          "disk_guard_interval_seconds": 3, "log_level": "verbose", "backup_folder": "relative-backups",
          "discord_username": "n" * 120}


@pytest.mark.asyncio
async def test_untouched_legacy_values_survive_startup_and_an_unrelated_save(stored):
    """A stored value a newer write rule would refuse: startup keeps it exactly,
    the page's whole-document save of an UNRELATED field keeps it exactly, and
    only changing that value meets the current rule."""
    loaded = stored(LEGACY)
    for field, value in LEGACY.items():
        assert getattr(loaded, field) == value, field

    result = await _put(_echo(backup_keep_days=30))
    assert not isinstance(result, HTTPException), getattr(result, "detail", None)
    saved = _on_disk(stored.path)
    assert saved["backup_keep_days"] == 30
    for field, value in LEGACY.items():
        assert saved[field] == value, field  # never migrated, clamped or rewritten

    # Changing one meets the rule; changing it to a valid value is accepted.
    refused = await _put(_echo(extract_max_concurrent=21))
    assert isinstance(refused, HTTPException) and refused.detail["field"] == "extract_max_concurrent"
    accepted = await _put(_echo(extract_max_concurrent=4))
    assert not isinstance(accepted, HTTPException)
    saved = _on_disk(stored.path)
    assert saved["extract_max_concurrent"] == 4 and saved["update_check_interval_hours"] == 500


def test_a_legacy_value_is_re_judged_only_when_it_changes():
    previous = AppSettings(extract_max_concurrent=20)
    validate_changes(AppSettings, {"extract_max_concurrent": 20}, previous)  # unchanged: not re-judged
    with pytest.raises(ValidationError):
        validate_changes(AppSettings, {"extract_max_concurrent": 21}, previous)
    # ``true`` is not the stored ``1``: it is a change, and refused as one.
    with pytest.raises(ValidationError):
        validate_changes(AppSettings, {"extract_max_concurrent": True}, AppSettings(extract_max_concurrent=1))


def test_the_load_correction_ranges_are_the_write_ranges():
    """One declaration per range: the bounds stored values are corrected to
    at load are exactly the write ``Range``s marked ``clamp_at_load`` -- and the
    ranges new in this batch are write-only."""
    from core.settings_constraints import load_clamped_ranges
    ranges = load_clamped_ranges(AppSettings)
    assert ranges["full_sync_interval_minutes"] == (0, 1440)
    assert ranges["auth_session_lifetime_hours"] == (1, 168)
    for write_only in ("extract_max_concurrent", "update_check_interval_hours", "min_free_disk_gb",
                       "disk_guard_resume_hysteresis_gb", "disk_guard_interval_seconds"):
        assert write_only not in ranges


# ── scoped namespaces ────────────────────────────────────────────────────────

async def _patch_integration(identity, body, current):
    from test_settings_namespace_mutation import _application
    application = _application(current)
    store = {"cfg": current}
    with patch("api.routes.get_settings", side_effect=lambda: store["cfg"]), \
         patch("api.routes.load_settings", side_effect=lambda: store["cfg"].model_copy(deep=True)), \
         patch("api.routes.save_settings", side_effect=lambda cfg: store.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"), \
         patch("api.routes.journal_configuration_change", AsyncMock(), create=True):
        try:
            await routes.patch_integration_configuration(
                identity, routes.IntegrationConfigurationUpdate.model_validate(body), application=application)
        except HTTPException as exc:
            return exc, store["cfg"]
    return None, store["cfg"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1e2, "100", True, "18EGGS", float("nan"), 601, -1])
async def test_an_integration_option_is_written_exactly_or_refused(value):
    from test_settings_namespace_mutation import _settings_with_full_integrations
    current = _settings_with_full_integrations()
    refused, after = await _patch_integration("alldebrid", {"options": {"rate_limit_per_minute": value}}, current)
    assert isinstance(refused, HTTPException) and refused.status_code == 400
    assert refused.detail["field"] == "rate_limit_per_minute"
    assert "rate_limit_per_minute" not in after.integrations["alldebrid"].options \
        or after.integrations["alldebrid"].options["rate_limit_per_minute"] != value


@pytest.mark.asyncio
async def test_an_unknown_secret_clear_is_a_refusal_not_a_server_error():
    from test_settings_namespace_mutation import _settings_with_full_integrations
    refused, _ = await _patch_integration("alldebrid", {"clear_secrets": ["not_a_secret"]},
                                          _settings_with_full_integrations())
    assert isinstance(refused, HTTPException) and refused.status_code == 400


def test_a_written_media_choice_is_exact_while_a_stored_unknown_one_still_loads():
    with pytest.raises(ValidationError):
        validate_changes(MediaOptions, {"video_codec": "18EGGS"}, MediaOptions())
    with pytest.raises(ValidationError):
        validate_changes(MediaOptions, {"video_quality": "HIGH "}, MediaOptions(video_quality="low"))
    validate_changes(MediaOptions, {"video_codec": "hevc"}, MediaOptions())
    # Load keeps its documented recovery for a stored value an older release wrote.
    assert MediaOptions(video_codec="mpeg2").video_codec == "auto"


@pytest.mark.parametrize("body", [
    {"graceful_stop_timeout_seconds": True}, {"graceful_stop_timeout_seconds": 5.0},
    {"graceful_stop_timeout_seconds": "5"}, {"private_lan_connections": "on"},
])
def test_the_transfer_policy_request_is_strict(body):
    with pytest.raises(ValidationError):
        routes.TransferPolicyUpdate.model_validate(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("value,requirement", [
    (5.9, "must be a whole number"), (True, "must be a whole number"), ("1048576", "must be a whole number"),
    (float("inf"), "must be a whole number"), (-5, "must be 0 or more"), (2 ** 53, f"must be {2 ** 53 - 1} or less"),
])
async def test_the_runtime_limit_is_written_exactly_or_refused(value, requirement):
    from test_settings_namespace_mutation import _application
    with pytest.raises(HTTPException) as caught:
        await routes.patch_execution_runtime_limits({"max_download_bytes_per_second": value},
                                                    application=_application(AppSettings()))
    assert caught.value.status_code == 400 and caught.value.detail["requirement"] == requirement


# ── Usenet servers, accounts, authentication, delivery profiles ─────────────

@pytest.mark.parametrize("host", ["news.example.com\nport=1", "nntp://news.example.com", "news.example.com/path",
                                  "user@news.example.com", "news example.com", "h" * 254])
def test_a_written_usenet_host_is_one_host_name(host):
    with pytest.raises(ValidationError) as caught:
        validate_changes(UsenetServer, {"host": host}, UsenetServer())
    assert caught.value.errors()[0]["type"] == "setting_host"


@pytest.mark.parametrize("host", ["news.example.com", "203.0.113.7", "::1", "[2001:db8::1]", ""])
def test_a_usenet_host_name_or_address_is_accepted(host):
    validate_changes(UsenetServer, {"host": host}, UsenetServer(host="other.example"))


def test_a_stored_usenet_host_is_not_re_judged_on_load():
    assert UsenetServer(host="legacy host value").host == "legacy host value"


@pytest.mark.parametrize("body", [{"port": "119"}, {"connections": 8.0}, {"ssl": "true"}, {"priority": True}])
def test_the_usenet_server_request_is_strict(body):
    with pytest.raises(ValidationError):
        routes.UsenetServerUpdate.model_validate(body)


@pytest.mark.parametrize("body", [{"stats_report_window_hours": "168"}, {"stats_report_interval_hours": 1.0},
                                  {"discord_notify_added": 1}])
def test_the_own_notifications_request_is_strict(body):
    with pytest.raises(ValidationError):
        AccountNotificationUpdate.model_validate(body)


@pytest.mark.parametrize("body", [{"auth_session_lifetime_hours": "12"}, {"auth_session_lifetime_hours": 12.5},
                                  {"oidc_allow_all": "yes"}])
def test_the_authentication_request_is_strict(body):
    with pytest.raises(ValidationError):
        AuthenticationConfigUpdate.model_validate(body)


@pytest.mark.parametrize("hours", ["24", 24.0, True])
def test_a_delivery_profile_retention_is_a_whole_number(hours):
    with pytest.raises(ValidationError):
        DeliveryProfile.model_validate({"id": "radarr", "categories": ["movies"], "clients": ["c"],
                                        "source_kinds": ["torrent"], "local_root": "/download/movies",
                                        "retention_hours": hours})


# ── no echo ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_refused_value_never_reaches_the_response_or_the_log(stored, caplog):
    stored({})
    caplog.set_level(logging.INFO)
    refused = await _put(_echo(stats_report_webhook_url=f"gopher://example.invalid/{SECRET}"))
    assert isinstance(refused, HTTPException)
    assert SECRET not in json.dumps(refused.detail)
    assert SECRET not in caplog.text
    assert "Settings value refused: stats_report_webhook_url: setting_url" in caplog.text
    from test_settings_namespace_mutation import _settings_with_full_integrations
    caplog.clear()
    await _patch_integration("alldebrid", {"options": {"rate_limit_per_minute": SECRET}},
                             _settings_with_full_integrations())
    assert SECRET not in caplog.text


# ── the OIDC verification proposal (a proven proposal is saved) ─────────────

@pytest.mark.parametrize("body", [{"oidc_provider_name": "p" * 257}, {"public_base_url": "u" * 2049},
                                  {"oidc_allow_all": "yes"}, {"oidc_group_claim": 5}])
def test_an_oidc_verification_proposal_meets_the_authentication_rules(body):
    from api.auth_config_routes import OidcVerificationRequest
    with pytest.raises(ValidationError):
        OidcVerificationRequest.model_validate(body)


@pytest.mark.asyncio
async def test_an_oidc_verification_proposal_needs_an_https_public_origin():
    from types import SimpleNamespace
    from starlette.requests import Request
    from api.auth_config_routes import OidcVerificationRequest, verify_pending_oidc_configuration
    request = Request({"type": "http", "method": "POST", "path": "/api/auth/oidc/verify-config", "headers": [],
                       "client": ("203.0.113.9", 1234), "state": {}})
    request.state.principal = SimpleNamespace(authenticated=True, account_id="admin")
    response = await verify_pending_oidc_configuration(
        request, OidcVerificationRequest(public_base_url="http://dp.example"))
    assert response.status_code == 400
    assert b"HTTPS origin" in response.body


@pytest.mark.parametrize("model,body", [
    ("AccountUpdate", {"enabled": "yes"}), ("AccountUpdate", {"enabled": 1}), ("AccountUpdate", {"role": "owner"}),
    ("AccountCreate", {"kind": "local", "username": 5}), ("AccountCreate", {"kind": "local", "username": "u" * 257}),
    ("PasswordReset", {"password": ""}), ("OwnPassword", {"current_password": "a", "new_password": "p" * 4097}),
    ("AccountMerge", {"target_id": "a", "source_ids": []}),
])
def test_users_and_access_and_my_account_requests_are_strict_and_bounded(model, body):
    import api.account_routes as account_routes
    with pytest.raises(ValidationError):
        getattr(account_routes, model).model_validate(body)


# ── Gate 9 correction A: an integration refusal never carries the input ─────

CREDENTIAL = "AD-KEY-7c1e-SECRET-0042"


@pytest.mark.asyncio
async def test_the_integration_mutation_refusal_carries_no_submitted_value(caplog):
    """The canonical mutation owner turns a schema refusal into
    ``InvalidIntegrationOptions`` without the submitted value (pydantic's own
    text includes it): neither the exception, the HTTP refusal nor the log
    carries a refused credential, while the refusal still names its field and
    rule."""
    from integrations.configuration_mutation import InvalidIntegrationOptions, mutate_integration_configuration
    from providers.alldebrid.definition import definition as alldebrid
    from test_settings_namespace_mutation import _application, _settings_with_full_integrations
    from integrations.configuration_mutation import SettingsStore
    from contextlib import asynccontextmanager

    current = _settings_with_full_integrations()

    @asynccontextmanager
    async def lock():
        yield

    store = SettingsStore(lambda: current, lambda: current.model_copy(deep=True), lambda cfg: None,
                          lambda cfg: None, lock)
    caplog.set_level(logging.DEBUG)
    for options in ({"api_key": {"token": CREDENTIAL}}, {"api_key": [CREDENTIAL]},
                    {"rate_limit_per_minute": CREDENTIAL}):
        with pytest.raises(InvalidIntegrationOptions) as caught:
            await mutate_integration_configuration(_application(current), alldebrid, options=options, store=store)
        assert CREDENTIAL not in str(caught.value) and CREDENTIAL not in repr(caught.value)
        assert isinstance(caught.value.__cause__, ValidationError)  # the route words the field from it
        refused, _ = await _patch_integration("alldebrid", {"options": options}, _settings_with_full_integrations())
        assert isinstance(refused, HTTPException) and refused.status_code == 400
        assert refused.detail["field"] == next(iter(options))
        assert CREDENTIAL not in json.dumps(refused.detail)
    assert CREDENTIAL not in caplog.text


@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_schema_refusal_is_not_turned_into_a_400(monkeypatch):
    """Only an expected validation refusal is an invalid option; a programming
    failure in the path propagates as itself."""
    import core.settings_constraints as constraints
    from integrations.configuration_mutation import SettingsStore, mutate_integration_configuration
    from providers.alldebrid.definition import definition as alldebrid
    from test_settings_namespace_mutation import _application, _settings_with_full_integrations
    from contextlib import asynccontextmanager

    def broken(*_args, **_kwargs):
        raise RuntimeError("a defect, not a refusal")

    monkeypatch.setattr(constraints, "validate_changes", broken)
    current = _settings_with_full_integrations()

    @asynccontextmanager
    async def lock():
        yield

    store = SettingsStore(lambda: current, lambda: current.model_copy(deep=True), lambda cfg: None,
                          lambda cfg: None, lock)
    with pytest.raises(RuntimeError, match="a defect"):
        await mutate_integration_configuration(_application(current), alldebrid,
                                               options={"rate_limit_per_minute": 10}, store=store)


# ── Gate 9 correction B: a value of another type is a change ────────────────

@pytest.mark.parametrize("submitted,stored", [
    (100.0, 100), (1.0, 1), (True, 1), (False, 0), (1, True), ("100", 100),
])
def test_a_differently_typed_value_equal_to_the_stored_one_is_a_change(submitted, stored):
    from providers.alldebrid.definition import AllDebridOptions
    field = "prepare_backup_torrents" if isinstance(stored, bool) else "rate_limit_per_minute"
    with pytest.raises(ValidationError):
        validate_changes(AllDebridOptions, {field: submitted}, {field: stored})


def test_an_identical_stored_value_is_still_not_re_judged():
    from providers.alldebrid.definition import AllDebridOptions
    # Untouched legacy values, exactly as stored (types included): not re-judged.
    validate_changes(AllDebridOptions, {"rate_limit_per_minute": 100}, {"rate_limit_per_minute": 100})
    validate_changes(AppSettings, {"extract_max_concurrent": 20, "min_free_disk_gb": -1.0},
                     AppSettings(extract_max_concurrent=20, min_free_disk_gb=-1.0))
    validate_changes(AppSettings, {"min_free_disk_gb": float("nan")}, AppSettings(min_free_disk_gb=float("nan")))
    # A float field written with a whole JSON number is a change, and a valid one.
    validate_changes(AppSettings, {"min_free_disk_gb": 2}, AppSettings(min_free_disk_gb=2.0))


def test_a_nested_value_of_another_type_is_a_change():
    from integrations.usenet.definition import UsenetOptions
    server = UsenetServer(id="s1", host="news.example.com", port=119).model_dump()
    stored = {"servers": [server]}
    validate_changes(UsenetOptions, {"servers": [dict(server)]}, stored)            # identical: not re-judged
    with pytest.raises(ValidationError):
        validate_changes(UsenetOptions, {"servers": [{**server, "port": 119.0}]}, stored)
    with pytest.raises(ValidationError):
        validate_changes(UsenetOptions, {"servers": [{**server, "ssl": 1}]}, {"servers": [{**server, "ssl": True}]})


@pytest.mark.asyncio
async def test_an_integration_option_resubmitted_as_a_float_is_refused_through_the_route():
    from test_settings_namespace_mutation import _settings_with_full_integrations
    current = _settings_with_full_integrations()
    current.integrations["alldebrid"].options["rate_limit_per_minute"] = 100
    refused, after = await _patch_integration("alldebrid", {"options": {"rate_limit_per_minute": 100.0}}, current)
    assert isinstance(refused, HTTPException) and refused.status_code == 400
    assert refused.detail == {"message": "Value must be a whole number.", "requirement": "must be a whole number",
                              "field": "rate_limit_per_minute"}
    accepted, after = await _patch_integration("alldebrid", {"options": {"rate_limit_per_minute": 100}}, current)
    assert accepted is None and after.integrations["alldebrid"].options["rate_limit_per_minute"] == 100
