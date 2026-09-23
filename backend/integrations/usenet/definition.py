"""Usenet registration: ONE canonical integration owning provider + executor.

Usenet is a one-to-one provider/executor pairing, so `integrations.usenet` is
the single canonical namespace and `integrations.usenet.enabled` is the single
canonical enable state governing BOTH halves. There is no second namespace and
no second boolean to keep synchronized.

This definition lives under ``integrations/`` rather than ``providers/`` or
``executors/`` on purpose: it is the integration-level artifact that PAIRS the
two halves, so it is the only module that legitimately imports both. The
architectural boundary each tree enforces -- no provider imports an executor,
no executor imports a provider -- therefore stays intact.
"""
from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition, IntegrationPresentation

def usable_servers(options: dict) -> int:
    """News servers that could actually acquire: enabled, addressable, and
    allowed at least one connection.

    A server with zero connections cannot acquire anything, so counting it
    would report Usenet ready when it is not. Configurations written before
    the floor existed can still carry 0, so this is checked here rather than
    trusted from validation alone.
    """
    def field(item, name, default):
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    servers = (options or {}).get("servers") or []
    usable = 0
    for item in servers:
        if not field(item, "enabled", True):
            continue
        if not str(field(item, "host", "") or "").strip():
            continue
        try:
            if int(field(item, "connections", 0) or 0) < MIN_CONNECTIONS:
                continue
        except (TypeError, ValueError):
            continue
        usable += 1
    return usable


# SABnzbd 5.1.3 `config.py ConfigServer` allows 0..500. DebridPulse's floor is
# 1, deliberately narrower: 0 is how SAB switches a server off, and DP already
# has an explicit per-server Enable control for that. A DP server left at 0
# would read as enabled while the native side cannot open a single connection
# to it -- configured, testable, and incapable of acquiring.
MIN_CONNECTIONS, MAX_CONNECTIONS = 1, 500
MIN_PRIORITY, MAX_PRIORITY = 0, 99
# SAB sorts servers ascending by priority: 0 is the highest priority.
PRIORITY_HELP = "Lower values have priority."

# Acquisition tuning bounds, each characterized against the bundled SABnzbd
# 5.1.3 (source tree plus live probes). DebridPulse's range is never WIDER than
# the native one, so a persisted canonical value can never be silently rewritten
# by the service into something the operator did not choose.
#
#   article cache   `misc.cache_limit`, an OptionStr in K/M/G notation with no
#                   range of its own; the running cache is clamped to
#                   min(value, 4 GiB on 64-bit, available memory). 0 = no cache.
#   direct write    `misc.direct_write`, OptionBool, native default True.
#   acquisition     `misc.max_art_tries`, OptionNumber, default 3, native
#   retries         minval 2 (a smaller request is clamped up), no maxval.
#   articles/req    `servers.<id>.pipelining_requests`, default 2, clamps 1..20.
#   server timeout  `servers.<id>.timeout` seconds, default 60, clamps 20..240.
#
# All of them apply live: cache_limit and direct_write through registered
# option callbacks, max_art_tries because it is read per attempt, and the
# per-server values because a server write re-initialises that server.
MIN_ARTICLE_CACHE_MB, MAX_ARTICLE_CACHE_MB = 0, 4096
MIN_ACQUISITION_RETRIES, MAX_ACQUISITION_RETRIES = 2, 25
MIN_ARTICLES_PER_REQUEST, MAX_ARTICLES_PER_REQUEST = 1, 20
MIN_SERVER_TIMEOUT_SECONDS, MAX_SERVER_TIMEOUT_SECONDS = 20, 240


def _new_server_id() -> str:
    return uuid4().hex


class UsenetServer(BaseModel):
    """One configured NNTP server.

    Fields are exactly those SABnzbd's server configuration supports. There is
    deliberately no API/API-key field: SAB's NNTP server authentication is
    username/password only (characterized against SABnzbd 5.1.3), so exposing
    one would be a dead setting.
    """
    # Canonical, stable record identity. It is what a per-server Save, Test or
    # Remove addresses, so editing one card can never disturb another. Never a
    # list index and never derived from the host: two servers may share a host.
    id: str = Field(default_factory=_new_server_id)
    host: str = ""
    port: int = Field(default=563, ge=1, le=65535)
    ssl: bool = True
    username: str = ""
    password: str = Field(default="", repr=False)
    connections: int = Field(default=8, ge=MIN_CONNECTIONS, le=MAX_CONNECTIONS)
    priority: int = Field(default=0, ge=MIN_PRIORITY, le=MAX_PRIORITY)
    # How many articles are requested from this server without waiting for each
    # reply, and how long this server may take to answer before the connection
    # is treated as failed. Both are per-server acquisition behaviour.
    articles_per_request: int = Field(default=2, ge=MIN_ARTICLES_PER_REQUEST,
                                      le=MAX_ARTICLES_PER_REQUEST)
    timeout_seconds: int = Field(default=60, ge=MIN_SERVER_TIMEOUT_SECONDS,
                                 le=MAX_SERVER_TIMEOUT_SECONDS)
    enabled: bool = True
    # Operator-facing display name. Empty means "derive from host"; a non-empty
    # value is an explicit override that survives later host edits.
    display_name: str = ""


