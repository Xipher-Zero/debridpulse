from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _FakeAria2:
    def __init__(self):
        self.tell_status = AsyncMock()
        self._call = AsyncMock()


