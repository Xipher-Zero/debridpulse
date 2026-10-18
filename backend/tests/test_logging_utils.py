import logging

from core.logging_utils import log_startup_banner, sanitize_log_value


def test_sanitize_log_value_redacts_magnets_and_preserves_hash_hint():
    raw = "Failed magnet:?xt=urn:btih:ABCDEF1234567890ABCDEF1234567890ABCDEF12&dn=Example"

    safe = sanitize_log_value(raw)

    assert "magnet:?" not in safe
    assert "ABCDEF1234567890ABCDEF1234567890ABCDEF12" not in safe
    assert "<magnet:abcdef12...>" in safe


def test_sanitize_log_value_redacts_discord_webhook_urls():
    raw = "POST https://discord.com/api/webhooks/123456/super-secret-token failed"

    safe = sanitize_log_value(raw)

    assert "super-secret-token" not in safe
    assert "123456" not in safe
    assert "<webhook-url>" in safe


def test_sanitize_log_value_redacts_url_passwords_and_query_tokens():
    raw = (
        "https://u:very-secret@x.test/a"
        "?sslmode=disable&apikey=abc123&token=def456"
    )

    safe = sanitize_log_value(raw)

    assert "very-secret" not in safe
    assert "abc123" not in safe
    assert "def456" not in safe
    assert "https://u:<redacted>@x.test/a" in safe


def test_startup_banner_uses_logger_and_expected_links(caplog):
    logger = logging.getLogger("alldebrid.main")

    with caplog.at_level(logging.INFO, logger="alldebrid.main"):
        log_startup_banner(
            logger,
            version="1.8.7",
            mode="Docker / Unraid",
            database="SQLite",
            download_client="aria2",
            web_ui="http://0.0.0.0:8080",
            auth="disabled",
        )

    assert "DebridPulse  v1.8.7" in caplog.text
    assert "https://github.com/Xipher-Zero/debridpulse" in caplog.text
    assert "buymeacoffee.com" not in caplog.text
    assert "discord.gg" not in caplog.text


import pytest  # noqa: E402

PASSKEY = "PASSKEY0123456789"


@pytest.mark.parametrize("raw,shown", [
    # The reproduced shape: the tracker (and its passkey) before ``xt=``.
    ("magnet:?dn=movie.mkv&tr=https%3A%2F%2Ftracker.example.org%2F" + PASSKEY + "%2Fannounce&xt=urn:btih:" + "d" * 40,
     "<magnet:dddddddd...>"),
    ("magnet:?tr=https://tracker.example.org/" + PASSKEY + "/announce&xt=urn:btih:" + "f" * 40, "<magnet:ffffffff...>"),
    ("magnet:?xt=urn:btih:" + "E" * 40 + "&tr=https%3A%2F%2Ft.example%2F" + PASSKEY, "<magnet:eeeeeeee...>"),
    ("MAGNET:?xt=urn:btih:" + "A" * 32 + "&tr=udp://t.example/" + PASSKEY, "<magnet:aaaaaaaa...>"),  # base32 hash
    ("magnet:?xt=urn:btmh:1220" + "a" * 64 + "&tr=udp://t.example/" + PASSKEY, "<magnet>"),  # no v1 info-hash
    ("magnet:?dn=only-a-name&tr=udp://t.example/" + PASSKEY, "<magnet>"),
])
def test_every_magnet_form_is_shown_only_as_its_info_hash_hint(raw, shown):
    safe = sanitize_log_value(f"Failed {raw} today")
    assert safe == f"Failed {shown} today"
    assert PASSKEY not in safe and "tracker" not in safe
