from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_toast_public_bridge_delegates_to_canonical_presenter() -> None:
    operator = read("operator-title.js")
    bridge = read("ui-toast-contract.js")
    assert "function canonicalToast" in operator
    assert "toast: canonicalToast" in operator
    assert "window.DPIcons.toast" in bridge
    assert "window.DPToastContract" in bridge
    assert "DPUICorrectionBatch1" not in bridge


def test_toast_timing_is_word_count_clamped() -> None:
    operator = read("operator-title.js")
    bridge = read("ui-toast-contract.js")
    assert "const TOAST_MIN_MS = 3000" in operator
    assert "const TOAST_MAX_MS = 10000" in operator
    assert "const TOAST_WORD_MS = 250" in operator
    assert "words*250" in bridge.replace(" ", "")
    assert "Math.max(3000,Math.min(10000" in bridge.replace(" ", "")


def test_reviewed_toast_copy_is_owned_by_bridge() -> None:
    bridge = read("ui-toast-contract.js")
    assert "DebridPulse stared at that for a moment. It is not a link, magnet, or torrent." in bridge
    assert "Checking transfers for recoverable work…" in bridge
    assert "Checking AllDebrid for ready torrents" in bridge


def test_toast_has_no_manual_dismissal_contract() -> None:
    joined = read("operator-title.js") + read("ui-toast-contract.js") + read("ui-toast-contract.css")
    for token in ("dp-toast-close", "dp-toast-dismiss", "Dismiss notification"):
        assert token not in joined
