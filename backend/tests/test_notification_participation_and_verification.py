"""Notification participation, effective destination, and verification evidence.

Three facts about a notification section are deliberately independent, and most
of the ways this page could go wrong are a confusion between two of them:

* **participation** -- whether the section takes part in delivery at all;
* **configuration** -- whether the destination the delivery logic would
  actually use exists (including Statistics Reporting's fallback to the primary
  Discord webhook);
* **verification** -- whether a Test has exercised exactly what is saved.

Switching a section off must stop its delivery and erase nothing. Clearing one
destination must not touch another, and must not silently move either section's
participation. A Test must prove the saved material and nothing else, and its
proof must stop being true when that material changes -- but not when something
unrelated does.

These cases exercise the one canonical owner, ``services.notification_service``,
plus the real runtime gates that read it.
"""
import json
from types import SimpleNamespace

import pytest

import core.config as config
from core.config import AppSettings
from services import notification_service as notifications


PRIMARY = "https://discord.com/api/webhooks/1/primary"
ADDED = "https://discord.com/api/webhooks/2/added"
REPORT = "https://discord.com/api/webhooks/3/report"


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_settings", AppSettings())
    return path


def _live(monkeypatch, settings):
    """Make ``settings`` what every runtime reader sees."""
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(notifications, "get_settings", lambda: settings)
    return settings


# ── Upgrade contract ─────────────────────────────────────────────────────────

def test_configuration_written_before_participation_existed_keeps_working(config_path):
    """The whole point of the default: an upgrade must not silence anything.

    A pre-existing installation had a webhook and its event choices and nothing
    else; nothing in it can say whether the operator wanted to participate,
    because the question did not exist. Answering "yes" is what preserves the
    behaviour they already had.
    """
    config_path.write_text(json.dumps({
        "discord_webhook_url": PRIMARY,
        "stats_report_webhook_url": REPORT,
        "stats_report_interval_hours": 12,
        "discord_notify_error": False,
    }))

    loaded = config.load_settings()

    assert notifications.discord_participates(loaded) is True
    assert notifications.reporting_participates(loaded) is True
    # ... and nothing else about it moved.
    assert loaded.discord_notify_error is False
    assert notifications.notification_state(loaded) == {
        "discord_notifications_configured": True,
        "discord_notifications_verified": False,
        "stats_reporting_configured": True,
        "stats_reporting_verified": False,
    }


def test_fresh_installation_participates_but_delivers_nothing(config_path):
    """Sane fresh state: nothing is switched off, and nothing is configured."""
    fresh = config.load_settings()

    assert (fresh.discord_notifications_enabled, fresh.stats_reporting_enabled) == (True, True)
    assert notifications.discord_configured(fresh) is False
    assert notifications.reporting_configured(fresh) is False
    assert fresh.stats_report_interval_hours == 0


def test_participation_survives_a_save_and_reload(config_path):
    settings = AppSettings(discord_notifications_enabled=False, stats_reporting_enabled=False,
                           discord_webhook_url=PRIMARY)
    config.save_settings(settings)
    reloaded = config.load_settings()

    assert reloaded.discord_notifications_enabled is False
    assert reloaded.stats_reporting_enabled is False
    # Switched off is not erased.
    assert reloaded.discord_webhook_url == PRIMARY


# ── Participation gates delivery, and nothing else ───────────────────────────

def test_disabled_discord_hands_out_a_client_with_no_destinations(monkeypatch):
    """The ONE runtime gate. Every send path already no-ops on an empty URL, so
    one gate switches the whole feature off without any of them knowing."""
    _live(monkeypatch, AppSettings(discord_notifications_enabled=False,
                                   discord_webhook_url=PRIMARY, discord_webhook_added=ADDED))
    client = notifications.NotificationService().client()
    assert (client.webhook_url, client.added_webhook_url) == ("", "")


def test_enabled_discord_hands_out_the_configured_destinations(monkeypatch):
    _live(monkeypatch, AppSettings(discord_webhook_url=PRIMARY, discord_webhook_added=ADDED))
    client = notifications.NotificationService().client()
    assert (client.webhook_url, client.added_webhook_url) == (PRIMARY, ADDED)


@pytest.mark.asyncio
async def test_a_destinationless_client_delivers_nothing_on_any_event_path(monkeypatch):
    """Closes the inference: the gate hands out empty URLs, and every send the
    runtime can make returns without reaching the network when they are empty.

    Without this the participation gate would only be proven to change a string.
    """
    import aiohttp

    from services.notifications import NotificationService as Client

    _live(monkeypatch, AppSettings(discord_notifications_enabled=False,
                                   discord_webhook_url=PRIMARY, discord_webhook_added=ADDED))
    client = notifications.NotificationService().client()

    def _refuse(*args, **kwargs):
        raise AssertionError("a disabled section opened a session")

    monkeypatch.setattr(aiohttp, "ClientSession", _refuse)

    await client.send_added("probe")
    await client.send_complete("probe")
    await client.send_error("probe")
    await client.send_partial("probe", 2, 1, 1)
    await client.send_extract_complete("probe")
    await client.send_extract_failed("probe")
    await client.send("probe", "probe")
    await client.send_update("1.0.0", "1.0.1")
    assert await client.test() is False


