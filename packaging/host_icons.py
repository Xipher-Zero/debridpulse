#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The host icons: their one owner.

The icons ship as a base64 archive split into parts (``frontend/host-icons.parts``,
docs/THIRD_PARTY_ARTWORK.md). Every build of the application unpacks them the
same way, through this file: the server image (Dockerfile) and the installed
desktop application (packaging/desktop/assemble.py). The archive is accepted
only with its pinned checksum and exactly its known files, each a flat .png or
.svg name; anything else stops the build.

    python packaging/host_icons.py <parts directory> <target directory>
"""
from __future__ import annotations

import sys
from base64 import b64decode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

EXPECTED_SHA256 = "2bfb7cadf647f6d4093ce4ad7d13159e137a190925a1b840f8a50a7f579be90f"
EXPECTED_NAMES = frozenset({
    "1fichier.png", "4shared.png", "alfafile.png", "fastbit.png", "file-upload.png",
    "fileal.png", "filedot.png", "filefactory.png", "filespace.png", "gigapeta.png",
    "hexupload.png", "hitfile.png", "isra-cloud.png", "katfile.png", "mediafire.png",
    "mega.svg", "modsbase.png", "mp4upload.png", "prefiles.png", "rapidgator.png",
    "scribd.png", "sendit.png", "simfileshare.png", "streamtape.png", "turbobit.png",
    "upload42.png", "uploadhaven.png", "uploadrar.png", "world-files.png",
})


def unpack(parts: Path, target: Path) -> list[str]:
    """Unpack the verified archive in ``parts`` into ``target``; the names written."""
    encoded = "".join(path.read_text(encoding="ascii") for path in sorted(parts.iterdir()))
    archive = b64decode(encoded, validate=True)
    if sha256(archive).hexdigest() != EXPECTED_SHA256:
        raise RuntimeError("Host artwork archive checksum mismatch")
    target.mkdir(parents=True, exist_ok=True)
    written = []
    with ZipFile(BytesIO(archive)) as package:
        names = {member.filename for member in package.infolist() if not member.is_dir()}
        if names != EXPECTED_NAMES:
            raise RuntimeError("Unexpected host artwork archive contents")
        for member in package.infolist():
            if member.is_dir():
                continue
            name = Path(member.filename)
            if name.name != member.filename or name.suffix.lower() not in {".png", ".svg"}:
                raise RuntimeError("Unexpected host artwork archive member")
            (target / name.name).write_bytes(package.read(member))
            written.append(name.name)
    return sorted(written)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    unpack(Path(sys.argv[1]), Path(sys.argv[2]))
