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
