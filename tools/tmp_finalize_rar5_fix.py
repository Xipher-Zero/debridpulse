from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "backend/tests/test_security_hardening_v106.py",
    '''def test_7zip_type_switches_pin_plain_archive_parsers():
    assert _seven_zip_type_switches(Path("payload.7z")) == ["-t7z"]
    assert _seven_zip_type_switches(Path("payload.rar")) == ["-trar"]
    assert _seven_zip_type_switches(Path("payload.r00")) == ["-trar"]
''',
    '''def test_7zip_type_switches_pin_plain_7z_and_magic_validate_rar(tmp_path):
    assert _seven_zip_type_switches(Path("payload.7z")) == ["-t7z"]

    rar5 = tmp_path / "payload.rar"
    rar5.write_bytes(b"Rar!\\x1a\\x07\\x01\\x00" + b"payload")
    assert _seven_zip_type_switches(rar5) == []

    rar4 = tmp_path / "payload.r00"
    rar4.write_bytes(b"Rar!\\x1a\\x07\\x00" + b"payload")
    assert _seven_zip_type_switches(rar4) == []
''',
    "RAR parser security contract",
)

replace_once(
    "backend/tests/test_security_hardening_v106.py",
    '''def test_rar_extract_uses_strict_rar_parser():
    with patch("services.extractor._tool_available", return_value=True), patch(
        "services.extractor._preflight_7z"
    ), patch("services.extractor._run_tool", return_value=(0, "")) as run_tool:
        _extract_rar_to(Path("payload.rar"), Path("/tmp/dp-security-test"))

    command = run_tool.call_args.args[0]
    assert "-trar" in command
''',
    '''def test_rar_extract_uses_magic_validated_7zip_autodetect(tmp_path):
    archive = tmp_path / "payload.rar"
    archive.write_bytes(b"Rar!\\x1a\\x07\\x01\\x00" + b"payload")

    with patch("services.extractor._tool_available", return_value=True), patch(
        "services.extractor._preflight_7z"
    ), patch("services.extractor._run_tool", return_value=(0, "")) as run_tool:
        _extract_rar_to(archive, Path("/tmp/dp-security-test"))

    command = run_tool.call_args.args[0]
    assert "-trar" not in command
    assert str(archive) in command
''',
    "RAR extraction security contract",
)

print("RAR5 security contracts corrected")
