"""DP 1.0.13 post-Usenet corrective pass, work item H.

"Submitted As" must report the canonical REQUEST KIND, not the submission
channel's historical assumption. Before NZB, an uploaded file was always a
torrent, so ``manual_file -> Torrent file`` happened to be true; a completed
NZB transfer then displayed "Torrent file".

Presentation separates the two facts and derives the kind from the already
durable canonical request records. No provider identity is consulted and no
second ``submitted_as`` field is persisted.
"""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
BACKEND = Path(__file__).resolve().parents[1]


# --- no second durable truth -----------------------------------------------

def test_no_submitted_as_column_or_field_is_introduced():
    for folder in ("db", "transfers", "api", "application"):
        for path in (BACKEND / folder).rglob("*.py"):
            assert "submitted_as" not in path.read_text(encoding="utf-8"), path


def test_the_canonical_request_kind_is_already_durable():
    """The root request payload is plain JSON carrying ``kind``; both
    projections already re-read it after a restart."""
    from transfers import codec
    from transfers.models import TransferRequest

    payload = codec.dump(TransferRequest("nzb", b"<nzb/>", name="posting.nzb"))
    assert json.loads(payload)["kind"] == "nzb"
    assert codec.request(codec.load(payload)).kind == "nzb"


# --- the neutral derived projection ----------------------------------------

def test_the_details_projection_derives_the_distinct_root_request_kinds():
    source = (BACKEND / "transfers" / "presentation_repository.py").read_text(encoding="utf-8")
    assert "request_kinds" in source
    assert "parent_id IS NULL" in source


def test_the_bounded_list_projection_derives_the_same_neutral_field_without_a_per_row_query():
    source = (BACKEND / "api" / "operational_downloads.py").read_text(encoding="utf-8")
    assert '"request_kinds"' in source
    assert "root_request_kinds AS (" in source
    # Derived inside the ONE bounded projection SQL, aggregated over the page.
    assert "json_extract(r.payload, '$.kind')" in source
    assert "GROUP BY r.transfer_id" in source
    # The BOUNDED path never falls back to the comprehensive Details
    # projection. (The explicit ``status=consolidated`` durable-history view is
    # a separate, pre-existing diagnostic that deliberately keeps comprehensive
    # presentation; it is not this list.)
    bounded = source[source.index("# Durable, page-global provider-enablement facts"):]
    assert "repository.presentation(" not in bounded


@pytest.mark.asyncio
async def test_details_reports_the_request_kind_of_an_uploaded_nzb(tmp_path):
    result = await _presentation_for(tmp_path, "nzb", b"<nzb/>", "posting.nzb")
    assert result["source"] == "manual_file"
    assert result["request_kinds"] == ["nzb"]


@pytest.mark.asyncio
async def test_details_reports_the_request_kind_of_an_uploaded_torrent(tmp_path):
    result = await _presentation_for(tmp_path, "torrent", b"d4:infod4:name4:teste", "a.torrent")
    assert result["source"] == "manual_file"
    assert result["request_kinds"] == ["torrent"]


@pytest.mark.asyncio
async def test_the_kind_survives_a_restart_because_it_is_read_from_durable_requests(tmp_path):
    first = await _presentation_for(tmp_path, "nzb", b"<nzb/>", "posting.nzb")
    second = await _presentation_for(tmp_path, "nzb", b"<nzb/>", "posting.nzb", reopen=True)
    assert first["request_kinds"] == second["request_kinds"] == ["nzb"]


async def _presentation_for(tmp_path, kind, payload, name, *, reopen=False):
    """Persist one transfer with a single root request and project it.

    ``reopen`` builds a brand-new repository over the same database file, which
    is what a restart leaves behind: nothing in memory, only durable rows.
    """
    import db.database as database
    from transfers.models import TransferRequest
    from transfers.recovery_repository import TransferRepository

    database.DB_PATH = tmp_path / "presentation.db"
    repository = TransferRepository()
    if not reopen:
        await database.init_db()
        await repository.initialize()
        transfer, _created = await repository.admit(
            (TransferRequest(kind, payload, name=name),), name=name, source="manual_file")
        _presentation_for.transfer_id = transfer.id
    return await repository.presentation(_presentation_for.transfer_id, details=True)


# --- the one frontend formatter --------------------------------------------

def _source_label():
    """The formatter and the uploaded-file kind map it consults."""
    start = APP_JS.index("const UPLOADED_FILE_LABELS")
    block = APP_JS[start:]
    return block[:block.index("\n}", block.index("function sourceLabel(")) + 2]


def test_the_formatter_separates_the_channel_from_the_request_kind():
    block = _source_label()
    assert "function sourceLabel(source, requestKinds" in APP_JS
    assert "Torrent file" in block and "NZB file" in block
    assert "Magnet link" in block and "Direct link" in block and "API" in block


def test_the_formatter_never_consults_provider_identity():
    """The label is a pure function of (submission channel, request kinds).

    ``alldebrid_existing`` / ``inventory`` are CHANNEL tokens -- the operator
    imported from provider inventory -- not a branch on who resolved the
    transfer. Nothing here may read a provider field off the transfer.
    """
    block = _source_label()
    assert re.search(r"\bsourceLabel\(source, requestKinds", APP_JS)
    for forbidden in ("current_provider", "delivering_provider", "provider_id",
                      "provider_name", "usenet", "sabnzbd", "general_http"):
        assert forbidden not in block, forbidden


def test_a_mixed_or_unknown_upload_gets_a_truthful_neutral_label():
    block = _source_label()
    assert "Uploaded file" in block


def test_both_surfaces_use_the_one_formatter_with_the_canonical_fact():
    details = APP_JS[APP_JS.index('<div class="dk">Submitted As</div>'):]
    details = details[:details.index("</div>", details.index("dv"))]
    assert "sourceLabel(t.source, t.request_kinds)" in details
    downloads = (STATIC / "ui-downloads.js").read_text(encoding="utf-8")
    assert "sourceLabel(t.source, t.request_kinds)" in downloads


def test_channel_only_aggregates_stop_asserting_an_uploaded_file_is_a_torrent():
    """Statistics and notifications group BY CHANNEL and have no request kind
    available, so they must name the channel truthfully rather than repeat the
    same legacy assumption."""
    statistics = (STATIC / "ui-statistics.js").read_text(encoding="utf-8")
    assert "manual_file: 'Uploaded File'" in statistics
    notifications = (BACKEND / "services" / "notifications.py").read_text(encoding="utf-8")
    assert '"manual_file":        "Uploaded file (UI)"' in notifications
