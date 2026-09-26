"""Application notification boundary for DebridPulse.

ONE owner for the four facts every notification surface needs, so the runtime,
the scheduler, the Test routes and the Settings status projection can never
disagree about them:

* **participation** -- whether a section takes part at all;
* **the effective destination** -- including Statistics Reporting's existing
  fallback to the primary Discord webhook, which used to be re-derived
  separately in ``services.stats`` and ``core.scheduler``;
* **configuration** -- whether the destination the delivery logic would
  actually use exists;
* **verification** -- durable evidence that a Test exercised exactly the
  material currently saved.

Participation and configuration are deliberately SEPARATE facts. Disabling a
section stops its delivery and erases nothing: the stored webhooks, identity,
event choices, interval and window all survive, the status keeps reporting the
truth about them, and a Test can still be run against them.

Verification reuses the application's existing evidence pattern
(``integrations.definition``): the durable truth is a fingerprint of the
material a Test covered, and ``Verified`` is DERIVED by asking whether that
fingerprint still describes what is saved. There is no second subsystem and no
boolean anybody has to remember to clear. There is also no draft/proof carrier
here, because a notification Test always exercises the SAVED configuration --
every field on the page commits at its own boundary before the action runs.
"""
from __future__ import annotations

from core.config import get_settings
from integrations.definition import VerificationSubject, verification_fingerprint
from services.notifications import NotificationService as DiscordNotificationClient

# The two independently testable notification subjects.
DISCORD_SUBJECT = "discord"
REPORTING_SUBJECT = "stats_report"


def _text(cfg, field: str) -> str:
    return str(getattr(cfg, field, "") or "").strip()


# ── Participation ─────────────────────────────────────────────────────────────

def discord_participates(cfg) -> bool:
    """Whether Discord Notifications take part in delivery at all."""
    return bool(getattr(cfg, "discord_notifications_enabled", True))


def reporting_participates(cfg) -> bool:
    """Whether Statistics Reporting takes part in SCHEDULED delivery at all.

    The report interval is a separate cadence fact and is not consulted here;
    the scheduler applies both, because "switched off" and "no automatic
    cadence" are different statements about different things.
    """
    return bool(getattr(cfg, "stats_reporting_enabled", True))


# ── Effective destinations ────────────────────────────────────────────────────

def discord_destination(cfg) -> str:
    """The primary Discord destination."""
    return _text(cfg, "discord_webhook_url")


def added_destination(cfg) -> str:
    """The Download Added destination: the dedicated override, or the primary."""
    return _text(cfg, "discord_webhook_added") or discord_destination(cfg)


def reporting_destination(cfg) -> str:
    """The statistics-report destination: the dedicated webhook, or the primary.

    This IS the fallback the delivery path uses, declared once so a status can
    never claim something the delivery would not do.
    """
    return _text(cfg, "stats_report_webhook_url") or discord_destination(cfg)


def report_window_hours(cfg) -> int:
    """How much recent activity a report covers, bounded to its saved range.

    One owner, so a scheduled report and an operator-triggered one can never
    summarise different periods. A value that cannot be read as a number falls
    back to the field's own default rather than failing the delivery.
    """
    try:
        hours = int(getattr(cfg, "stats_report_window_hours", 24))
    except (TypeError, ValueError):
        hours = 24
    return max(1, min(8760, hours))


# ── Configuration ─────────────────────────────────────────────────────────────

def discord_configured(cfg) -> bool:
    """Discord has the destination its delivery and its Test both require."""
    return bool(discord_destination(cfg))


def reporting_configured(cfg) -> bool:
    """Reporting has an EFFECTIVE destination -- dedicated, or by fallback.

    Clearing the dedicated reporting webhook therefore leaves reporting
    configured while the primary Discord webhook remains; clearing the last
    one leaves it honestly unconfigured, without touching either section's
    participation.
    """
    return bool(reporting_destination(cfg))


# ── Verification ──────────────────────────────────────────────────────────────

