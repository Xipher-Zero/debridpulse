"""1.0.13: NNTP servers are executor-internal acquisition mechanics.

Gate 1 proved against a real SABnzbd 5.1.3 that a dead priority-0 server plus a
working priority-1 server completes ONE job with ONE nzo_id and ONE history
entry. No NNTP server may ever surface as a DP candidate or failover target.
"""
from __future__ import annotations

import pytest

from transfers.models import ExecutionState

from sab_fakes import FakeSab  # noqa: F401
from test_v113_sabnzbd_executor import lab, request_for  # noqa: F401


@pytest.mark.asyncio
async def test_server_failover_does_not_create_a_second_execution_or_identity(lab):
    sab, executor = lab.sab, lab.executor
    sab.servers = {"dead": {"priority": 0, "enable": 1},
                   "live": {"priority": 1, "enable": 1}}
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    nzo = bound.native["nzo_id"]

    # SAB internally switches servers; DP sees one job throughout.
    sab.servers["dead"]["enable"] = 0
    observation = (await executor.observe_many((bound,))).observations[0]
    assert observation.handle.native["nzo_id"] == nzo
    assert len(sab.queue) == 1
    assert len(sab.submissions) == 1

    sab.finish(nzo)
    observation = (await executor.observe_many((bound,))).observations[0]
    assert observation.state == ExecutionState.SUCCEEDED
    assert observation.handle.native["nzo_id"] == nzo


@pytest.mark.asyncio
async def test_no_nntp_server_identity_leaks_into_neutral_observation(lab):
    sab, executor = lab.sab, lab.executor
    sab.servers = {"news.secret.example": {"priority": 0, "enable": 1,
                                           "username": "u", "password": "p"}}
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    observation = (await executor.observe_many((bound,))).observations[0]
    blob = (repr(observation.handle.correlation) + repr(observation.handle.native)
            + repr(observation.error) + repr(observation.progress))
    for secret in ("news.secret.example", "password", "p"):
        assert secret not in blob or secret == "p"
    assert "news.secret.example" not in blob


def test_nntp_connections_and_priority_are_executor_configuration_only():
    """They are not core routing inputs: nothing in transfers/ mentions them."""
    import pathlib
    core_dir = pathlib.Path(__file__).resolve().parents[1] / "transfers"
    for path in core_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for token in ("nntp", "nzo_id", "sabnzbd", "newsgroup"):
            assert token not in text, f"{path.name} leaks {token}"
