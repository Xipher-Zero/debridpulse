from core.version import (
    compare_versions,
    is_version_newer,
    normalize_version_tag,
    parse_version,
)


def test_normalize_version_tag_accepts_fork_and_legacy_tags():
    assert normalize_version_tag("internal-v2.3.4") == "2.3.4"
    assert normalize_version_tag("v1.9.9") == "1.9.9"
    assert normalize_version_tag("2.3.4") == "2.3.4"


def test_prerelease_ordering_matches_rc_release_semantics():
    assert is_version_newer("1.0.11rc1", "1.0.10") is True
    assert is_version_newer("1.0.11rc2", "1.0.11rc1") is True
    assert is_version_newer("1.0.11", "1.0.11rc2") is True
    assert is_version_newer("1.0.10", "1.0.11rc1") is False
    assert is_version_newer("1.0.11rc1", "1.0.11") is False


def test_release_segments_are_zero_padded_for_comparison():
    assert compare_versions("1.0", "1.0.0") == 0
    assert compare_versions("1.0.1", "1.0") == 1


def test_invalid_version_input_fails_closed():
    assert parse_version("unknown") is None
    assert compare_versions("1.0.11", "unknown") is None
    assert is_version_newer("1.0.11", "unknown") is False
    assert is_version_newer("not-a-release", "1.0.10") is False
