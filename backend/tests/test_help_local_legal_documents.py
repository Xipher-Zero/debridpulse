from pathlib import Path

import pytest
from fastapi import HTTPException

from api.settings_validation_routes import _LEGAL_DOCUMENTS, _legal_document_payload


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("document_id", "relative_path"),
    (
        ("gpl", Path("LICENSE")),
        ("notice", Path("NOTICE")),
        ("upstream-mit", Path("LICENSES/MIT.txt")),
        ("source-offer", Path("SOURCE_OFFER.md")),
        ("third-party", Path("docs/DEPENDENCY_LICENSES.md")),
    ),
)
def test_bundled_document_endpoint_reads_the_canonical_packaged_file(document_id, relative_path):
    payload = _legal_document_payload(document_id)

    assert payload["id"] == document_id
    assert payload["content"] == (ROOT / relative_path).read_text(encoding="utf-8")
    assert payload["title"] == _LEGAL_DOCUMENTS[document_id]["title"]
    assert payload["latest_url"] == _LEGAL_DOCUMENTS[document_id]["latest_url"]
    assert payload["latest_url"].startswith("https://github.com/Xipher-Zero/debridpulse/blob/main/")
    assert payload["bundled_version"]


def test_bundled_document_endpoint_is_a_fixed_allowlist_not_an_arbitrary_file_reader():
    with pytest.raises(HTTPException) as exc_info:
        _legal_document_payload("../../config/config.json")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Unknown bundled document"


def test_all_five_license_actions_have_local_document_entries():
    assert set(_LEGAL_DOCUMENTS) == {
        "gpl",
        "notice",
        "upstream-mit",
        "source-offer",
        "third-party",
    }
