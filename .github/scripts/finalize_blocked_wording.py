from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}: {old!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "backend/services/manager_v2.py",
    "# All files were filtered/blocked — nothing to download",
    "# All files were blocked — nothing remains to download",
)
replace_once(
    "backend/services/manager_v2.py",
    'event_msg = "All files were filtered/blocked — marked completed"',
    'event_msg = "All files were blocked — marked completed"',
)
replace_once(
    "backend/tests/test_settings_runtime_contract_census.py",
    '    assert "Filtered files were skipped" not in manager\n',
    '    assert "Filtered files were skipped" not in manager\n    assert "filtered/blocked" not in manager\n',
)

manager = (ROOT / "backend/services/manager_v2.py").read_text(encoding="utf-8")
assert "filtered/blocked" not in manager
assert "All files were blocked — marked completed" in manager
print("Generic explicit-block finalization wording no longer references retired filters.")
