from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_toast_has_one_canonical_runtime_owner_without_manual_dismissal() -> None:
    operator = _read("operator-title.js")
    batch = _read("ui-correction-batch1.js")
    bridge = _read("ui-toast-contract.js")

    assert "function canonicalToast" in operator
    assert "toast: canonicalToast" in operator
    assert "window.DPIcons.toast" in batch
    assert "window.DPIcons.toast" in bridge

    obsolete = (
        "dp-toast-close",
        "dp-toast-dismiss",
        "Dismiss notification",
        "mouseenter",
        "mouseleave",
        "focusin",
        "focusout",
    )
    for token in obsolete:
        assert token not in operator
        assert token not in batch
        assert token not in bridge


def test_toast_timing_contract_is_word_count_clamped_and_legacy_formula_is_absent() -> None:
    operator = _read("operator-title.js")
    batch = _read("ui-correction-batch1.js")

    assert "const TOAST_MIN_MS = 3000" in operator
    assert "const TOAST_MAX_MS = 10000" in operator
    assert "const TOAST_WORD_MS = 250" in operator
    assert "const TOAST_FADE_MS = 250" in operator
    assert "toastWordCount(message) * TOAST_WORD_MS" in operator
    assert "Math.max(3000, Math.min(10000, words * 250))" in batch

    for token in ("Math.min(12000", "? 4500 : 3500", "words * 230", "chars * 7"):
        assert token not in batch


def test_toast_presentation_css_contains_no_dismiss_only_material() -> None:
    source = _read("ui-toast-contract.css") + "\n" + _read("ui-correction-batch1.css")
    assert ".dp-toast-close" not in source
    assert ".dp-toast-dismiss" not in source
    assert "manual dismissal" not in source
    assert "text-align: center" in source
    assert "pointer-events: none" in source