def verification_subjects(cfg) -> tuple[VerificationSubject, ...]:
    """What each Test actually exercises -- no more, and no less.

    The Discord test posts to the primary webhook AS the configured identity,
    so the identity is part of what it proved. The report test posts a report
    body to the effective reporting destination and carries no identity of its
    own, so only that destination is material to it.
    """
    return (
        VerificationSubject(DISCORD_SUBJECT, {
            "webhook_url": discord_destination(cfg),
            "username": _text(cfg, "discord_username"),
            "avatar_url": _text(cfg, "discord_avatar_url"),
        }),
        VerificationSubject(REPORTING_SUBJECT, {
            "webhook_url": reporting_destination(cfg),
        }),
    )


def verification_fingerprints(cfg) -> dict[str, str]:
    return {subject.id: verification_fingerprint(subject.material)
            for subject in verification_subjects(cfg)}


def _evidence(cfg) -> dict:
    stored = getattr(cfg, "notification_verification", None)
    return dict(stored) if isinstance(stored, dict) else {}


def verified(cfg, subject: str) -> bool:
    """Derived: stored evidence still describes the saved material."""
    current = verification_fingerprints(cfg).get(subject)
    return bool(current) and _evidence(cfg).get(subject) == current


def carried_verification(settings):
    """Retire evidence that no longer describes the configuration being saved.

    The same rule ``integrations.configuration._carried_verification`` applies
    to a namespace: a verification-relevant change drops the proof, an
    unrelated change cannot, and re-entering a previously proven value does
    NOT silently restore ``Verified`` -- the operator tests again.

    Returns the settings unchanged when nothing had to be retired.
    """
    stored = _evidence(settings)
    if not stored:
        return settings
    current = verification_fingerprints(settings)
    carried = {subject: fingerprint for subject, fingerprint in stored.items()
               if current.get(subject) == fingerprint}
    if carried == stored:
        return settings
    return settings.model_copy(update={"notification_verification": carried})


def record_verification_outcome(settings, subject: str, fingerprint: str, ok: bool):
    """Commit or retire evidence for a subject of the CURRENT SAVED material.

    A Test whose material is not what is saved matches no subject, so it
    neither verifies nor revokes anything. A Test of exactly the saved material
    does both: success is durable proof, and failure retires a proof that has
    stopped being true rather than leaving the card claiming ``Verified``.

    Returns the updated settings, or ``None`` when nothing changed.
    """
    if verification_fingerprints(settings).get(subject) != fingerprint:
        return None
    evidence = _evidence(settings)
    if ok:
        evidence[subject] = str(fingerprint)
    else:
        evidence.pop(subject, None)
    if evidence == _evidence(settings):
        return None
    return settings.model_copy(update={"notification_verification": evidence})


def notification_state(cfg) -> dict:
    """The public, secret-free projection both cards' header status reads.

    Four derived booleans and nothing else: no webhook, no fingerprint, and no
    second definition of "configured" for a browser to keep.
    """
    return {
        "discord_notifications_configured": discord_configured(cfg),
        "discord_notifications_verified": verified(cfg, DISCORD_SUBJECT),
        "stats_reporting_configured": reporting_configured(cfg),
        "stats_reporting_verified": verified(cfg, REPORTING_SUBJECT),
    }


class NotificationService:
    def client(self) -> DiscordNotificationClient:
        """Return a concrete client; empty URLs intentionally no-op in the client.

        This is the ONE runtime participation gate for Discord delivery: a
        disabled section hands out a client with no destinations, so every
        event path, and the update-available notice, stop delivering without
        any of them holding a second opinion about it. An operational Test
        constructs the client directly with the destination it is testing, and
        is therefore deliberately unaffected.
        """
        cfg = get_settings()
        if not discord_participates(cfg):
            return DiscordNotificationClient(webhook_url="", added_webhook_url="")
        return DiscordNotificationClient(
            webhook_url=discord_destination(cfg),
            added_webhook_url=_text(cfg, "discord_webhook_added"),
        )