@pytest.mark.asyncio
async def test_an_enabled_client_would_reach_the_network(monkeypatch):
    """The negative above only means something if the positive is different."""
    import aiohttp

    reached = []

    def _record(*args, **kwargs):
        reached.append(True)
        raise RuntimeError("stop before any real request")

    _live(monkeypatch, AppSettings(discord_webhook_url=PRIMARY))
    monkeypatch.setattr(aiohttp, "ClientSession", _record)
    await notifications.NotificationService().client().send_complete("probe")
    assert reached


def test_download_added_falls_back_to_the_primary_when_no_override_exists(monkeypatch):
    _live(monkeypatch, AppSettings(discord_webhook_url=PRIMARY))
    client = notifications.NotificationService().client()
    assert client.added_webhook_url == PRIMARY


def test_disabling_discord_leaves_every_stored_value_alone():
    stored = AppSettings(discord_webhook_url=PRIMARY, discord_webhook_added=ADDED,
                         discord_username="Bot", discord_avatar_url="https://x/a.png",
                         discord_notify_added=False, stats_report_webhook_url=REPORT)
    off = stored.model_copy(update={"discord_notifications_enabled": False})

    assert notifications.discord_configured(off) is True
    assert notifications.discord_destination(off) == PRIMARY
    for field in ("discord_webhook_url", "discord_webhook_added", "discord_username",
                  "discord_avatar_url", "discord_notify_added", "stats_report_webhook_url"):
        assert getattr(off, field) == getattr(stored, field)
    # And the other section is untouched in every respect.
    assert off.stats_reporting_enabled is True
    assert notifications.reporting_configured(off) is True


def test_disabling_reporting_leaves_discord_and_its_own_configuration_alone():
    stored = AppSettings(discord_webhook_url=PRIMARY, stats_report_webhook_url=REPORT,
                         stats_report_interval_hours=6, stats_report_window_hours=168)
    off = stored.model_copy(update={"stats_reporting_enabled": False})

    assert off.discord_notifications_enabled is True
    assert notifications.discord_destination(off) == PRIMARY
    assert (off.stats_report_webhook_url, off.stats_report_interval_hours,
            off.stats_report_window_hours) == (REPORT, 6, 168)


@pytest.mark.parametrize("enabled,interval,destination,admitted", [
    (True, 6, REPORT, True),
    (False, 6, REPORT, False),     # switched off
    (True, 0, REPORT, False),      # no automatic cadence -- a separate fact
    (True, 6, "", False),          # nowhere to send
])
def test_scheduled_reporting_needs_all_three_independent_facts(enabled, interval, destination, admitted):
    from core.scheduler import _has_reporting_webhook

    cfg = AppSettings(stats_reporting_enabled=enabled, stats_report_interval_hours=interval,
                      stats_report_webhook_url=destination)
    admits = (notifications.reporting_participates(cfg)
              and interval > 0 and _has_reporting_webhook(cfg))
    assert admits is admitted


def test_scheduler_admission_honours_the_primary_discord_fallback():
    from core.scheduler import _has_reporting_webhook

    assert _has_reporting_webhook(AppSettings(discord_webhook_url=PRIMARY)) is True
    assert _has_reporting_webhook(AppSettings()) is False


# ── Configuration is the EFFECTIVE destination ───────────────────────────────

def test_reporting_is_configured_by_the_primary_discord_fallback():
    cfg = AppSettings(discord_webhook_url=PRIMARY)
    assert notifications.reporting_destination(cfg) == PRIMARY
    assert notifications.reporting_configured(cfg) is True


def test_dedicated_reporting_webhook_wins_over_the_fallback():
    cfg = AppSettings(discord_webhook_url=PRIMARY, stats_report_webhook_url=REPORT)
    assert notifications.reporting_destination(cfg) == REPORT


def test_clearing_the_dedicated_reporting_webhook_leaves_reporting_configured():
    cfg = AppSettings(discord_webhook_url=PRIMARY, stats_report_webhook_url=REPORT)
    cleared = cfg.model_copy(update={"stats_report_webhook_url": ""})

    assert notifications.reporting_configured(cleared) is True
    assert notifications.reporting_destination(cleared) == PRIMARY
    # Status only. Participation is never touched by a destination change.
    assert cleared.stats_reporting_enabled is True


def test_losing_the_last_fallback_makes_reporting_honestly_unconfigured():
    cfg = AppSettings(discord_webhook_url=PRIMARY)
    cleared = cfg.model_copy(update={"discord_webhook_url": ""})

    assert notifications.reporting_configured(cleared) is False
    assert notifications.discord_configured(cleared) is False
    assert (cleared.stats_reporting_enabled, cleared.discord_notifications_enabled) == (True, True)


def test_a_download_added_override_alone_is_not_a_configured_discord_section():
    """Configured means what the section's own delivery and Test require."""
    cfg = AppSettings(discord_webhook_added=ADDED)
    assert notifications.discord_configured(cfg) is False
    assert notifications.added_destination(cfg) == ADDED


