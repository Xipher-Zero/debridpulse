"""Provider-neutral application metrics contract (DP 1.0.12 final audit).

``/api/metrics`` reports universal transfer and scheduler state, so every
metric lives in the ``debridpulse_`` namespace. The obsolete ``alldebrid_*``
application family is not published in parallel: v1.0.12 is unreleased, so no
compatibility shim keeps the old names alive.
"""
import re
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import api.routes as routes
import main


class _MetricsDb:
    async def fetchall(self, query, params=()):
        if "FROM torrents" in query:
            return [
                {"status": "queued", "c": 2},
                {"status": "downloading", "c": 1},
                {"status": "completed", "c": 4},
                {"status": "error", "c": 3},
            ]
        if "FROM download_files" in query:
            return [{"status": "pending", "c": 5}, {"status": "completed", "c": 9}]
        raise AssertionError(query)

    async def fetchone(self, query, params=()):
        assert "FROM torrents" in query
        return {"total": 123456}


@pytest.fixture
def scrape(monkeypatch):
    @asynccontextmanager
    async def fake_db():
        yield _MetricsDb()

    monkeypatch.setattr(routes, "get_db", fake_db)
    monkeypatch.setattr(routes, "_sse_subscribers", [object(), object()])

    async def go() -> str:
        return (await routes.prometheus_metrics()).body.decode()

    return go


EXPECTED_VALUES = {
    "debridpulse_transfers_total": 10,
    "debridpulse_active_downloads": 3,
    "debridpulse_completed_downloads": 4,
    "debridpulse_error_transfers": 3,
    "debridpulse_pending_files": 5,
    "debridpulse_sse_subscribers": 2,
    "debridpulse_downloaded_bytes_total": 123456,
}


@pytest.mark.asyncio
async def test_scrape_publishes_the_neutral_family_with_the_same_values(scrape):
    body = await scrape()
    for name, value in EXPECTED_VALUES.items():
        assert re.search(rf"^{name} {value}$", body, re.MULTILINE), name
        assert f"# TYPE {name} gauge" in body, name
    # Label cardinality is unchanged: one series per status, one label.
    for status, count in {"queued": 2, "downloading": 1, "completed": 4, "error": 3}.items():
        assert f'debridpulse_transfers_by_status{{status="{status}"}} {count}' in body


@pytest.mark.asyncio
async def test_scrape_no_longer_publishes_any_alldebrid_application_metric(scrape):
    body = await scrape()
    assert "alldebrid_" not in body
    for obsolete in (
        "alldebrid_torrents_total",
        "alldebrid_torrents_by_status",
        "alldebrid_active_downloads",
        "alldebrid_completed_downloads",
        "alldebrid_error_torrents",
        "alldebrid_pending_files",
        "alldebrid_sse_subscribers",
        "alldebrid_downloaded_bytes_total",
    ):
        assert obsolete not in body


@pytest.mark.asyncio
async def test_no_universal_help_text_claims_provider_or_executor_ownership(scrape):
    body = await scrape()
    helps = [line for line in body.splitlines() if line.startswith("# HELP ")]
    assert helps
    for line in helps:
        lowered = line.lower()
        assert "alldebrid" not in lowered, line
        assert "aria2" not in lowered, line
        assert "torrent" not in lowered, line


@pytest.mark.asyncio
async def test_every_metric_is_in_the_debridpulse_namespace(scrape):
    body = await scrape()
    names = {
        re.match(r"([A-Za-z_:][A-Za-z0-9_:]*)", line).group(1)
        for line in body.splitlines()
        if line and not line.startswith("#")
    }
    assert names and all(name.startswith("debridpulse_") for name in names), names


def test_scrape_endpoint_keeps_its_path_and_prometheus_text_format(monkeypatch):
    @asynccontextmanager
    async def fake_db():
        yield _MetricsDb()

    monkeypatch.setattr(routes, "get_db", fake_db)
    response = TestClient(main.app).get("/api/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "debridpulse_transfers_total" in response.text
