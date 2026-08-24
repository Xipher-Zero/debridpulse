from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "backend/services/extractor.py",
    '''_RAR_EXTS  = {".rar", ".r00", ".r01", ".r02"}
''',
    '''_RAR_EXTS  = {".rar", ".r00", ".r01", ".r02"}
_RAR4_MAGIC = b"Rar!\\x1a\\x07\\x00"
_RAR5_MAGIC = b"Rar!\\x1a\\x07\\x01\\x00"
''',
    "RAR magic constants",
)

replace_once(
    "backend/services/extractor.py",
    '''def _seven_zip_type_switches(archive: Path) -> list[str]:
''',
    '''def _rar_version(archive: Path) -> int:
    """Validate RAR content by magic bytes and return major format version.

    7-Zip 25.x distinguishes its forced legacy RAR parser from RAR5.  Forcing
    ``-trar`` against a valid RAR5 archive makes 7-Zip reject it as "not archive".
    Validate the content ourselves, then allow 7-Zip to choose the RAR4/RAR5
    parser from the already-verified bytes.
    """
    try:
        with archive.open("rb") as source:
            header = source.read(max(len(_RAR4_MAGIC), len(_RAR5_MAGIC)))
    except OSError as exc:
        raise RuntimeError(f"Cannot read RAR header for {archive.name}: {exc}") from exc

    if header.startswith(_RAR5_MAGIC):
        return 5
    if header.startswith(_RAR4_MAGIC):
        return 4
    raise ValueError(f"RAR signature mismatch for {archive.name}")


def _seven_zip_type_switches(archive: Path) -> list[str]:
''',
    "RAR signature validator",
)

replace_once(
    "backend/services/extractor.py",
    '''    if suffix in _RAR_EXTS:
        return ["-trar"]
''',
    '''    if suffix in _RAR_EXTS:
        _rar_version(archive)
        # Do not force -trar here.  7-Zip 25.x rejects valid RAR5 archives when
        # forced through that legacy parser, while auto-detection correctly
        # identifies the already magic-validated input as Rar5.
        return []
''',
    "RAR parser selection",
)

# Add focused regression coverage alongside the existing archive helpers.
replace_once(
    "backend/tests/test_extractor.py",
    '''    _suffix,
    _TAR_7Z_EXTS,
''',
    '''    _suffix,
    _rar_version,
    _seven_zip_type_switches,
    _TAR_7Z_EXTS,
''',
    "RAR helper imports",
)

insert_after = '''def make_single_gz(path: Path, content: bytes = b"hello") -> None:
    with gzip.open(path, "wb") as f:
        f.write(content)

'''
addition = '''def make_rar_header(path: Path, version: int) -> None:
    if version == 5:
        path.write_bytes(b"Rar!\\x1a\\x07\\x01\\x00" + b"payload")
    elif version == 4:
        path.write_bytes(b"Rar!\\x1a\\x07\\x00" + b"payload")
    else:
        raise ValueError(version)


def test_rar5_magic_uses_7zip_autodetect_not_legacy_trar(tmp_path):
    archive = tmp_path / "payload.rar"
    make_rar_header(archive, 5)
    assert _rar_version(archive) == 5
    assert _seven_zip_type_switches(archive) == []


def test_rar4_magic_uses_7zip_autodetect(tmp_path):
    archive = tmp_path / "payload.rar"
    make_rar_header(archive, 4)
    assert _rar_version(archive) == 4
    assert _seven_zip_type_switches(archive) == []


def test_rar_extension_with_invalid_magic_fails_before_parser_selection(tmp_path):
    archive = tmp_path / "payload.rar"
    archive.write_bytes(b"not-a-rar")
    with pytest.raises(ValueError, match="RAR signature mismatch"):
        _seven_zip_type_switches(archive)

'''
replace_once(
    "backend/tests/test_extractor.py",
    insert_after,
    insert_after + "\n" + addition,
    "RAR parser regression tests",
)

print("RAR5 parser correction applied")
