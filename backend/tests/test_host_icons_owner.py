"""The host icons have one owner (packaging/host_icons.py), used by every build.

The server image (Dockerfile) and the installed desktop application
(packaging/desktop/assemble.py) both unpack ``frontend/host-icons.parts``
through it; the desktop builds once shipped without them (a broken image
beside every provider badge). Each icon it writes is the one the artwork
register records.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "packaging" / "host_icons.py"
sys.path.insert(0, str(ROOT / "packaging"))

from host_icons import EXPECTED_NAMES, unpack  # noqa: E402


def _register() -> dict[str, str]:
    text = (ROOT / "docs" / "THIRD_PARTY_ARTWORK.md").read_text(encoding="utf-8")
    return dict(re.findall(r"host-icons\.parts → icons/hosts/([\w.-]+)` \| `([0-9a-f]{64})`", text))


def test_the_owner_unpacks_every_host_icon_as_the_artwork_register_records_it(tmp_path):
    # Run as the builds run it: its own process, its command line.
    subprocess.run([sys.executable, str(OWNER), str(ROOT / "frontend" / "host-icons.parts"), str(tmp_path)], check=True)
    written = {path.name: sha256(path.read_bytes()).hexdigest() for path in tmp_path.iterdir()}
    assert set(written) == EXPECTED_NAMES and len(written) == 29
    register = _register()
    assert set(register) == EXPECTED_NAMES
    assert written == register


def test_an_altered_archive_is_refused_and_nothing_is_written(tmp_path):
    parts = tmp_path / "parts"
    shutil.copytree(ROOT / "frontend" / "host-icons.parts", parts)
    first = sorted(parts.iterdir())[0]
    text = first.read_text(encoding="ascii")
    first.write_text(("B" if text[0] != "B" else "C") + text[1:], encoding="ascii")
    with pytest.raises(RuntimeError, match="checksum"):
        unpack(parts, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_both_builds_unpack_through_the_one_owner_and_neither_keeps_its_own_copy():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY packaging/host_icons.py /tmp/host_icons.py" in dockerfile
    assert "python /tmp/host_icons.py /app/frontend/host-icons.parts /app/frontend/static/icons/hosts" in dockerfile
    assert "b64decode" not in dockerfile and "Host artwork archive" not in dockerfile
    assembler = (ROOT / "packaging" / "desktop" / "assemble.py").read_text(encoding="utf-8")
    assert re.search(r'from host_icons import unpack\n\s+unpack\(ROOT / "frontend" / "host-icons\.parts", '
                     r'out / "frontend" / "static" / "icons" / "hosts"\)', assembler)
    assert "b64decode" not in assembler
    # The desktop application's frontend copy comes first, so the icons land inside what it ships.
    assert assembler.index('copytree(ROOT / "frontend" / "static"') < assembler.index("from host_icons import unpack")
