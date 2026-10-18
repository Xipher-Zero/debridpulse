import json

import pytest

from auth.passwords import BasicVerificationCache, hash_password, verify_password
from auth.throttle import FailureThrottle


def test_argon2id_password_hash_round_trip():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert "correct horse battery staple" not in encoded
    assert verify_password(encoded, "correct horse battery staple") is True
    assert verify_password(encoded, "wrong") is False
    assert verify_password("", "anything") is False
    assert verify_password("not-an-argon2-hash", "anything") is False


def test_basic_verification_cache_is_bounded_and_verifier_specific():
    cache = BasicVerificationCache(max_entries=2, ttl_seconds=30)
    first_hash = hash_password("first")
    second_hash = hash_password("second")

    cache.remember("operator", "first", first_hash)
    assert cache.contains("operator", "first", first_hash) is True
    assert cache.contains("operator", "first", second_hash) is False
    assert cache.contains("operator", "wrong", first_hash) is False

    cache.remember("operator", "second", second_hash)
    cache.remember("other", "third", second_hash)
    assert cache.size == 2
    assert cache.contains("operator", "first", first_hash) is False

    cache.clear()
    assert cache.size == 0


def test_failure_throttle_increases_caps_and_recovers_on_success():
    throttle = FailureThrottle(
        max_entries=2,
        reset_after_seconds=300,
        free_failures=1,
        base_delay_seconds=0.25,
        max_delay_seconds=0.5,
    )
    key = "127.0.0.1"
    assert throttle.delay_for(key) == 0

    throttle.record_failure(key)
    assert throttle.delay_for(key) == 0
    throttle.record_failure(key)
    assert throttle.delay_for(key) == 0.25
    throttle.record_failure(key)
    assert throttle.delay_for(key) == 0.5
    throttle.record_failure(key)
    assert throttle.delay_for(key) == 0.5

    throttle.record_success(key)
    assert throttle.delay_for(key) == 0

    throttle.record_failure("a")
    throttle.record_failure("b")
    throttle.record_failure("c")
    assert throttle.size == 2


def test_legacy_plaintext_password_migrates_and_is_removed(monkeypatch, tmp_path):
    import core.config as config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "auth_username": "operator",
        "auth_password": "legacy-secret",
    }))
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    monkeypatch.setitem(config._legacy_startup_inputs, "credential", None)

    migrated = config.load_settings()
    persisted = json.loads(config_path.read_text())
    username, password_hash = config.legacy_credential_input()

    assert migrated.auth_password_enabled is True
    assert username == "operator"
    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, "legacy-secret") is True
    assert "auth_password" not in persisted
    assert persisted["auth_password_hash"] == password_hash
    assert persisted["auth_password_enabled"] is True


def test_incomplete_legacy_auth_stays_open(monkeypatch, tmp_path):
    import core.config as config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"auth_username": "operator", "auth_password": ""}))
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    monkeypatch.setitem(config._legacy_startup_inputs, "credential", None)

    migrated = config.load_settings()
    persisted = json.loads(config_path.read_text())

    assert migrated.auth_password_enabled is False
    assert config.legacy_credential_input() is None
    assert "auth_password" not in persisted
    assert "auth_password_hash" not in persisted
    assert persisted["auth_password_enabled"] is False


def test_missing_config_does_not_get_created_for_auth_defaults(monkeypatch, tmp_path):
    import core.config as config

    config_path = tmp_path / "missing.json"
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_settings", config.AppSettings())

    loaded = config.load_settings()
    assert loaded.auth_password_enabled is False
    assert config_path.exists() is False


def test_a_pending_file_credential_survives_settings_saves_until_retired(monkeypatch, tmp_path):
    """Until the account owner installs it, the configuration file's credential
    is carried by every rewrite of the file; once retired, no save writes it."""
    import core.config as config

    config_path = tmp_path / "config.json"
    password_hash = hash_password("secret")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    monkeypatch.setitem(config._legacy_startup_inputs, "credential", ("operator", password_hash))

    config.save_settings(config.AppSettings(auth_password_enabled=True, max_concurrent_downloads=7))
    persisted = json.loads(config_path.read_text())
    assert (persisted["auth_username"], persisted["auth_password_hash"]) == ("operator", password_hash)

    config.retire_legacy_credential_input()
    persisted = json.loads(config_path.read_text())
    assert config.legacy_credential_input() is None
    assert not {"auth_username", "auth_password", "auth_password_hash"} & set(persisted)


