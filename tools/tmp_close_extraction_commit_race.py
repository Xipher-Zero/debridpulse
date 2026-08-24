from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str, label: str) -> None:
    target = ROOT / path
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match in {path}, found {count}")
    target.write_text(text.replace(old, new, 1))


replace_once(
    "backend/services/extraction_safety.py",
    '''        for target in directory_targets:\n            if not target.exists():\n                target.mkdir(parents=True, exist_ok=False)\n                created_dirs.append(target)\n        for source, target in file_moves:\n            target.parent.mkdir(parents=True, exist_ok=True)\n            _ensure_safe_destination(root, target)\n            os.replace(source, target)\n            committed.append(target)\n''',
    '''        for target in directory_targets:\n            if target.exists():\n                continue\n            try:\n                # Directory targets are depth-sorted, so their parent has\n                # already been handled. If another extraction creates the same\n                # benign directory concurrently, accept that race only after\n                # re-validating that it is still a real directory.\n                target.mkdir(exist_ok=False)\n                created_dirs.append(target)\n            except FileExistsError:\n                if target.is_symlink() or not target.is_dir():\n                    raise\n        for source, target in file_moves:\n            _ensure_safe_destination(root, target)\n            # Staging is deliberately created under the destination, so source\n            # and target share a filesystem. link() provides an atomic\n            # no-clobber commit: if another extraction creates target after the\n            # preflight, FileExistsError wins instead of overwriting its data.\n            # The staging hard link is removed by the enclosing rmtree only\n            # after the entire merge has succeeded.\n            os.link(source, target, follow_symlinks=False)\n            committed.append(target)\n''',
    "atomic no-clobber extraction commit",
)

# Range fingerprints must represent stored bytes rather than transparent HTTP
# content decoding, otherwise Content-Range and sampled bytes can describe
# different representations.
network = ROOT / "backend/services/network_safety.py"
text = network.read_text()
old = 'headers={"Range": f"bytes=0-{sample_bytes - 1}"},'
new = 'headers={"Range": f"bytes=0-{sample_bytes - 1}", "Accept-Encoding": "identity"},'
if text.count(old) != 1:
    raise RuntimeError("first range request header contract changed")
text = text.replace(old, new, 1)
old = 'headers={"Range": f"bytes={last_start}-{total - 1}"},'
new = 'headers={"Range": f"bytes={last_start}-{total - 1}", "Accept-Encoding": "identity"},'
if text.count(old) != 1:
    raise RuntimeError("last range request header contract changed")
network.write_text(text.replace(old, new, 1))

# Add an actual concurrent same-target regression. One extraction may claim the
# pathname; the other must fail cleanly rather than replacing it.
test_path = ROOT / "backend/tests/test_full_audit_remediation_20260824.py"
tests = test_path.read_text()
if "import asyncio\n" not in tests:
    tests = tests.replace("from __future__ import annotations\n\n", "from __future__ import annotations\n\nimport asyncio\n", 1)
append = '''\n\n@pytest.mark.asyncio\nasync def test_concurrent_extractions_cannot_clobber_same_new_target(tmp_path):\n    dest = tmp_path / "download"\n    dest.mkdir()\n    first = dest / "first.zip"\n    second = dest / "second.zip"\n    with zipfile.ZipFile(first, "w") as zf:\n        zf.writestr("shared/payload.txt", "first")\n    with zipfile.ZipFile(second, "w") as zf:\n        zf.writestr("shared/payload.txt", "second")\n\n    extractor = Extractor(max_concurrent=2)\n    results = await asyncio.gather(\n        extractor.extract_archive(first, dest, delete_after=False),\n        extractor.extract_archive(second, dest, delete_after=False),\n    )\n\n    assert sorted(ok for ok, _message in results) == [False, True]\n    assert (dest / "shared" / "payload.txt").read_text() in {"first", "second"}\n    assert first.exists() and second.exists()\n\n\ndef test_sample_fingerprints_request_identity_bytes_and_refuse_redirects():\n    source = (Path(__file__).resolve().parents[1] / "services/network_safety.py").read_text()\n    assert source.count('"Accept-Encoding": "identity"') == 2\n    assert source.count("allow_redirects=False") == 2\n'''
if "test_concurrent_extractions_cannot_clobber_same_new_target" not in tests:
    tests += append
test_path.write_text(tests)

print("Extraction commit race remediation applied")
