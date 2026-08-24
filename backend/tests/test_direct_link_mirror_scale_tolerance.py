from services.dispatch_coordinator import plan_direct_link_mirror_suppression


def _row(file_id: int, host: str, size: int) -> dict:
    return {
        "file_id": file_id,
        "torrent_id": 42,
        "filename": "GF200826-TMNTSFS-RN.rar",
        "size_bytes": size,
        "source_url": f"https://{host}/file/{file_id}",
        "status": "pending",
        "download_id": None,
    }


def _duplicate_ids(rows: list[dict]) -> list[int]:
    return [
        int(duplicate["file_id"])
        for duplicate, _primary in plan_direct_link_mirror_suppression(rows)
    ]


def test_observed_megaup_metadata_variance_collapses_with_exact_mirror():
    rows = [
        _row(1, "1fichier.com", 3_595_501_360),
        _row(2, "rapidgator.net", 3_595_501_360),
        _row(3, "megaup.net", 3_597_035_110),
    ]

    assert _duplicate_ids(rows) == [2, 3]


def test_large_file_relative_variance_can_exceed_two_mib_and_still_match():
    base = 100 * 1024**3
    eighty_mib = 80 * 1024**2

    rows = [
        _row(1, "1fichier.com", base),
        _row(2, "megaup.net", base + eighty_mib),
    ]

    assert _duplicate_ids(rows) == [2]


def test_relative_variance_over_point_one_percent_is_rejected():
    base = 100 * 1024**3
    two_hundred_mib = 200 * 1024**2

    rows = [
        _row(1, "1fichier.com", base),
        _row(2, "megaup.net", base + two_hundred_mib),
    ]

    assert _duplicate_ids(rows) == []


def test_512_mib_catastrophe_guard_allows_boundary_when_relative_delta_is_small():
    base = 1024 * 1024**3
    five_hundred_twelve_mib = 512 * 1024**2

    rows = [
        _row(1, "1fichier.com", base),
        _row(2, "megaup.net", base + five_hundred_twelve_mib),
    ]

    assert _duplicate_ids(rows) == [2]


def test_512_mib_catastrophe_guard_rejects_larger_delta_even_below_point_one_percent():
    base = 1024 * 1024**3
    six_hundred_mib = 600 * 1024**2

    rows = [
        _row(1, "1fichier.com", base),
        _row(2, "megaup.net", base + six_hundred_mib),
    ]

    assert _duplicate_ids(rows) == []
