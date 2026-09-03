"""Temporary staging-only bootstrap for STATE-003 remediation validation.

This file MUST NOT enter the final candidate tree. It applies the proposed patch in
an Actions runner before pytest imports the application, then emits exact base64
contents of the changed tracked files so the validated blobs can be assembled into
a clean commit descending directly from the audited candidate.
"""

from __future__ import annotations

import base64
import os
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SENTINEL = ROOT / ".tmp-state003-patched"
TARGET_REF = "refs/heads/audit/state003-remediation-work"
CHANGED = (
    "backend/transfers/engine.py",
    "backend/transfers/repository.py",
    "backend/tests/test_second_pass_state002.py",
    "backend/tests/test_post_audit_architecture_documentation.py",
    "docs/architecture/UNIVERSAL_TRANSFER_CORE.md",
)


def _dump_validated_sources() -> None:
    print("STATE003_VALIDATED_BLOBS_BEGIN")
    for relative in CHANGED:
        encoded = base64.b64encode((ROOT / relative).read_bytes()).decode("ascii")
        print(f"STATE003_BLOB_BEGIN {relative}")
        for offset in range(0, len(encoded), 120):
            print(encoded[offset : offset + 120])
        print(f"STATE003_BLOB_END {relative}")
    print("STATE003_VALIDATED_BLOBS_END")


if os.getenv("GITHUB_REF") == TARGET_REF and not SENTINEL.exists():
    os.chdir(ROOT)
    runpy.run_path(str(ROOT / "scripts/tmp_state003_patch.py"), run_name="__main__")
    SENTINEL.write_text("patched\n", encoding="utf-8")
    print("STATE003_PATCH_APPLIED")
    _dump_validated_sources()
