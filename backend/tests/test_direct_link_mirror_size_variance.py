from services.dispatch_coordinator import plan_direct_link_mirror_suppression


def _row(file_id, host, size):
    return {
        "file_id": file_id,
        "torrent_id": 133,
        "filename": "GF200826-TMNTSFS-RN.rar",
        "size_bytes": size,
        "source_url": f"https://{host}/file/{file_id}",
        "status": "pending",
        "download_id": None,
    }


def test_real_world_megaup_metadata_variance_collapses_with_exact_mirror():
    rows = [
        _row(1, "1fichier.com", 3595501360),
        _row(2, "rapidgator.net", 3595501360),
        # Inferred from the pre-dispatch parent total captured during live
        # validation. aria2 later normalized this row to the actual payload
        # length, masking the initial host/provider metadata discrepancy.
        _row(3, "megaup.net", 3597035110),
    ]

    plan = plan_direct_link_mirror_suppression(rows)

    assert [duplicate["file_id"] for duplicate, _primary in plan] == [2, 3]
    assert {primary["file_id"] for _duplicate, primary in plan} == {1}


def test_size_variance_over_two_mib_can_collapse_when_relative_delta_is_small():
    rows = [
        _row(1, "1fichier.com", 10_000_000_000),
        _row(2, "rapidgator.net", 10_003_000_000),
    ]

    plan = plan_direct_link_mirror_suppression(rows)

    assert [duplicate["file_id"] for duplicate, _primary in plan] == [2]


def test_size_variance_over_point_one_percent_is_not_collapsed_even_when_absolute_delta_is_small():
    rows = [
        _row(1, "1fichier.com", 100_000_000),
        _row(2, "rapidgator.net", 100_200_000),
    ]

    assert plan_direct_link_mirror_suppression(rows) == []
