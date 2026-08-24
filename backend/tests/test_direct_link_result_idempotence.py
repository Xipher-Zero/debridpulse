from unittest.mock import AsyncMock

import pytest

from services.direct_link_result_guard import DirectLinkResultGuardManager


@pytest.mark.asyncio
async def test_completed_direct_link_parent_is_not_finalized_twice():
    manager = DirectLinkResultGuardManager()
    manager._normalize_direct_link_source_outcomes = AsyncMock(return_value=0)
    manager._direct_link_completion_state = AsyncMock(
        return_value=(
            {"status": "completed", "source": "direct_link", "name": "already done"},
            {
                "required_count": 1,
                "completed_count": 1,
                "error_count": 0,
                "active_count": 0,
                "completed_bytes": 123,
            },
        )
    )
    manager._mark_finished = AsyncMock()

    owned = await manager._complete_direct_link_result(77)

    assert owned is False
    manager._mark_finished.assert_not_awaited()
