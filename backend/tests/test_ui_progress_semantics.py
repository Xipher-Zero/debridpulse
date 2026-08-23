from api.serializers import public_torrent
from services.manager_v2 import normalize_provider_state


def test_ready_provider_progress_is_not_presented_as_local_progress():
    provider = normalize_provider_state(
        {
            "statusCode": 4,
            "status": "Ready",
            "size": 1000,
            "downloaded": 1000,
        }
    )

    assert provider["local_status"] == "ready"
    assert provider["progress"] == 100.0

    browser = public_torrent(
        {
            "id": 1,
            "status": provider["local_status"],
            "progress": provider["progress"],
        }
    )
    assert browser["progress"] == 0.0


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