class UsenetOptions(BaseModel):
    """Everything an operator configures for Usenet.

    Deliberately absent: any endpoint or credential for the acquisition
    service itself. That service is bundled, DebridPulse-private and
    DebridPulse-owned; its endpoint and control-plane key are runtime state,
    never settings, so there is nothing here for an operator to point at.
    """
    # --- DebridPulse -> acquisition-service communication ---
    # How long DebridPulse waits for its own download service to answer. This is
    # a service-communication timeout, never a news-server timeout.
    operation_timeout_seconds: int = Field(default=30, ge=5, le=300)
    # --- executor-wide acquisition tuning ---
    article_cache_megabytes: int = Field(default=1024, ge=MIN_ARTICLE_CACHE_MB,
                                         le=MAX_ARTICLE_CACHE_MB)
    direct_write: bool = True
    # Native news-server acquisition retry per article. Emphatically NOT the
    # DebridPulse transfer retry count, core recovery attempts, or a provider
    # retry count -- those are universal transfer policy and live elsewhere.
    max_acquisition_retries: int = Field(default=3, ge=MIN_ACQUISITION_RETRIES,
                                         le=MAX_ACQUISITION_RETRIES)
    # --- NNTP server collection ---
    servers: list[UsenetServer] = Field(default_factory=list)

    def configured(self) -> bool:
        """Usenet is configured only when it can actually acquire.

        The bundled service always exists, so its presence is never evidence
        of configuration: at least one enabled, addressable news server must be
        configured. Enabled with zero servers is Enabled + Unconfigured.
        """
        return usable_servers(self.model_dump()) > 0

    def public(self) -> dict:
        """Projection with every per-server credential removed.

        Server passwords are nested secrets: the plain model dump would carry
        them onto public settings surfaces, so this projection replaces each
        with a presence flag only.
        """
        result = self.model_dump()
        result["servers"] = [
            {**server, "password": "", "password_configured": bool(server.get("password"))}
            for server in result.get("servers", [])
        ]
        return result


def build(options: UsenetOptions, environment):
    """Build BOTH halves of the pairing from one canonical configuration.

    The acquisition service is internal: its endpoint and credential come from
    the DebridPulse-owned runtime, never from ``options``.
    """
    from executors.sabnzbd.admin import SabnzbdAdministration
    from executors.sabnzbd.client import SabEndpoint, SabnzbdClient
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from executors.sabnzbd import runtime as service_runtime, topology
    from providers.usenet.provider import UsenetProvider

    provider = UsenetProvider()
    root = environment.download_root
    service_runtime.runtime.configure(service_runtime.UsenetRuntimeConfiguration(
        download_root=root,
        operation_timeout_seconds=options.operation_timeout_seconds,
    ))
    client = SabnzbdClient(SabEndpoint(
        base_url=service_runtime.service_url(),
        api_key=service_runtime.internal_api_key(),
        timeout_seconds=options.operation_timeout_seconds,
    ))
    executor = SabnzbdExecutor(
        client,
        SabnzbdConfiguration(
            local_root=root,
            working_directory=topology.working_root(root),
            complete_directory=topology.complete_root(root),
            # The internal control-plane key is redacted from every diagnostic.
            secrets=(service_runtime.internal_api_key(),),
        ),
        environment.repository.authorize_execution,
    )
    # The administration surface is BOTH the configuration applier and the
    # managed lifecycle component; composition discovers each generically.
    administration = SabnzbdAdministration(client, options, root,
                                           service_runtime.runtime, executor,
                                           repository=environment.repository)
    executor.administration = administration
    executor.lifecycle = administration
    return provider, executor


definition = IntegrationDefinition(
    "usenet", "provider_executor", "Usenet", UsenetOptions, build,
    # No operator-owned secret: the service credential is runtime state.
    secret_fields=frozenset(),
    # Changing the news-server set can invalidate an owned native acquisition.
    ownership_fields=frozenset({"servers"}),
    # "Configured" is decided by `configured_options` below, not by presence
    # of a field: an internal service existing is never configuration.
    required_options=frozenset(),
    # Usenet is off until an operator turns it on: enabling it is what makes
    # both the provider and the SAB-backed executor participate.
    default_enabled=False,
    # The provider and the SAB-backed executor both record durable work, so
    # configuration ownership is fenced against both identities.
    durable_identities=frozenset({"usenet", "sabnzbd"}),
    presentation=IntegrationPresentation(
        status_name="Usenet",
        premium=True,
        # A real readiness check: an enabled-but-unconfigured Usenet integration
        # must never render as ready, which a static status could not express.
        status_endpoint="/integration-status/usenet",
        # Usenet is an aggregate premium acquisition FAMILY, not a named premium
        # account service: the operator configures news servers, and the panel
        # reports one Usenet readiness for all of them.
        status_tier="premium_family",
        status_tier_label="Premium",
        # Ordered after the named premium services and before the general
        # families; tier order is derived from this, so nothing else declares it.
        display_order=20,
    ),
)
