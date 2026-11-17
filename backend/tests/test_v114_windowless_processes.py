"""No program DebridPulse starts opens a console window on Windows.

The desktop application's backend has no console of its own, so a console
program it starts directly -- aria2, the Usenet service, rsync, the archive
tools -- would open a window on the person's desktop unless started with
``core.processes.WINDOWLESS`` (CREATE_NO_WINDOW). Every direct start in the
backend passes it; the exceptions are named with why.
"""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# Where a start may go without WINDOWLESS, and why.
EXEMPT = {
    # ProcessOwnership's gate: started DETACHED, and it starts the program with
    # CREATE_NO_WINDOW itself (_WINDOWS_GATE).
    "executors/process_ownership.py",
    # Runs inside the media worker's own owned (windowless) process, under its
    # sandbox's audit hook: its children share that hidden console.
    "executors/media/worker.py",
}
# The definition itself.
DEFINITION = "core/processes.py"
STARTS = {("asyncio", "create_subprocess_exec"), ("subprocess", "Popen"), ("subprocess", "run")}


def _starts(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and (node.func.value.id, node.func.attr) in STARTS:
            yield node


def _windowless(call: ast.Call, source: str) -> bool:
    if any(keyword.arg is None and ast.unparse(keyword.value) in {"WINDOWLESS", "kwargs"} for keyword in call.keywords):
        # ``**kwargs`` counts only where that dict is built from WINDOWLESS.
        return "WINDOWLESS" in source
    return any(keyword.arg == "creationflags" for keyword in call.keywords)


def test_every_program_the_backend_starts_directly_is_started_windowless():
    missing = []
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("tests/", ".venv/")) or relative in EXEMPT or relative == DEFINITION:
            continue
        source = path.read_text(encoding="utf-8")
        for call in _starts(ast.parse(source)):
            if not _windowless(call, source):
                missing.append(f"{relative}:{call.lineno}")
    assert missing == [], missing


def test_windowless_is_create_no_window_on_windows_and_nothing_elsewhere():
    from core.processes import WINDOWLESS
    if os.name == "nt":
        assert WINDOWLESS == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert WINDOWLESS == {}


def test_the_media_helpers_netless_module_still_loads_on_its_own():
    """The media helpers' wrappers import ``netless`` with only its own
    directory on the path (no backend packages): it must stay standalone --
    the regression in 0ba5f7cc, where it imported core.processes, broke every
    media finalization. Imported here exactly that way, in its own process."""
    import subprocess as sp
    import sys
    folder = BACKEND / "executors" / "media"
    probe = f"import sys; sys.path[:] = [{str(folder)!r}] + [p for p in sys.path if 'site-packages' not in p and p]; import netless"
    done = sp.run([sys.executable, "-I", "-S", "-c", probe], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    source = (folder / "netless.py").read_text(encoding="utf-8")
    assert "from core" not in source and "from executors" not in source
