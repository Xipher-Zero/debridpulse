"""Permanent guards for the "two truths kept synchronized by hand" census.

Each assertion pins an instance the final-audit recurrence census found and
folded into its single owner, so the same mechanism cannot quietly return.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_browser_derives_no_transfer_status_beside_the_backend_presentation():
    app = read("app.js")
    # transferDisplayStatus is the effective presentation, or the raw status a
    # caller without a projection supplied -- nothing is derived from
    # extraction or source-failure fields.
    body = app.split("function transferDisplayStatus(t) {", 1)[1].split("\n}", 1)[0]
    assert "presentation_status" in body
    for derived in ("extraction_status", "source_failure_count", "_with_errors", "'extracting'"):
        assert derived not in body, derived
    # No SSE handler paints a row status; the toast is a notification only.
    assert "patchExtractionTransferEvent" not in app
    notify = app.split("function notifyExtractionFailure(data) {", 1)[1].split("\n}", 1)[0]
    assert "badge(" not in notify and "querySelector" not in notify
    # The progress-only patch styles the bar with the presentation status the
    # row was rendered with, never with the event's raw lifecycle state.
    patch = app.split("function patchProgressOnlyTransferEvent(data) {", 1)[1].split("\n}\n", 1)[0]
    assert "presentationStatus" in patch and "update?.status || ''" not in patch and "nextStatus" not in patch


def test_frontend_never_adjusts_a_server_derived_count_locally():
    app = read("app.js")
    assert not re.search(r"(?<!let )pausedTransferCount\s*=\s*(0\b|Math\.max\(0,\s*pausedTransferCount)", app)
    assert app.count("pausedTransferCount = Math.max(0, Number(bs.paused) || 0);") == 1


def test_details_files_rows_have_exactly_one_renderer():
    app = read("app.js")
    owner = read("ui-detail-candidates.js")
    # app.js provides the table shell and asks the owner for the rows -- and for
    # WHICH collection those rows come from (the canonical-object presentation
    # when the backend projects one, the physical collection otherwise), so the
    # card title and the rows can never disagree about what is on screen.
    assert "const dpDisplayFiles = window.DPDetailCandidates.displayRows(t);" in app
    assert "<tbody>${window.DPDetailCandidates.rowsMarkup(dpDisplayFiles)}</tbody>" in app
    assert "t.files.map(" not in app
    assert "t.file_presentations.map(" not in app
    assert "window.DPDetailCandidates = Object.freeze({rowsMarkup: rows, displayRows: displayRows});" in owner
    # The owner does not re-fetch or re-render what app.js just rendered from
    # the same payload; it only binds its own disclosures.
    handler = owner.split("function onDetailRendered(event) {", 1)[1].split("function onDetailClosed()", 1)[0]
    assert "fetchPresentation" not in handler and "render(" not in handler and "bindDisclosures" in handler
    # One fetch of the transfer; the settings document is the cached canonical one.
    fetcher = owner.split("async function fetchPresentation(", 1)[1].split("function queueRefresh", 1)[0]
    assert "'/settings'" not in fetcher


def test_the_canonical_owner_is_never_wrapped_or_proxied():
    composition = (ROOT / "backend" / "application" / "composition.py").read_text(encoding="utf-8")
    events = (ROOT / "backend" / "application" / "consolidation_events.py").read_text(encoding="utf-8")
    canonical = (ROOT / "backend" / "transfers" / "canonical.py").read_text(encoding="utf-8")
    assert "engine.canonical.on_attached = consolidation_events.stage" in composition
    assert "__getattr__" not in events and "__setattr__" not in events
    assert "self.on_attached" in canonical
    # The callback follows the durable commit, never a refusal or a rollback.
    attach = canonical.split("async def attach(", 1)[1].split("async def _bound_origin", 1)[0]
    assert attach.index("await db.commit()") < attach.index("await self.on_attached(")
