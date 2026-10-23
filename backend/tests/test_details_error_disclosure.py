"""Details discloses the facts of the normalized error it displays.

GET /api/torrents/{id} carries ``error_disclosure`` -- the approved scalar
facts of the very error whose message Details shows, bound to it by identity,
already sanitized by NormalizedError -- and nothing else: never the free-form
``context``, never the trace-only ``diagnostic_evidence``, never on the list or
the event stream's presentation. Real temporary SQLite, real repository.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from api.routes import _error_disclosure, get_torrent
from integrations.catalog import definitions
from transfers import codec
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Permanence, Recovery, Retryability, Stage,
)
from transfers.recovery_repository import TransferRepository

# Transfer 627's durable error, as recorded.
REJECTED = NormalizedError(
    Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.CANDIDATE_PREPARATION, origin=Origin.PROVIDER,
    integration_id="realdebrid", native_code="35", diagnostic="infringing_file",
    permanence=Permanence.PERMANENT, retryability=Retryability.NEVER, recovery=Recovery.NONE,
)


@pytest_asyncio.fixture
async def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "details-error.sqlite3")
    await database.init_db()
    return TransferRepository()


def _store(transfer_id: int, error: NormalizedError | None, *, raw: str | None = None) -> None:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        conn.execute("INSERT INTO torrents(id,hash,name,status,source,normalized_error,error_message) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (transfer_id, f"hash-{transfer_id}", f"Transfer {transfer_id}", "error", "direct_link",
                      raw if raw is not None else (codec.dump(error) if error else None),
                      error.message if error else None))
        conn.commit()
    finally:
        conn.close()


async def _details(repository, transfer_id: int) -> dict:
    application = SimpleNamespace(repository=repository, definitions=definitions, engine=None)
    return await get_torrent(transfer_id, application=application)


@pytest.mark.asyncio
async def test_provider_rejection_discloses_its_provider_reason_and_native_code(repository):
    _store(627, REJECTED)
    details = await _details(repository, 627)
    assert details["error_message"] == "Candidate rejected"
    disclosure = details["error_disclosure"]
    assert disclosure["integration_id"] == "realdebrid"
    assert disclosure["integration_name"]  # the catalog's own label, never invented here
    assert (disclosure["origin"], disclosure["domain"], disclosure["diagnostic"], disclosure["native_code"]) == (
        "provider", "provider", "infringing_file", "35")
    assert (disclosure["stage"], disclosure["permanence"], disclosure["retryability"]) == (
        "candidate_preparation", "permanent", "never")
    # The category is the message itself; it is not repeated.
    assert "category" not in disclosure


@pytest.mark.asyncio
async def test_an_executor_and_a_core_error_use_the_same_disclosure(repository):
    executor = NormalizedError(Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION,
                               origin=Origin.EXECUTOR, integration_id="aria2", native_code="3",
                               diagnostic="Resource not found")
    core = NormalizedError(Domain.LOCAL_RESOURCE, Category.DOWNLOAD_STORAGE_UNAVAILABLE, Stage.EXECUTION,
                           origin=Origin.LOCAL_SYSTEM, permanence=Permanence.TEMPORARY,
                           retryability=Retryability.BACKOFF)
    _store(1, executor)
    _store(2, core)
    first, second = await _details(repository, 1), await _details(repository, 2)
    assert first["error_message"] == "Download failed"
    assert {key: first["error_disclosure"][key] for key in ("integration_id", "origin", "diagnostic", "native_code")} == {
        "integration_id": "aria2", "origin": "executor", "diagnostic": "Resource not found", "native_code": "3"}
    # Nothing attributes a local storage failure to any integration.
    assert second["error_disclosure"] == {"origin": "local_system", "domain": "local_resource", "stage": "execution",
                                          "permanence": "temporary", "retryability": "backoff"}


@pytest.mark.asyncio
async def test_missing_optional_facts_are_left_out_and_no_error_has_no_disclosure(repository):
    sparse = NormalizedError(Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.RESOLUTION, origin=Origin.PROVIDER)
    _store(3, sparse)
    _store(4, None)
    sparse_details, clean = await _details(repository, 3), await _details(repository, 4)
    assert sparse_details["error_disclosure"] == {"origin": "provider", "domain": "provider", "stage": "resolution"}
    assert "integration_id" not in sparse_details["error_disclosure"]
    assert clean["error_message"] is None and "error_disclosure" not in clean


def test_a_disclosure_is_given_only_for_the_error_displayed():
    shown = REJECTED.as_dict()
    assert _error_disclosure(REJECTED, shown, definitions)
    # A different (newer, older or foreign) error than the one displayed, a
    # missing displayed error or a missing error: the message alone.
    other = NormalizedError(Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.CANDIDATE_PREPARATION,
                            origin=Origin.PROVIDER, integration_id="torbox", diagnostic="other")
    assert _error_disclosure(other, shown, definitions) is None
    assert _error_disclosure(REJECTED, None, definitions) is None
    assert _error_disclosure(None, shown, definitions) is None


@pytest.mark.asyncio
async def test_hostile_reasons_reach_the_browser_only_as_sanitized_bounded_text(repository):
    hostile = NormalizedError(
        Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.RESOLUTION, origin=Origin.PROVIDER,
        integration_id="realdebrid", native_code="35\n<b>x</b>",
        diagnostic=("<img src=x onerror=alert(1)>\r\nsee https://cdn.example/f?token=SIGNEDSECRET "
                    "api_key=hunter2 ٣‮" + "x" * 900),
    )
    _store(5, hostile)
    disclosure = (await _details(repository, 5))["error_disclosure"]
    text = json.dumps(disclosure)
    for secret in ("SIGNEDSECRET", "hunter2", "https://", "\r"):
        assert secret not in disclosure["diagnostic"] and secret not in disclosure["native_code"], secret
    assert len(disclosure["diagnostic"]) <= 500 and len(disclosure["native_code"]) <= 128
    # Markup stays inert characters for the browser to print as text.
    assert "<img" in disclosure["diagnostic"] and "SIGNEDSECRET" not in text


@pytest.mark.asyncio
async def test_neither_context_nor_diagnostic_evidence_ever_reaches_details(repository):
    evidenced = NormalizedError(
        Domain.PROVIDER, Category.CANDIDATE_REJECTED, Stage.CANDIDATE_PREPARATION, origin=Origin.PROVIDER,
        integration_id="realdebrid", native_code="35", diagnostic="infringing_file",
        context={"context_canary": "CONTEXT-CANARY-VALUE"},
        diagnostic_evidence={"files": [{"name": "EVIDENCE-CANARY.mkv", "bytes": 123}]},
    )
    _store(6, evidenced)
    # The durable row does hold both, so their absence below is the projection's doing.
    conn = sqlite3.connect(database.DB_PATH)
    stored = conn.execute("SELECT normalized_error FROM torrents WHERE id=6").fetchone()[0]
    conn.close()
    assert "CONTEXT-CANARY-VALUE" in stored and "EVIDENCE-CANARY" in stored
    payload = json.dumps(await _details(repository, 6))
    for canary in ("CONTEXT-CANARY-VALUE", "context_canary", "EVIDENCE-CANARY", "diagnostic_evidence"):
        assert canary not in payload, canary
    assert '"diagnostic": "infringing_file"' in payload


def test_the_list_and_event_stream_presentation_carry_no_disclosure():
    # The disclosure is added by the Details route alone: the repository's
    # presentation (the list path and the event stream) never builds it.
    from pathlib import Path
    source = Path(__file__).parents[1].joinpath("transfers", "_repository_base.py").read_text()
    assert "error_disclosure" not in source
    routes = Path(__file__).parents[1].joinpath("api", "routes.py").read_text()
    assert routes.count('result["error_disclosure"]') == 1
    downloads = Path(__file__).parents[1].joinpath("api", "operational_downloads.py").read_text()
    assert "error_disclosure" not in downloads


# -- the reporting integration's own words for a failure ------------------------------
# Details' REASON (``error_reason``): declared by the integration for one native
# code (``IntegrationPresentation.error_reasons``), for the error Details shows or
# the failure DebridPulse is still recovering from, with only a real schedule.

def _challenge(stage=Stage.RESOLUTION, detail="Sign in to confirm you're not a bot. Use --cookies-from-browser "
               "or --cookies. See https://example.test/wiki?token=sekrit-value&sig=abcdef0123456789"):
    from integrations.media.outcomes import outcome_error
    return outcome_error("verification_challenge", stage, detail=detail)


CHALLENGE_WORDS = {
    "reason": "YouTube requested verification",
    "explanation": "YouTube requested additional verification during media extraction. This may be temporary; "
                   "an account requirement has not been established.",
}


@pytest.mark.asyncio
async def test_a_failed_verification_challenge_states_its_reason_beside_the_unchanged_error(repository):
    error = _challenge()
    _store(711, error)
    detail = await _details(repository, 711)
    assert detail["error_reason"] == {**CHALLENGE_WORDS, "scheduled_retry_at": None}
    # The error and its disclosure are exactly what they were: the category
    # message, and the native facts -- sanitized -- only in the disclosure.
    assert detail["error_message"] == "Source temporarily unavailable"
    facts = detail["error_disclosure"]
    assert facts["native_code"] == "verification_challenge" and facts["retryability"] == "backoff"
    assert "sekrit-value" not in json.dumps(detail) and "abcdef0123456789" not in json.dumps(detail)


@pytest.mark.asyncio
async def test_errors_without_declared_words_present_exactly_as_before(repository):
    from integrations.media.outcomes import outcome_error
    _store(712, outcome_error("auth_required", Stage.RESOLUTION, detail="This video is private"))
    _store(713, REJECTED)
    _store(714, None)
    for transfer_id in (712, 713, 714):
        assert "error_reason" not in await _details(repository, transfer_id)


class _Records:
    """The repository's own record readers, as fakes."""

    def __init__(self, requests=(), artifacts=()):
        self._requests, self._artifacts = tuple(requests), tuple(artifacts)

    async def requests(self, transfer_id):
        return self._requests

    async def artifacts(self, transfer_id):
        return self._artifacts


