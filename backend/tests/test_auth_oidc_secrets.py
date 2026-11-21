import json

from core import config as config_module
from core.config import AppSettings
from core.config_validator import validate_and_sanitise


def test_oidc_client_secret_is_excluded_from_model_serialization():
    cfg = AppSettings(
        oidc_client_secret="super-secret",
        oidc_client_secret_clear=True,
    )
    dumped = cfg.model_dump()
    assert "oidc_client_secret" not in dumped
    assert "oidc_client_secret_clear" not in dumped
    assert "super-secret" not in repr(dumped)


def test_oidc_client_secret_survives_validation_without_becoming_serializable():
    cfg = AppSettings(
        auth_oidc_enabled=True,
        oidc_issuer_url="https://id.example/application/o/downloadpulse",
        oidc_client_id="client",
        oidc_client_secret="super-secret",
        public_base_url="https://pulse.example",
    )
    validated = validate_and_sanitise(cfg)
    assert validated.oidc_client_secret == "super-secret"
    assert "oidc_client_secret" not in validated.model_dump()


def test_explicit_oidc_client_secret_clear_persists_empty_secret(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    previous = AppSettings(oidc_client_secret="old-secret")
    candidate = previous.model_copy(
        update={
            "oidc_client_secret": "",
            "oidc_client_secret_clear": True,
        }
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "_settings", previous)

    config_module.save_settings(candidate)

    persisted = json.loads(config_path.read_text())
    assert persisted["oidc_client_secret"] == ""
    assert "oidc_client_secret_clear" not in persisted
    assert candidate.oidc_client_secret == ""
