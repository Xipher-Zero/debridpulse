"""Completion authority, path normalization and compatible settings constraints."""
from dataclasses import replace

import pytest

from test_universal_lifecycle import core, submit, failure
from transfers.errors import Category
from transfers.models import TransferRequest, TransferState, ExecutionState


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected", [
    ("all_blocked", TransferState.COMPLETED),
    ("all_done", TransferState.COMPLETED),
    ("still_active", TransferState.TRANSFERRING),
    ("physical_error", TransferState.FAILED),
    ("repaired_history", TransferState.COMPLETED),
])
async def test_completion_uses_real_artifacts_and_observations(core, scenario, expected):
    transfer = await core.engine.submit((TransferRequest("parcel", "first", name="first.bin"), TransferRequest("parcel", "second", name="second.bin")))
    await core.engine.resolve_pending()
    artifacts = await core.repository.artifacts(transfer.id)
    if scenario == "all_blocked":
        for artifact in artifacts:
            await core.engine.select_artifact(transfer.id, artifact.id, selected=False)
    else:
        await core.engine.reconcile_executions()
        artifacts = await core.repository.artifacts(transfer.id)
        core.executor.finish(artifacts[0].execution)
        if scenario in {"all_done", "repaired_history"}:
            core.executor.finish(artifacts[1].execution)
        elif scenario == "physical_error":
            current = core.executor.jobs[artifacts[1].execution.attempt_id]
            core.executor.jobs[artifacts[1].execution.attempt_id] = replace(current, state=ExecutionState.FAILED, error=failure(Category.CONTENT_INVALID))
        if scenario == "repaired_history":
            await core.repository.state(transfer.id, TransferState.FAILED, error=failure())
    await core.engine.reconcile_executions()
    assert (await core.repository.get(transfer.id)).state == expected


class TestPathHelpers:
    def test_safe_name_strips_dangerous_chars(self):
        from transfers.filesystem import safe_name
        assert "/" not in safe_name("a/b/c")
        assert "\\" not in safe_name("a\\b")
        # After fix: leading dots are stripped so '..' cannot appear at the start
        result = safe_name("../etc/passwd")
        assert not result.startswith("..")
        assert result  # non-empty fallback

    def test_safe_name_strips_leading_dots(self):
        from transfers.filesystem import safe_name
        assert not safe_name("../evil").startswith("..")
        assert not safe_name("../../root").startswith("..")
        assert safe_name(".hidden_file") == "hidden_file"  # leading dot stripped

    def test_safe_name_normal_stays_intact(self):
        from transfers.filesystem import safe_name
        result = safe_name("My Movie (2024) [1080p]")
        assert "Movie" in result
        assert "(2024)" in result

    def test_safe_name_preserves_normal(self):
        from transfers.filesystem import safe_name
        result = safe_name("My Movie (2024) [1080p]")
        assert result  # non-empty
        assert len(result) <= 255


# ── full_sync restartable set ─────────────────────────────────────────────────
