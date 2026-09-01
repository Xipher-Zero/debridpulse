from api.serializers import public_torrent


def test_pre_materialization_states_always_present_zero_progress():
    for status in ("pending", "uploading", "processing", "ready"):
        browser = public_torrent(
            {
                "id": 1,
                "status": status,
                "progress": 100.0,
            }
        )
        assert browser["progress"] == 0.0


def test_local_transfer_states_preserve_real_progress():
    expected = {
        "queued": 37.0,
        "downloading": 42.5,
        "paused": 61.0,
        "completed": 100.0,
        "error": 73.0,
    }

    for status, progress in expected.items():
        browser = public_torrent(
            {
                "id": 1,
                "status": status,
                "progress": progress,
            }
        )
        assert browser["progress"] == progress