# ── Verification is derived, bound to material, and durable ──────────────────

def _proven(cfg, subject):
    return notifications.record_verification_outcome(
        cfg, subject, notifications.verification_fingerprints(cfg)[subject], True)


def test_a_successful_test_establishes_verified_for_what_it_tested():
    cfg = AppSettings(discord_webhook_url=PRIMARY)
    assert notifications.verified(cfg, notifications.DISCORD_SUBJECT) is False
    proven = _proven(cfg, notifications.DISCORD_SUBJECT)
    assert notifications.verified(proven, notifications.DISCORD_SUBJECT) is True
    # One subject at a time: proving Discord proves nothing about reporting.
    assert notifications.verified(proven, notifications.REPORTING_SUBJECT) is False


def test_verification_survives_a_save_and_reload(config_path):
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    config.save_settings(proven)
    reloaded = config.load_settings()
    assert notifications.verified(reloaded, notifications.DISCORD_SUBJECT) is True


@pytest.mark.parametrize("change", [
    {"discord_webhook_url": "https://discord.com/api/webhooks/9/other"},
    {"discord_username": "Renamed"},
    {"discord_avatar_url": "https://example.com/new.png"},
])
def test_changing_material_the_test_exercised_downgrades_to_configured(change):
    """The Discord test posts to that webhook AS that identity, so all three are
    material to what it proved."""
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY, discord_username="Bot"),
                     notifications.DISCORD_SUBJECT)
    changed = proven.model_copy(update=change)

    assert notifications.discord_configured(changed) == bool(
        notifications.discord_destination(changed))
    assert notifications.verified(changed, notifications.DISCORD_SUBJECT) is False


@pytest.mark.parametrize("change", [
    {"discord_notify_added": False},
    {"discord_notify_update": False},
    {"discord_notifications_enabled": False},
    {"stats_report_interval_hours": 24},
    {"stats_report_window_hours": 720},
    {"update_check_interval_hours": 0},
])
def test_unrelated_changes_never_revoke_a_proof(change):
    """Event selection and participation do not change what the Test proved."""
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    assert notifications.verified(proven.model_copy(update=change),
                                  notifications.DISCORD_SUBJECT) is True


def test_a_failed_test_retires_the_proof_without_touching_configuration():
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    fingerprint = notifications.verification_fingerprints(proven)[notifications.DISCORD_SUBJECT]
    failed = notifications.record_verification_outcome(
        proven, notifications.DISCORD_SUBJECT, fingerprint, False)

    assert notifications.verified(failed, notifications.DISCORD_SUBJECT) is False
    assert notifications.discord_configured(failed) is True
    assert failed.discord_webhook_url == PRIMARY


def test_an_outcome_about_material_that_is_not_saved_changes_nothing():
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    assert notifications.record_verification_outcome(
        proven, notifications.DISCORD_SUBJECT, "a-fingerprint-of-something-else", False) is None
    assert notifications.verified(proven, notifications.DISCORD_SUBJECT) is True


def test_re_entering_a_previously_proven_value_does_not_restore_verified():
    """Evidence is RETIRED when it stops describing what is saved, so a proof
    cannot come back without a Test -- exactly as an integration namespace
    behaves."""
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    away = notifications.carried_verification(proven.model_copy(update={"discord_webhook_url": ADDED}))
    assert away.notification_verification == {}

    back = notifications.carried_verification(away.model_copy(update={"discord_webhook_url": PRIMARY}))
    assert notifications.verified(back, notifications.DISCORD_SUBJECT) is False


def test_carrying_verification_keeps_a_proof_an_unrelated_change_cannot_touch():
    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    carried = notifications.carried_verification(
        proven.model_copy(update={"discord_notify_error": False}))
    assert notifications.verified(carried, notifications.DISCORD_SUBJECT) is True


def test_reporting_verification_follows_the_effective_destination():
    """Proving the fallback proves the destination, so adopting that same URL as
    the dedicated webhook is not a different thing to have proven."""
    cfg = AppSettings(discord_webhook_url=PRIMARY)
    proven = _proven(cfg, notifications.REPORTING_SUBJECT)
    assert notifications.verified(proven, notifications.REPORTING_SUBJECT) is True

    same = proven.model_copy(update={"stats_report_webhook_url": PRIMARY})
    assert notifications.verified(same, notifications.REPORTING_SUBJECT) is True

    elsewhere = proven.model_copy(update={"stats_report_webhook_url": REPORT})
    assert notifications.verified(elsewhere, notifications.REPORTING_SUBJECT) is False


def test_verification_evidence_is_never_published():
    """A fingerprint is internal. What a client sees is four derived booleans."""
    from api.routes import _public_settings

    proven = _proven(AppSettings(discord_webhook_url=PRIMARY), notifications.DISCORD_SUBJECT)
    published = _public_settings(proven, ())

    assert "notification_verification" not in published
    assert proven.notification_verification  # it does exist, it is simply not served
    assert published["discord_notifications_verified"] is True
    assert published["discord_notifications_configured"] is True
    assert published["discord_webhook_url"] == "" and published["discord_webhook_url_configured"] is True
    assert PRIMARY not in json.dumps(published)
