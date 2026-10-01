"""Real-Debrid registration and provider-owned settings schema."""
from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition, IntegrationPresentation, VerificationSubject
from providers.realdebrid.rate_limit import SERVICE_LIMIT_PER_MINUTE


class RealDebridOptions(BaseModel):
    # The user-bound open-source client credential and its refresh token, as
    # Real-Debrid's device authorization issued them. Never typed by an operator.
    client_id: str = Field(default="", repr=False)
    client_secret: str = Field(default="", repr=False)
    refresh_token: str = Field(default="", repr=False)
    # Real-Debrid documents 250 requests/minute, refused ones included; the
    # default leaves headroom and the ceiling is the service's own.
    rate_limit_per_minute: int = Field(default=240, ge=1, le=SERVICE_LIMIT_PER_MINUTE)


def canonical_options(settings) -> RealDebridOptions:
    """Decode the canonical ``integrations.realdebrid`` namespace of ``settings``."""
    entry = (getattr(settings, "integrations", None) or {}).get("realdebrid")
    return RealDebridOptions(**(getattr(entry, "options", None) or {}))


def credential_material(options: RealDebridOptions) -> dict:
    """What the Real-Debrid Test proves: the authorization grant itself.

    The user-bound client credential identifies the grant. The refresh token
    is deliberately not part of it -- refreshing an access token is the
    protocol working, not a configuration change, so it must never revoke a
    proof -- and the local rate limit decides nothing about authentication."""
    return {"client_id": str(options.client_id or ""), "client_secret": str(options.client_secret or "")}


def _verification_subjects(options: RealDebridOptions):
    if not (options.client_id and options.client_secret and options.refresh_token):
        return ()
    return (VerificationSubject("credential", credential_material(options)),)


def build(options, environment):
    from integrations.runtime_state import ProviderRuntimeStateStore
    from providers.realdebrid.admin import persist_refreshed_credential
    from providers.realdebrid.client import Credential, RealDebridService
    from providers.realdebrid.host_runtime import RealDebridHostMaintenance
    from providers.realdebrid.provider import RealDebridProvider

    credential = Credential(options.client_id, options.client_secret, options.refresh_token)
    client = RealDebridService(credential if credential.usable else None,
                               rate_limit_per_minute=options.rate_limit_per_minute,
                               on_refresh=persist_refreshed_credential)
    provider = RealDebridProvider(client)
    commands = getattr(environment, "commands", None)
    # Host inventory maintenance reaches the application through the generic
    # integration lifecycle seam; composition names no provider.
    provider.lifecycle = RealDebridHostMaintenance(
        provider, ProviderRuntimeStateStore(),
        notify=getattr(commands, "notify_applicability_changed", None))
    return provider


definition = IntegrationDefinition(
    "realdebrid", "provider", "Real-Debrid", RealDebridOptions, build,
    secret_fields=frozenset({"client_id", "client_secret", "refresh_token"}),
    ownership_fields=frozenset({"client_id"}),
    required_options=frozenset({"client_id", "client_secret", "refresh_token"}),
    # Participates only once an operator turns it on, so an install that never
    # uses Real-Debrid is not reported as an unconfigured provider.
    default_enabled=False,
    verification_subjects=_verification_subjects,
    presentation=IntegrationPresentation(
        status_name="Real-Debrid",
        premium=True,
        status_endpoint="/integration-status/realdebrid",
        display_order=11,
        status_tier="premium_service",
        status_tier_label="Premium Services",
    ),
)