def test_settings_hold_no_local_credential():
    from core.config import AppSettings

    assert not {"auth_username", "auth_password", "auth_password_hash"} & set(AppSettings.model_fields)
    assert "auth_password_hash" not in AppSettings(auth_password_hash=hash_password("secret")).model_dump()


def test_a_success_clears_only_its_own_identitys_failures_from_that_peer():
    """PA-AUTHZ-3: guessing one account's password and then signing in as
    another account from the same peer keeps the guesses' delay."""
    throttle = FailureThrottle(free_failures=2, base_delay_seconds=0.25, max_delay_seconds=2.0)
    peer = "192.0.2.7"
    for _ in range(5):
        throttle.record_failure(peer, "admin")
    delay = throttle.delay_for(peer)
    assert delay == 1.0
    throttle.record_success(peer, "alice")
    assert throttle.delay_for(peer) == delay

    # A user's own mistypes are cleared by their own success, as before.
    throttle.record_failure(peer, "alice")
    throttle.record_failure(peer, "alice")
    assert throttle.delay_for(peer) == 2.0
    throttle.record_success(peer, "alice")
    assert throttle.delay_for(peer) == delay
    assert throttle.delay_for("192.0.2.8") == 0  # another peer is unaffected


def test_attributed_failures_stay_bounded_and_overflow_is_never_cleared_by_a_success():
    throttle = FailureThrottle(free_failures=0, base_delay_seconds=1.0, max_delay_seconds=64.0, max_identities=2)
    peer = "192.0.2.9"
    for name in ("a", "b", "c", "d", "e"):
        throttle.record_failure(peer, name)
    assert len(throttle._entries[peer].failures) == 3  # two attributed identities and the overflow
    assert throttle.delay_for(peer) == 16.0
    for name in ("c", "d", "e"):
        throttle.record_success(peer, name)
    assert throttle.delay_for(peer) == 16.0
    throttle.record_success(peer, "a")
    throttle.record_success(peer, "b")
    assert throttle.delay_for(peer) == 4.0  # three overflowed failures remain, no lockout beyond the window


def test_failures_still_recover_after_the_window(monkeypatch):
    import auth.throttle as module

    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    throttle = FailureThrottle(free_failures=0, base_delay_seconds=1.0, reset_after_seconds=300)
    throttle.record_failure("p", "admin")
    assert throttle.delay_for("p") == 1.0
    now[0] += 301
    assert throttle.delay_for("p") == 0


@pytest.mark.asyncio
async def test_signing_in_as_your_own_account_does_not_reset_the_peers_guesses(tmp_path, monkeypatch):
    """The reproduced sequence through the real verifier: wrong ``admin``
    guesses, then a valid sign-in as the guesser's own User account."""
    from types import SimpleNamespace

    from account_support import account_database
    from auth import accounts
    from auth.accounts import Role
    from auth.manager import verify_local_credentials
    from auth.throttle import password_failure_throttle

    await account_database(tmp_path, monkeypatch)
    await accounts.create_local_account("alice", "alicepw", role=Role.USER)
    cfg = SimpleNamespace(auth_password_enabled=True)
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"))
    password_failure_throttle.clear()
    try:
        for _ in range(4):
            assert await verify_local_credentials(request, "admin", "guess", settings=cfg) is None
        held = password_failure_throttle.delay_for("192.0.2.10")
        assert held > 0
        assert (await verify_local_credentials(request, "alice", "alicepw", settings=cfg)).username == "alice"
        assert password_failure_throttle.delay_for("192.0.2.10") == held
    finally:
        password_failure_throttle.clear()
