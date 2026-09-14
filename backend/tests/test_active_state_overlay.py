"""Regression coverage for the bounded active-transfer browser overlay."""
from types import SimpleNamespace

import pytest

import application.service as service_module
from application.service import ApplicationService


def _transfer(identity, status, progress):
    return SimpleNamespace(id=identity, state=status, progress=progress)


class _Repository:
    def __init__(self, before, after):
        self._snapshots = (tuple(before), tuple(after))
        self.active_calls = 0
        self.artifact_calls = 0
        self.presentation_calls = 0

    async def active(self):
        index = min(self.active_calls, len(self._snapshots) - 1)
        self.active_calls += 1
        return self._snapshots[index]

    async def artifacts(self, _transfer_id):
        self.artifact_calls += 1
        return ()

    async def presentation(self, _transfer_id, **_kwargs):
        self.presentation_calls += 1
        raise AssertionError("periodic active-state publication must not build comprehensive presentation")


class _Engine:
    def __init__(self, repository):
        self.repository = repository
        self.dispatch_permitted = True
        self.reconcile_calls = 0

    async def reconcile_executions(self):
        self.reconcile_calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 25])
async def test_periodic_progress_is_one_batched_overlay_without_comprehensive_presentation(monkeypatch, count):
    before = [_transfer(identity, "downloading", identity) for identity in range(1, count + 1)]
    after = [_transfer(identity, "downloading", identity + 0.5) for identity in range(1, count + 1)]
    repository = _Repository(before, after)
    application = ApplicationService(_Engine(repository))
    published = []

    async def capture(name, data):
        published.append((name, data))

    monkeypatch.setattr(service_module, "publish", capture)

    await application.reconcile_executions()

    assert repository.active_calls == 2
    assert repository.presentation_calls == 0
    assert application.engine.reconcile_calls == 1
    assert [name for name, _data in published] == ["torrent_updated", "stats_changed"]
    payload = published[0][1]
    assert payload["progress_only"] is True
    assert len(payload["items"]) == count
    assert all(item["status"] == "downloading" for item in payload["items"])
    assert all(item["status_changed"] is False for item in payload["items"])


@pytest.mark.asyncio
async def test_active_overlay_marks_transitions_for_authoritative_lightweight_refresh(monkeypatch):
    before = [
        _transfer(11, "downloading", 30),
        _transfer(12, "queued", 0),
        _transfer(13, "downloading", 80),
    ]
    after = [
        _transfer(11, "downloading", 35),
        _transfer(12, "paused", 0),
    ]
    repository = _Repository(before, after)
    application = ApplicationService(_Engine(repository))
    published = []

    async def capture(name, data):
        published.append((name, data))

    monkeypatch.setattr(service_module, "publish", capture)

    await application.reconcile_executions()

    assert repository.presentation_calls == 0
    update = published[0]
    assert update[0] == "torrent_updated"
    items = {item["id"]: item for item in update[1]["items"]}
    assert items[11] == {"id": 11, "status": "downloading", "progress": 35.0, "status_changed": False}
    assert items[12] == {"id": 12, "status": "paused", "progress": 0.0, "status_changed": True}
    assert items[13]["status_changed"] is True
    assert [name for name, _data in published] == ["torrent_updated", "stats_changed"]


@pytest.mark.asyncio
async def test_unchanged_active_state_emits_no_browser_churn(monkeypatch):
    snapshot = [_transfer(21, "downloading", 50)]
    repository = _Repository(snapshot, snapshot)
    application = ApplicationService(_Engine(repository))
    published = []

    async def capture(name, data):
        published.append((name, data))

    monkeypatch.setattr(service_module, "publish", capture)

    await application.reconcile_executions()

    assert repository.presentation_calls == 0
    assert published == []


# --------------------------------------------------------------------------- #
# TASK_DebridPulse_1.0.12_File_Selection_Projection_and_NOW_Control_Corrections
# Section 10.3/14.2 -- ApplicationService.resolve_pending() must reuse the
# existing semantic _publish() for exactly the transfer ids the engine
# reports as having crossed the canonical manifest-commit boundary this
# cycle: never all active transfers, never a publish-all fallback, and the
# existing execution-wakeup behavior must be preserved regardless.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resolve_pending_publishes_targeted_semantic_updates_for_exactly_the_affected_transfers(monkeypatch):
    class _ResolvingEngine:
        def __init__(self, repository):
            self.repository = repository
            self.resolve_calls = 0

        async def resolve_pending(self):
            self.resolve_calls += 1
            return frozenset({5, 9})

    repository = _Repository([], [])
    application = ApplicationService(_ResolvingEngine(repository))
    published = []

    async def capture(transfer_id):
        published.append(transfer_id)

    monkeypatch.setattr(application, "_publish", capture)

    assert not application.execution_wakeup.is_set()
    await application.resolve_pending()

    assert application.engine.resolve_calls == 1
    assert sorted(published) == [5, 9]              # exactly the affected transfers, no others
    assert application.execution_wakeup.is_set()    # existing execution wake behavior preserved


@pytest.mark.asyncio
async def test_resolve_pending_publishes_nothing_when_no_transfer_crossed_the_boundary(monkeypatch):
    class _IdleEngine:
        def __init__(self, repository):
            self.repository = repository

        async def resolve_pending(self):
            return frozenset()

    repository = _Repository([], [])
    application = ApplicationService(_IdleEngine(repository))
    published = []

    async def capture(transfer_id):
        published.append(transfer_id)

    monkeypatch.setattr(application, "_publish", capture)
    await application.resolve_pending()

    assert published == []                          # no publish-all fallback, no event churn
    assert application.execution_wakeup.is_set()
