"""How DownloadPulse starts a program directly.

On Windows the backend runs without a console of its own (the desktop
application's), so every console program it starts -- download engines,
services, archive tools -- would otherwise open a console window of its own on
the person's desktop. Each is started with ``WINDOWLESS``: a console
the program has, never shows. Elsewhere it adds nothing.

Programs started through ``executors.process_ownership.ProcessOwnership``
already are; this is for every program started directly.
"""
from __future__ import annotations

import os
import subprocess

WINDOWLESS: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
