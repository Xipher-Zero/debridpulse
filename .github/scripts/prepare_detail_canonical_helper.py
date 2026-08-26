from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_detail_canonical_helper.py <helper>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one helper occurrence of {old!r}, found {count}")
    text = text.replace(old, new)


# Keep generated modal CSS clean at EOF.
replace_once(
    'modal.write_text(modal_text.rstrip() + modal_append + "\\n", encoding="utf-8")\n',
    'modal.write_text(modal_text.rstrip() + modal_append.rstrip() + "\\n", encoding="utf-8")\n',
)

# One older cascade-order contract stores the imported filename without a
# leading slash. Migrate that literal as well as the canonical slash-prefixed
# cache reference already handled by the cleanup helper.
replace_once(
    '    ("/ui-modal-contract.css?v=24", "/ui-modal-contract.css?v=25"),\n'
    '    ("/ui-downloads-page.css?v=26", "/ui-downloads-page.css?v=27"),\n',
    '    ("/ui-modal-contract.css?v=24", "/ui-modal-contract.css?v=25"),\n'
    '    ("ui-modal-contract.css?v=24", "ui-modal-contract.css?v=25"),\n'
    '    ("/ui-downloads-page.css?v=26", "/ui-downloads-page.css?v=27"),\n',
)

# The prior correction-batch contract encoded the old temporary composition
# with Clear Selection in the left action cluster. Replace only that exact
# assertion with the now-reviewed left-actions/right-status invariant.
anchor = 'contract = TESTS / "test_ui_detail_overlay_cleanup_contract.py"\n'
replacement = '''stale_bulk_contract = TESTS / "test_ui_downloads_correction_batch_contract.py"\nreplace_exact(\n    stale_bulk_contract,\n    '    assert "actions.append(pause, resume, reset, separator, remove, clear);" in runtime\\n',\n    '    assert "actions.append(pause, resume, reset, separator, remove);" in runtime\\n'\n    '    assert "status.append(count, clear);" in runtime\\n',\n)\n\n''' + anchor
replace_once(anchor, replacement)

path.write_text(text, encoding="utf-8")
