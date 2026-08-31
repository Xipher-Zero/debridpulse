from pathlib import Path

from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_v103_staging_candidate_preserves_external_aria2_global_policy():
    version = Version((REPO_ROOT / "VERSION").read_text().strip())
    assert version >= Version("1.0.3")

    control = (REPO_ROOT / "backend/services/transfer_control.py").read_text()
    manager = (REPO_ROOT / "backend/services/manager_v2.py").read_text()
    service = (REPO_ROOT / "backend/services/transfer_service.py").read_text()

    assert "bind_architecture" in manager
    assert "TransferStateMachine" in service
    assert "TransferControlService" in service
    assert not (REPO_ROOT / "backend/services/_control_bootstrap.py").exists()
    assert "max-overall-download-limit" not in control
    assert "change_global_options" not in control
    assert "_aria2_owned_gids" in control
    assert "Blocked attempt to remove foreign aria2 GID" not in control
