"""1.0.13 WS5d SEC-005: a finalization helper is read as it runs and never waited for without limit.

``worker._run`` ran the lossless finalizer (FFmpeg) with
``subprocess.run(..., stderr=PIPE)``: every byte the helper wrote to stderr
was held in memory until it exited -- only the last ``MAX_DETAIL_CHARS`` were
ever reported -- and nothing bounded how long it could run. Its stderr is now
read as it comes, keeping only the tail a failure reports, and a helper whose
output stands still for ``FINALIZATION_STALL_SECONDS`` is ended.

Real FFmpeg bulk stderr was not reproduced (audit): the bulk case uses the
proven stand-in helper; the ordinary real-FFmpeg good and bad cases run the
confined finalizer on the shipped fixtures.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from executors.media import worker

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "media"

# 128 MiB of stderr, then a recognisable last line, then a failing exit.
BULK = ("import sys\nchunk = b'E' * (1 << 20)\nfor _ in range(128):\n    sys.stderr.buffer.write(chunk)\n"
        "sys.stderr.buffer.write(b'\\nthe last thing it said')\nsys.exit(1)")


def _child(script: str, timeout: float) -> dict:
    """Run ``script`` in a fresh interpreter (its own memory, its own hang)."""
    completed = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, capture_output=True, text=True,
                               timeout=timeout)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_bulk_helper_stderr_is_never_held_in_memory_and_its_tail_is_still_reported():
    found = _child(f"""
import json, sys, tracemalloc
from executors.media import worker
tracemalloc.start()
try:
    worker._run([sys.executable, "-c", {BULK!r}])
    outcome = None
except worker.Failure as failure:
    outcome = [failure.code, failure.detail]
print(json.dumps({{"peak": tracemalloc.get_traced_memory()[1], "outcome": outcome}}))
""", timeout=120)
    code, detail = found["outcome"]
    assert code == "finalization_failed"
    assert detail.endswith("the last thing it said") and len(detail) == worker.MAX_DETAIL_CHARS
    assert found["peak"] < 8 * 1024 * 1024, found["peak"]          # not the 128 MiB it wrote


def test_a_helper_that_stops_making_progress_is_ended_and_the_finalization_fails(tmp_path):
    pid_file, output = tmp_path / "pid", tmp_path / "output.mkv"
    stuck = (f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
             f"open({str(output)!r}, 'wb').write(b'head')\ntime.sleep(120)")
    found = _child(f"""
import json, sys, time
from executors.media import worker
worker.FINALIZATION_STALL_SECONDS = 1
started = time.monotonic()
try:
    worker._run([sys.executable, "-c", {stuck!r}], output={str(output)!r})
    outcome = None
except worker.Failure as failure:
    outcome = [failure.code, failure.detail]
print(json.dumps({{"outcome": outcome, "elapsed": time.monotonic() - started}}))
""", timeout=60)
    assert found["outcome"] == ["finalization_failed", "finalization stopped making progress"]
    assert found["elapsed"] < 30
    with pytest.raises(ProcessLookupError):                        # ended and reaped, not left running
        os.kill(int(pid_file.read_text()), 0)


def test_a_helper_that_keeps_writing_is_never_ended_however_long_it_runs(tmp_path, monkeypatch):
    output = tmp_path / "output.mkv"
    steady = (f"import time\nwith open({str(output)!r}, 'wb') as out:\n    for _ in range(16):\n"
              "        out.write(b'x' * 1024); out.flush(); time.sleep(0.25)")
    monkeypatch.setattr(worker, "FINALIZATION_STALL_SECONDS", 1)
    started = time.monotonic()
    worker._run([sys.executable, "-c", steady], output=str(output))
    assert time.monotonic() - started > 3                          # outlived the window, by progressing
    assert output.stat().st_size == 16 * 1024


def test_an_ordinary_helper_succeeds_and_a_short_failure_reports_its_whole_stderr(tmp_path):
    worker._run([sys.executable, "-c", "import sys; sys.stderr.write('chatter\\n')"], output=str(tmp_path / "o"))
    with pytest.raises(worker.Failure) as failure:
        worker._run([sys.executable, "-c", "import sys; sys.stderr.write('bad input\\n'); sys.exit(3)"],
                    output=str(tmp_path / "o"))
    assert (failure.value.code, failure.value.detail) == ("finalization_failed", "bad input")
    with pytest.raises(worker.Failure) as missing:
        worker._run([str(tmp_path / "no-such-helper")])
    assert missing.value.code == "runtime_unavailable"


@pytest.mark.real_runtime
def test_the_real_finalizer_still_copies_a_good_input_and_fails_a_bad_one_through_run(tmp_path):
    from executors.media.sandbox import MediaSandbox
    confined = MediaSandbox(str(tmp_path / "runtime")).confined_tools()
    V, A = {"vcodec": "avc1", "acodec": "none"}, {"vcodec": "none", "acodec": "mp4a.40.2"}
    for name in ("video.mp4", "audio.m4a"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    output = tmp_path / "output.mp4"
    worker._run(worker.finalization_argv(confined, "mp4", [(str(tmp_path / "video.mp4"), V),
                                                           (str(tmp_path / "audio.m4a"), A)],
                                         None, {"title": "Clip"}, str(output)), output=str(output))
    streams = json.loads(subprocess.run([confined["ffprobe"], "-v", "error", "-show_streams", "-of", "json",
                                         str(output)], check=True, capture_output=True, text=True).stdout)
    assert sorted(item["codec_type"] for item in streams["streams"]) == ["audio", "video"]

    (tmp_path / "broken.mp4").write_bytes((FIXTURES / "video.mp4").read_bytes()[:64] + b"\0" * 4096)
    bad = tmp_path / "bad.mp4"
    with pytest.raises(worker.Failure) as failure:
        worker._run(worker.finalization_argv(confined, "mp4", [(str(tmp_path / "broken.mp4"), V)],
                                             None, {}, str(bad)), output=str(bad))
    assert failure.value.code == "finalization_failed"
    assert 0 < len(failure.value.detail) <= worker.MAX_DETAIL_CHARS
