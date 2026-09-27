"""Direct-link submissions carry the caller's explicit selection intent.

The built-in browser is an interactive client and says so; an API/headless
caller that omits the field keeps the ALL default. Nothing is inferred from
the source type, provider, user agent or SSE connection."""
from __future__ import annotations

import pytest

import api.routes as routes
from test_v113_transfer_auth_context import lab  # noqa: F401

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self):
        self.calls = []

    async def submit_links(self, links, **kwargs):
        self.calls.append((list(links), kwargs))
        return {"ok": True, "id": 1}


async def test_the_links_route_threads_explicit_interactive_intent():
    app = _Recorder()
    await routes.add_debrid_links({"links": ["https://h.example/f"], "selection_mode": "interactive"}, app)
    assert app.calls == [(["https://h.example/f"], {"selection_mode": "interactive"})]


async def test_a_headless_caller_that_omits_intent_keeps_the_all_default():
    app = _Recorder()
    await routes.add_debrid_links({"links": ["https://h.example/f"]}, app)
    assert app.calls == [(["https://h.example/f"], {"selection_mode": None})]


@pytest.mark.parametrize("mode,expected", [("interactive", "interactive"), (None, "all"), ("all", "all")])
async def test_every_submitted_root_request_carries_the_normalized_intent(lab, mode, expected):
    from application.service import ApplicationService
    repository, _registry, engine, *_rest = lab
    service = ApplicationService(engine)
    result = await service.submit_links(["https://h.example/a.bin", "ftp://h.example/b/"], selection_mode=mode)
    records = await repository.requests(result["id"])
    assert [record.request.selection_mode for record in records] == [expected, expected]


async def test_an_unknown_intent_is_refused_like_every_other_submission(lab):
    from application.service import ApplicationService
    _repository, _registry, engine, *_rest = lab
    with pytest.raises(ValueError):
        await ApplicationService(engine).submit_links(["https://h.example/a.bin"], selection_mode="bogus")