def _request(state, error=None, retry_at=0.0):
    return SimpleNamespace(state=state, error=error, retry_at=retry_at)


def _artifact(state, error=None):
    return SimpleNamespace(state=state, error=error)


@pytest.mark.asyncio
async def test_a_scheduled_resolution_retry_shows_the_reason_and_that_sources_real_schedule():
    from api.routes import _error_reason
    records = _Records(requests=(_request("resolved"), _request("pending", _challenge(), 1_900_000_000.5)))
    assert await _error_reason(records, 1, None, None, None, definitions) == {
        **CHALLENGE_WORDS, "scheduled_retry_at": 1_900_000_000.5}
    # A pending source with no scheduled retry is not a scheduled retry.
    records = _Records(requests=(_request("pending", _challenge(), 0.0),))
    assert await _error_reason(records, 1, None, None, None, definitions) is None


@pytest.mark.asyncio
async def test_a_held_acquisition_failure_reads_its_schedule_only_from_the_recovery_wake():
    from api.routes import _error_reason
    # The executor records its failures under its own identity (yt_dlp).
    from integrations.media.outcomes import outcome_error
    acquisition = outcome_error("verification_challenge", Stage.EXECUTION, integration_id="yt_dlp")
    records = _Records(artifacts=(_artifact("recovery_wait", acquisition),))
    waiting = await _error_reason(records, 1, None, None, "retry_at:1900000060.0", definitions)
    assert waiting == {**CHALLENGE_WORDS, "scheduled_retry_at": 1_900_000_060.0}
    # Held for the operator (budget spent): no automatic retry is claimed.
    for wake in ("operator_retry", None, "retry_at:not-a-time"):
        assert (await _error_reason(records, 1, None, None, wake, definitions))["scheduled_retry_at"] is None
    # A settled file's error is history, and a success leaves nothing to explain.
    for state in ("completed", "cancelled", "error"):
        assert await _error_reason(_Records(artifacts=(_artifact(state, _challenge()),)), 1, None, None,
                                   None, definitions) is None
    assert await _error_reason(_Records(requests=(_request("resolved"),), artifacts=(_artifact("completed"),)),
                               1, None, None, None, definitions) is None


@pytest.mark.asyncio
async def test_a_displayed_error_is_explained_only_when_it_is_that_same_error():
    from api.routes import _error_reason
    error = _challenge()
    transfer = SimpleNamespace(error=error)
    records = _Records(requests=(_request("pending", _challenge(), 1_900_000_000.0),))
    assert (await _error_reason(records, 1, transfer, error.as_dict(), None, definitions))["scheduled_retry_at"] is None
    # A transition between the two reads fails closed, never to another failure's words.
    assert await _error_reason(records, 1, SimpleNamespace(error=REJECTED), error.as_dict(), None, definitions) is None


@pytest.mark.asyncio
async def test_nothing_more_is_read_when_no_integration_declares_words():
    from api.routes import _error_reason

    class Unreadable:
        async def requests(self, transfer_id):
            raise AssertionError("read")

        artifacts = requests

    assert await _error_reason(Unreadable(), 1, None, None, None, []) is None
    assert await _error_reason(Unreadable(), 1, None, None, None,
                               [item for item in definitions if not item.presentation.error_reasons]) is None
