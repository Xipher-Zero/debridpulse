"""A refused settings value is answered in words, never as a validation dump.

Real validation errors from the real option, policy and Usenet server models,
the real request-validation handler, and the real refusal at the
integration-option route.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from api.routes import _refused, _validation_refusal
from integrations.usenet.definition import UsenetServer
from providers.general_webdav.definition import GeneralWebdavOptions
from providers.torbox.definition import TorBoxOptions
from transfers.settings import TransferSettings

RAW = ("validation error", "TorBoxOptions", "TransferSettings", "UsenetServer", "input_value", "input_type",
       "[type=", "pydantic", "http", "SECRET-VALUE")


def _first(model, **values) -> dict:
    with pytest.raises(ValidationError) as caught:
        model(**values)
    return caught.value.errors(include_url=False)[0]


def _clean(text: str) -> None:
    for raw in RAW:
        assert raw not in text, raw


@pytest.mark.parametrize("model, values, field, requirement", [
    (TorBoxOptions, {"rate_limit_per_minute": 500}, "rate_limit_per_minute", "must be 300 or less"),
    (TorBoxOptions, {"request_timeout_seconds": 1}, "request_timeout_seconds", "must be 5 or more"),
    (TorBoxOptions, {"request_timeout_seconds": "soon"}, "request_timeout_seconds", "must be a whole number"),
    (TransferSettings, {"max_concurrent_executions": 21}, "max_concurrent_executions", "must be 20 or less"),
    (UsenetServer, {"host": "news.example", "port": 70000}, "port", "must be 65535 or less"),
])
def test_a_broken_bound_names_its_rule_from_the_schema(model, values, field, requirement):
    refusal = _validation_refusal(_first(model, **values))
    assert refusal == {"message": f"Value {requirement}.", "requirement": requirement, "field": field}
    _clean(json.dumps(refusal))


def test_a_validators_own_sentence_is_the_message():
    refusal = _validation_refusal(_first(GeneralWebdavOptions, collection_scan_timeout_seconds=5))
    assert refusal == {"message": "A collection scan timeout is 0 (no limit) or 10 to 3600 seconds",
                       "field": "collection_scan_timeout_seconds"}


def test_a_rule_without_wording_is_not_valid_never_pydantic_prose():
    refusal = _validation_refusal({"type": "url_parsing", "loc": ("endpoint",), "msg": "Input should be a valid URL",
                                   "ctx": {"error": "relative URL"}})
    assert refusal == {"message": "Value is not valid.", "requirement": "is not valid", "field": "endpoint"}


def test_the_refusal_is_a_400_with_the_detail_in_the_log(caplog):
    with pytest.raises(ValidationError) as caught:
        TorBoxOptions(rate_limit_per_minute=500)
    with caplog.at_level(logging.INFO, logger="debridpulse.routes"):
        refused = _refused(caught.value)
    assert isinstance(refused, HTTPException) and refused.status_code == 400
    assert refused.detail["message"] == "Value must be 300 or less."
    # The full validation text is kept for diagnosis, in the log only.
    assert "less_than_equal" in caplog.text and "rate_limit_per_minute" in caplog.text


def test_a_request_body_refusal_words_every_value_without_echoing_it():
    from main import request_validation_error_handler

    class Body(BaseModel):
        password: int
        retries: int

    with pytest.raises(ValidationError) as caught:
        Body(password="SECRET-VALUE", retries=99.5)
    response = asyncio.run(request_validation_error_handler(None, RequestValidationError(caught.value.errors())))
    assert response.status_code == 422
    detail = json.loads(response.body)["detail"]
    assert detail == [
        {"type": "int_parsing", "loc": ["password"], "msg": "Value must be a whole number.",
         "requirement": "must be a whole number"},
        {"type": "int_from_float", "loc": ["retries"], "msg": "Value must be a whole number.",
         "requirement": "must be a whole number"},
    ]
    _clean(response.body.decode())


@pytest.mark.asyncio
async def test_the_integration_option_route_refuses_in_words(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import api.routes as routes
    from integrations.configuration_mutation import InvalidIntegrationOptions

    async def refuse(*_args, **_kwargs):
        try:
            TorBoxOptions(rate_limit_per_minute=500)
        except ValidationError as exc:
            raise InvalidIntegrationOptions(str(exc)) from exc

    monkeypatch.setattr("integrations.configuration_mutation.mutate_integration_configuration", refuse)
    monkeypatch.setattr(routes, "_integration_definition", lambda _application, _id: SimpleNamespace(id="torbox"))
    with pytest.raises(HTTPException) as caught:
        await routes.patch_integration_configuration(
            "torbox", routes.IntegrationConfigurationUpdate(options={"rate_limit_per_minute": 500}), application=None)
    assert caught.value.status_code == 400
    assert caught.value.detail == {"message": "Value must be 300 or less.", "requirement": "must be 300 or less",
                                   "field": "rate_limit_per_minute"}
