"""Details' Original Resource: the complete safe URL, and Copy URL only when it
is proved to be the submitted URL unchanged.

The canaries below are inert fake values; every test asserts their absence
from what reaches the browser."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

import db.database as database
from types import SimpleNamespace

from api.routes import _original_resource_url, _public_transfer_presentation, _safe_original_resource, get_torrent
from application.observability import Observability
from backup_support import restore_point_database
from services import event_bus
from transfers._repository_base import REQUEST_ADMITTED_VERBATIM
from integrations.catalog import definitions
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

CANARY = "CANARY-7f3e-not-a-secret"
LONG_PATH = "/".join(["a-deliberately-long-release-directory"] * 6 + ["Release.Name.2026.part01.rar"])


def _request(kind, payload, name="fixture"):
    return {"kind": kind, "payload": payload, "name": name, "fingerprint": "", "preferred_provider": None}


def _details(request, *, verbatim=True, **extra):
    return _public_transfer_presentation({"id": 1, "status": "downloading", "request": request, **extra},
                                         definitions, admitted_verbatim=verbatim)


# ── Safe unchanged URL ──────────────────────────────────────────────────────

def test_an_unchanged_safe_url_beyond_the_compact_label_is_complete_and_copyable():
    raw = f"https://files.example/{LONG_PATH}"
    assert len(raw) > 180
    result = _details(_request("https", raw))
    assert "…" in result["original_resource"] and len(result["original_resource"]) <= 180
    assert result["original_resource_sanitized_full"] == raw
    assert result["original_resource_copy_allowed"] is True
    assert "original_resource_copy_value" not in result


def test_percent_encoding_and_an_explicit_port_are_kept_byte_for_byte():
    raw = "https://files.example:8443/dir%20name/%E2%9C%93/file%2B1.bin"
    assert _original_resource_url(_request("https", raw), True) == (raw, True)


# ── Security sanitization: shown sanitized, never copyable ─────────────────

@pytest.mark.parametrize("raw, shown", [
    (f"https://files.example/{LONG_PATH}?token={CANARY}", f"https://files.example/{LONG_PATH}?…"),
    (f"https://files.example/a.bin?passkey={CANARY}&x=1", "https://files.example/a.bin?…"),
    (f"https://files.example/a.bin?{CANARY}", "https://files.example/a.bin?…"),
    (f"https://files.example/a.bin#{CANARY}", "https://files.example/a.bin"),
    (f"https://user:{CANARY}@files.example/a.bin", "https://files.example/a.bin"),
    (f"https://{CANARY}@files.example/a.bin", "https://files.example/a.bin"),
    (f"https://files.example@{CANARY.lower()}.example/a.bin", f"https://{CANARY.lower()}.example/a.bin"),
])
def test_any_removed_component_shows_only_the_sanitized_url_and_denies_copy(raw, shown):
    result = _details(_request("https", raw))
    assert result["original_resource_sanitized_full"] == shown
    assert result["original_resource_copy_allowed"] is False
    serialized = json.dumps(result)
    for secret in (CANARY, "x=1"):
        if secret.lower() not in shown.lower():
            assert secret not in serialized


def test_display_shortening_is_not_redaction_and_redaction_is_not_shortening():
    long_redacted = f"https://files.example/{LONG_PATH}?token={CANARY}"
    full, allowed = _original_resource_url(_request("https", long_redacted), True)
    assert full == f"https://files.example/{LONG_PATH}?…" and "…" not in full[:-1]
    assert allowed is False
    compact = _safe_original_resource(_request("https", long_redacted))
    assert compact != full and len(compact) <= 180


# ── Provenance: redaction before persistence, legacy, other paths ──────────

def test_a_request_without_the_admission_attestation_is_never_copyable():
    raw = "https://files.example/a.bin"
    result = _details(_request("https", raw), verbatim=False)
    assert result["original_resource_sanitized_full"] == raw   # still shown in full
    assert result["original_resource_copy_allowed"] is False
    assert _original_resource_url(_request("https", raw)) == (raw, False)       # the default is no proof
    assert _original_resource_url(_request("https", raw), "true") == (raw, False)
    # The request representation carries no attestation of its own.
    assert "admitted_verbatim" not in codec.dump(TransferRequest("https", raw, "a.bin"))


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "copy-eligibility.sqlite3")
    await database.init_db()
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0))
    await engine.initialize()
    return engine


async def _route(repository, transfer_id):
    """The Details endpoint itself, as the browser reaches it."""
    application = SimpleNamespace(repository=repository, definitions=definitions, engine=None)
    return await get_torrent(transfer_id, application=application)


async def _submit(engine, *raws, groups=None, deduplicate=False):
    requests = tuple(TransferRequest("https", raw, f"r{index}.bin") for index, raw in enumerate(raws))
    extra = {"alternative_groups": groups} if groups else {}
    transfer = await engine.submit(requests, deduplicate=deduplicate, **extra)
    return transfer.id


async def _attestations():
    async with database.get_db() as db:
        return [dict(row) for row in await db.fetchall(
            "SELECT transfer_id, detail, claimed FROM application_events WHERE kind=? ORDER BY id",
            (REQUEST_ADMITTED_VERBATIM,))]


async def _roots(transfer_id):
    async with database.get_db() as db:
        return [dict(row) for row in await db.fetchall(
            "SELECT id, payload FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL ORDER BY ordinal,id",
            (transfer_id,))]


@pytest.mark.asyncio
async def test_the_admission_boundary_attests_an_unchanged_submission(engine):
    raw = f"https://files.example/{LONG_PATH}"
    transfer_id = await _submit(engine, raw)
    root = (await _roots(transfer_id))[0]
    # One claimed, non-secret fact naming exactly that root; the request JSON is unchanged.
    assert await _attestations() == [{"transfer_id": transfer_id, "detail": root["id"], "claimed": 1}]
    assert codec.load(root["payload"]) == codec.load(codec.dump(TransferRequest("https", raw, "r0.bin")))
    result = await _route(engine.repository, transfer_id)
    assert result["original_resource_sanitized_full"] == raw
    assert result["original_resource_copy_allowed"] is True


@pytest.mark.asyncio
async def test_credentials_split_out_before_persistence_deny_copy_of_the_clean_looking_remainder(engine):
    transfer_id = await _submit(engine, f"https://user:{CANARY}@files.example/a.bin")
    # What is durable now looks exactly like a safe unchanged URL ...
    root = (await _roots(transfer_id))[0]
    assert codec.load(root["payload"])["payload"] == "https://files.example/a.bin"
    assert CANARY not in root["payload"]
    # ... but it is not what was submitted, and nothing attests that it is.
    assert await _attestations() == []
    result = await _route(engine.repository, transfer_id)
    assert result["original_resource_sanitized_full"] == "https://files.example/a.bin"
    assert result["original_resource_copy_allowed"] is False
    assert CANARY not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_signed_query_is_persisted_for_the_provider_but_never_reaches_details(engine):
    transfer_id = await _submit(engine, f"https://files.example/a.bin?sig={CANARY}")
    result = await _route(engine.repository, transfer_id)
    assert result["original_resource_sanitized_full"] == "https://files.example/a.bin?…"
    assert result["original_resource_copy_allowed"] is False
    assert CANARY not in json.dumps(result)


@pytest.mark.asyncio
async def test_each_root_is_attested_on_its_own_and_details_reads_only_the_root_it_shows(engine):
    # Alternative sources: the presented first root carried credentials, a later one did not.
    transfer_id = await _submit(engine, f"https://user:{CANARY}@a.example/x.bin", "https://b.example/x.bin",
                                groups=(1, 1))
    roots = await _roots(transfer_id)
    assert [row["detail"] for row in await _attestations()] == [roots[1]["id"]]
    assert (await _route(engine.repository, transfer_id))["original_resource_copy_allowed"] is False
    # And the reverse: the presented root is attested.
    second = await _submit(engine, "https://b.example/y.bin", f"https://user:{CANARY}@a.example/y.bin")
    assert (await _route(engine.repository, second))["original_resource_copy_allowed"] is True


@pytest.mark.asyncio
async def test_unproven_admission_paths_and_legacy_rows_are_never_copyable(engine):
    raw = "https://files.example/legacy.bin"
    # Any admission that is not the boundary's (no ``verbatim``) proves nothing.
    direct, _created = await engine.repository.admit((TransferRequest("https", raw, "a.bin"),), name="direct",
                                                     deduplicate=False)
    # A deduplicated resubmission joins existing work without new proof.
    first = await _submit(engine, f"https://user:{CANARY}@files.example/same.bin", deduplicate=True)
    assert await _submit(engine, "https://files.example/same.bin", deduplicate=True) == first
    assert await _attestations() == []
    for transfer_id in (direct.id, first):
        assert (await _route(engine.repository, transfer_id))["original_resource_copy_allowed"] is False
    # A forged attestation for another transfer's root, or a non-root, is not this root's.
    other = await _submit(engine, "https://files.example/other.bin")
    other_root = (await _roots(other))[0]["id"]
    async with database.get_db() as db:
        await db.execute("INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,1)",
                         (direct.id, REQUEST_ADMITTED_VERBATIM, other_root))
        await db.commit()
    assert (await _route(engine.repository, direct.id))["original_resource_copy_allowed"] is False


@pytest.mark.asyncio
async def test_the_attestation_survives_restart_and_is_never_delivered_or_published(engine, monkeypatch):
    transfer_id = await _submit(engine, "https://files.example/kept.bin")
    # Restart: a new repository over the same database.
    restarted = TransferRepository()
    assert await restarted.first_root_admitted_verbatim(transfer_id) is True
    assert (await _route(restarted, transfer_id))["original_resource_copy_allowed"] is True
    # Claimed: the observability queue never sees it, and no published event names it.
    pending = await restarted.pending_events()
    assert REQUEST_ADMITTED_VERBATIM not in {event["kind"] for event in pending}
    published = []

    async def publisher(kind, data):
        published.append(json.dumps(data, default=str))
    monkeypatch.setattr(event_bus, "_publisher", publisher)
    await Observability(restarted).deliver()
    assert published and not any(REQUEST_ADMITTED_VERBATIM in body or "admitted_verbatim" in body for body in published)


@pytest.mark.asyncio
async def test_a_restore_point_carries_the_attestation_into_the_restored_database(engine, tmp_path, monkeypatch):
    attested = await _submit(engine, "https://files.example/backed-up.bin")
    unattested = await _submit(engine, f"https://user:{CANARY}@files.example/backed-up.bin")
    member = await restore_point_database(tmp_path, monkeypatch)
    # The restored state is that database: reopened, it keeps both verdicts.
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(member.read_bytes())
    monkeypatch.setattr(database, "DB_PATH", restored)
    await database.init_db()
    repository = TransferRepository()
    assert (await _route(repository, attested))["original_resource_copy_allowed"] is True
    assert (await _route(repository, unattested))["original_resource_copy_allowed"] is False


@pytest.mark.asyncio
async def test_a_database_wipe_removes_the_attestation_with_its_transfers(engine):
    from services.db_maintenance import wipe_database
    transfer_id = await _submit(engine, "https://files.example/wiped.bin")
    assert await _attestations()
    await wipe_database(verified_quiesced=True)
    assert await _attestations() == []
    assert await engine.repository.first_root_admitted_verbatim(transfer_id) is False


# ── The submitted original, never a resolved or failover artifact ──────────

def test_resolved_and_failover_urls_never_replace_or_widen_the_original():
    raw = "https://source.example/original/file.rar"
    result = _details(
        _request("https", raw),
        files=[{"id": 1, "source_url": f"https://cdn.example/unlocked/{CANARY}",
                "download_url": f"https://mirror.example/{CANARY}", "candidates": [{"endpoints": []}]}],
        route_attempts=[{"provider_id": "alldebrid"}],
    )
    assert result["original_resource_sanitized_full"] == raw
    assert result["original_resource_copy_allowed"] is True
    assert CANARY not in json.dumps(result)


# ── Unsupported, ambiguous, normalized or active input ─────────────────────

@pytest.mark.parametrize("kind, raw", [
    ("https", "HTTPS://files.example/a.bin"),            # scheme case-folded
    ("https", "https://Files.Example/a.bin"),            # host lower-cased
    ("https", "https://files.example"),                  # empty path becomes "/"
    ("https", "https://files.example/a.bin?"),           # empty query dropped
    ("https", "https://files.example/a.bin#"),           # empty fragment dropped
    ("https", " https://files.example/a.bin"),           # surrounding whitespace
    ("https", "https://@files.example/a.bin"),           # empty userinfo
    ("https", "https://bücher.example/a.bin"),           # non-ASCII host
    ("https", "https://xn--bcher-kva.example\\@evil.example/a.bin"),
    ("https", "https://[2001:db8::1]/a.bin"),            # literal not a plain host
    ("https", "https://files.example/a\nb.bin"),         # control character
    ("https", "javascript:alert(1)//files.example/a.bin"),
    ("https", "data:text/html,<script>alert(1)</script>"),
    ("ftp", "ftp://files.example/a.bin"),                 # a remote-file source, not a web URL
    ("sftp", "sftp://files.example/a.bin"),
    ("webdavs", "webdavs://files.example/a.bin"),
    ("magnet", "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"),
    ("torrent", "files.example/a.torrent"),
])
def test_anything_the_sanitizer_would_alter_or_does_not_present_as_a_web_url_is_never_copyable(kind, raw):
    full, allowed = _original_resource_url(_request(kind, raw), True)
    assert allowed is False
    result = _details(_request(kind, raw))
    assert result["original_resource_copy_allowed"] is False
    if kind in {"magnet", "torrent"}:
        assert "original_resource_sanitized_full" not in result


def test_remote_file_sources_show_their_complete_sanitized_location_only():
    raw = f"sftp://user:{CANARY}@host.example:2222/{LONG_PATH}"
    result = _details(_request("sftp", raw))
    assert result["original_resource_sanitized_full"] == f"sftp://host.example:2222/{LONG_PATH}"
    assert result["original_resource_copy_allowed"] is False
    assert CANARY not in json.dumps(result)


def test_bytes_and_staged_payloads_are_never_urls():
    uploaded = {"kind": "https", "payload": {"$bytes": "aHR0cHM6Ly9maWxlcy5leGFtcGxlL2EuYmlu"}, "name": "a.bin",
                "fingerprint": "", "preferred_provider": None}
    assert _original_resource_url(uploaded, True) == (None, False)


# ── No Details context, no new fields ──────────────────────────────────────

def test_without_the_durable_request_no_resource_field_is_projected():
    result = _public_transfer_presentation({"id": 1, "status": "downloading"}, definitions)
    assert not [key for key in result if key.startswith("original_resource")]
    assert _original_resource_url(None, True) == (None, False)
    assert _original_resource_url({"kind": "https"}, True) == (None, False)


def test_details_stays_behind_the_existing_authentication_boundary():
    from auth.policy import is_public_path
    assert not is_public_path("/api/torrents/1")
    assert not is_public_path("/api/torrents/1/trace")
