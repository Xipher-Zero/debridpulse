from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str, label: str) -> None:
    target = ROOT / path
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match in {path}, found {count}")
    target.write_text(text.replace(old, new, 1))


# Provider/systemic unlock failures are not source failover candidates. Strip
# the persistence taxonomy prefix before presenting the diagnostic reason.
replace_once(
    "backend/services/direct_link_result_guard.py",
    '''        if persisted.startswith("source-unlock:"):\n            reason = persisted.split(":", 1)[1].strip() or "source unlock failed"\n            return True, reason\n        if persisted.startswith("aria2-dispatch:"):\n''',
    '''        if persisted.startswith("source-unlock:"):\n            reason = persisted.split(":", 1)[1].strip() or "source unlock failed"\n            return True, reason\n        if persisted.startswith("provider-unlock:"):\n            reason = persisted.split(":", 1)[1].strip() or "provider unlock failed"\n            return False, reason\n        if persisted.startswith("aria2-dispatch:"):\n''',
    "provider unlock failover taxonomy",
)

# Update the source-contract test to assert the classified prefix rather than
# pinning the former unconditional source-unlock string.
replace_once(
    "backend/tests/test_direct_link_mirror_failover.py",
    '''    assert 'reason=f"source-unlock: {error_text}"' in manager_source\n''',
    '''    assert 'reason=f"{_direct_link_unlock_failure_prefix(error)}: {error_text}"' in manager_source\n    assert 'return "provider-unlock"' in manager_source\n''',
    "mirror failover source contract",
)

# These extractor tests are about batching/concurrency, not overwrite semantics.
# Give each fixture a unique member name now that cross-operation clobbering is
# intentionally rejected by the extraction ownership boundary.
replace_once(
    "backend/tests/test_extractor.py",
    '''def make_zip(path: Path, content: bytes = b"hello") -> None:\n    with zipfile.ZipFile(path, "w") as zf:\n        zf.writestr("file.txt", content)\n''',
    '''def make_zip(path: Path, content: bytes = b"hello", member: str = "file.txt") -> None:\n    with zipfile.ZipFile(path, "w") as zf:\n        zf.writestr(member, content)\n''',
    "zip test helper member name",
)
replace_once(
    "backend/tests/test_extractor.py",
    '''    make_zip(tmp_path / "a.zip", b"aaa")\n    make_zip(tmp_path / "b.zip", b"bbb")\n''',
    '''    make_zip(tmp_path / "a.zip", b"aaa", member="a.txt")\n    make_zip(tmp_path / "b.zip", b"bbb", member="b.txt")\n''',
    "multiple archive unique members",
)
replace_once(
    "backend/tests/test_extractor.py",
    '''    # Extracted file present (both wrote file.txt into tmp_path)\n    assert (tmp_path / "file.txt").exists()\n''',
    '''    assert (tmp_path / "a.txt").read_bytes() == b"aaa"\n    assert (tmp_path / "b.txt").read_bytes() == b"bbb"\n''',
    "multiple archive expected outputs",
)
replace_once(
    "backend/tests/test_extractor.py",
    '''        make_zip(tmp_path / f"archive{i}.zip", f"content{i}".encode())\n''',
    '''        make_zip(\n            tmp_path / f"archive{i}.zip",\n            f"content{i}".encode(),\n            member=f"file{i}.txt",\n        )\n''',
    "serial concurrency unique members",
)
replace_once(
    "backend/tests/test_extractor.py",
    '''        make_zip(tmp_path / f"p{i}.zip", f"data{i}".encode())\n''',
    '''        make_zip(\n            tmp_path / f"p{i}.zip",\n            f"data{i}".encode(),\n            member=f"parallel{i}.txt",\n        )\n''',
    "parallel concurrency unique members",
)

# Release identity was deliberately advanced because the audit remediation is
# materially beyond 1.0.6 and inherited upstream tags occupy 1.0.7-1.0.9.
replace_once(
    "backend/tests/test_v105_performance_architecture.py",
    '''    assert (ROOT / "VERSION").read_text().strip() == "1.0.6"\n''',
    '''    assert (ROOT / "VERSION").read_text().strip() == "1.0.10"\n''',
    "release version contract",
)

# Keep changelog language aligned with the existing candidate window: the
# improvement is content verification, not shrinking the tolerance itself.
changelog = ROOT / "CHANGELOG.md"
text = changelog.read_text()
text = text.replace(
    "Strengthened near-size mirror identity with a 512 MiB absolute variance ceiling plus bounded first/last content fingerprints; unverifiable near-size candidates remain independent downloads.",
    "Strengthened near-size mirror identity with bounded first/last content fingerprints while retaining the existing 512 MiB catastrophe guard; unverifiable near-size candidates remain independent downloads.",
)
changelog.write_text(text)

print("Full audit remediation test/contracts finalized")
