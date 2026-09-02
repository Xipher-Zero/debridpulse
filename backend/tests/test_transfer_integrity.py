"""Local possession and path reservation through the universal lifecycle."""
from dataclasses import replace
from pathlib import Path

import pytest

from test_universal_lifecycle import core, submit, failure
from transfers.errors import Category
from transfers.models import ResolutionResult, ResourceState, TransferState, TransferOutcome, OutcomeKind


@pytest.mark.asyncio
@pytest.mark.parametrize('condition,adopt', [('exact',True),('missing',False),('wrong_size',False),('unknown_size',False),('sidecar',False),('directory',False),('symlink',False)])
async def test_existing_payload_requires_verified_possession(core, condition, adopt, tmp_path):
    candidate = core.provider.candidate()
    if condition == 'unknown_size':
        candidate = replace(candidate, expected_bytes=0)
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE,(candidate,))]
    transfer = await submit(core)
    await core.engine.resolve_pending()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if condition == 'directory':
        target.mkdir()
    elif condition == 'symlink':
        outside = tmp_path / 'foreign'
        outside.write_bytes(b'done')
        target.symlink_to(outside)
    elif condition != 'missing':
        target.write_bytes(b'bad' if condition == 'wrong_size' else b'done')
    if condition == 'sidecar':
        Path(core.executor.resumable_paths(str(target))[0]).write_bytes(b'resume')
    await core.engine.reconcile_executions()
    result = await core.repository.get(transfer.id)
    if adopt:
        assert result.state == TransferState.COMPLETED
        assert core.executor.jobs == {}
    else:
        assert result.state != TransferState.COMPLETED
        if condition == 'symlink':
            assert not core.executor.jobs
            assert result.error.category == Category.PATH_POLICY_VIOLATION
        else:
            assert len(core.executor.jobs) == 1
            assert (await core.repository.artifacts(transfer.id))[0].target == str(target)


@pytest.mark.asyncio
@pytest.mark.parametrize('history,file_exists,suffix', [('deleted',False,False),('completed',False,False),('downloading',False,True),('deleted',True,True)])
async def test_new_transfer_reservation_uses_live_owners_and_actual_files(core, history, file_exists, suffix):
    first = await submit(core, 'first', 'same.bin')
    await core.engine.tick()
    first_artifact = (await core.repository.artifacts(first.id))[0]
    target = Path(first_artifact.target)
    if history == 'deleted':
        await core.engine.delete(first.id, remote=False)
    elif history == 'completed':
        core.executor.finish(first_artifact.execution)
        await core.engine.tick()
        target.unlink()
    if file_exists:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'done')
    second = await submit(core, 'second', 'same.bin')
    await core.engine.resolve_pending()
    second_artifact = (await core.repository.artifacts(second.id))[0]
    assert (second_artifact.target != first_artifact.target) is suffix


@pytest.mark.asyncio
@pytest.mark.parametrize('deleted', [False, True])
async def test_uncertain_execution_reserves_path_even_with_terminal_parent(core, deleted):
    from unittest.mock import AsyncMock
    first = await submit(core, 'first', 'same.bin')
    await core.engine.tick()
    artifact = (await core.repository.artifacts(first.id))[0]
    if deleted:
        core.executor.cancel = AsyncMock(return_value=TransferOutcome(OutcomeKind.FAILURE, failure()))
        await core.engine.delete(first.id, remote=False)
    else:
        core.executor.observe = AsyncMock(return_value=None)
        await core.engine.reconcile_executions()
        assert (await core.repository.get(first.id)).state == TransferState.FAILED
    second = await submit(core, 'second', 'same.bin')
    await core.engine.resolve_pending()
    assert (await core.repository.artifacts(second.id))[0].target != artifact.target
