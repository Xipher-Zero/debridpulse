"""Configuration metadata shared by modular integration definitions."""
from dataclasses import dataclass, replace
from typing import Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class IntegrationSettings(BaseModel):
    enabled: bool = True
    priority: int = 0
    options: dict = Field(default_factory=dict, repr=False)
    clear_secrets: list[str] = Field(default_factory=list, exclude=True)


class IntegrationGroupSettings(BaseModel):
    """One aggregate participation gate over a family of integrations.

    A group is identified by the ``presentation.status_group`` its members
    already declare, so nothing here holds a second list of who belongs to
    what. The gate is a gate and not a bulk editor: it never appears in, and
    never writes, a member's own ``integrations.<id>`` namespace.

    Defaults to enabled, so a configuration written before groups existed
    behaves exactly as it did.
    """
    enabled: bool = True


@dataclass(frozen=True)
class IntegrationEnvironment:
    repository: object
    download_root: str
    # Neutral application commands (pause/resume/cancel) an integration-owned
    # administration surface may forward operator intent to.
    commands: object = None
    # The neutral durable-input owner (``transfers.staged_input``). An
    # integration whose request class carries a large submitted payload borrows
    # it to READ that payload; it owns no part of its lifecycle.
    staged_input: object = None


@runtime_checkable
class IntegrationLifecycle(Protocol):
    """A managed integration component driven by the application lifecycle."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def maintain(self) -> None: ...


@runtime_checkable
class ManagedIntegration(Protocol):
    """An integration implementation that owns a lifecycle component (for
    example a managed daemon). Discovered generically by composition."""

    lifecycle: IntegrationLifecycle


@dataclass(frozen=True)
class ConfigurationApplication:
    """Outcome of applying canonical configuration to a native integration
    service.

    ``ok`` is proven truth, never optimism: a save whose native application did
    not land must report ``False`` so the operator is not told the service is
    configured when it is not.
    """
    ok: bool
    detail: str = ""
    failures: tuple[str, ...] = ()

    def public(self) -> dict:
        return {"applied": self.ok, "detail": self.detail, "failures": list(self.failures)}


@runtime_checkable
class ConfigurableIntegration(Protocol):
    """An integration whose canonical configuration must be pushed to its own
    out-of-core implementation before the integration is operational.

    "Out-of-core" is about the transfer core, not about ownership: such a
    service may be an operator-run endpoint or one DebridPulse bundles, starts
    and owns outright. The seam is identical either way, which is why nothing
    here names a service or assumes who runs it.

    Discovered generically by composition, exactly like the lifecycle and
    administration seams: the canonical namespace an implementation applies is
    named by the implementation itself, never by composition or the API.

    The invariant is ONE-WAY AUTHORITY, not operator-only invocation:

        DebridPulse canonical desired state -> native integration service

    and never the reverse. Service-side state is never imported back as
    canonical configuration, and this is not a synchronization loop.

    Application may be driven by EITHER an explicit canonical configuration
    mutation OR lifecycle convergence after the native service starts or
    restarts -- a service that came up with none of the canonical state must
    be given it, or it would run on stale or empty configuration until an
    operator happened to save again.
    """

    configuration_namespace: str

    async def apply_configuration(self) -> ConfigurationApplication: ...


@runtime_checkable
class AdministeredIntegration(Protocol):
    """An integration implementation exposing its own administration surface
    for integration-specific API/UI endpoints. Never used by neutral core
    runtime operations."""

    administration: object


@dataclass(frozen=True)
class IntegrationPresentation:
    """Safe, provider-owned presentation facts for neutral UI surfaces.

    Operational state is never inferred from configured state. Providers either
    expose a status endpoint or explicitly declare a local static state.
    """

    status_name: Optional[str] = None
    premium: bool = False
    status_endpoint: Optional[str] = None
    static_status: Optional[str] = None
    display_order: int = 100
    status_group: Optional[str] = None
    status_group_label: Optional[str] = None
    # Which acquisition tier this integration belongs to, and the operator-facing
    # heading for it. Neutral, integration-owned, and deliberately NOT derivable
    # from ``premium``: a named premium account service and an aggregate premium
    # acquisition family are both premium, and the status panel must still tell
    # them apart. A renderer groups by these; it never names an integration.
    # Tier ORDER is not declared here -- it falls out of ``display_order``, so
    # there is exactly one ordering authority.
    status_tier: Optional[str] = None
    status_tier_label: Optional[str] = None

    def public(self) -> dict:
        return {
            "status_name": self.status_name,
            "premium": self.premium,
            "status_endpoint": self.status_endpoint,
            "static_status": self.static_status,
            "display_order": self.display_order,
            "status_group": self.status_group,
            "status_group_label": self.status_group_label,
            "status_tier": self.status_tier,
            "status_tier_label": self.status_tier_label,
        }


@dataclass(frozen=True)
class IntegrationDefinition:
    id: str
    kind: str
    name: str
    options_model: type[BaseModel]
    factory: Callable
    secret_fields: frozenset[str] = frozenset()
    # Pre-canonical flat configuration names -> option names. Migration INPUT
    # only (``integrations.configuration.migrate_legacy_settings``).
    legacy_fields: tuple[tuple[str, str], ...] = ()
    # Optional integration-owned adjustment of option values that were taken
    # from legacy input: ``(legacy_options, existing_options) -> legacy_options``.
    legacy_upgrade: Optional[Callable[[dict, dict], dict]] = None
    ownership_fields: frozenset[str] = frozenset()
    required_options: frozenset[str] = frozenset()
    # Whether this integration participates before an operator has ever said
    # so. An integration that requires explicit opt-in declares ``False``; it
    # applies only when neither persisted state nor the request sets ``enabled``.
    default_enabled: bool = True
    # Every durable provider/executor identity this configuration owner covers.
    # A paired integration registers more than one implementation, so its
    # configuration owns more than one durable identity; leaving this empty
    # means the integration owns exactly its own id.
    durable_identities: frozenset[str] = frozenset()
    presentation: IntegrationPresentation = IntegrationPresentation()

    @property
    def owned_identities(self) -> frozenset[str]:
        """The identities configuration-ownership checks must fence against.

        Work is durably recorded under a provider id OR an executor id, so a
        paired integration whose executor carries a different identity would
        otherwise have its live executions invisible to the fence.
        """
        return self.durable_identities or frozenset({self.id})

    def build(self, settings: IntegrationSettings, environment: IntegrationEnvironment):
        """Construct this integration's implementation(s) from one namespace.

        A ``provider_executor`` integration is one product whose provider and
        executor halves are governed by a SINGLE canonical enabled state --
        there is no second boolean to keep synchronized. Its factory returns a
        tuple and the same ``enabled``/``priority`` is applied to every half.
        """
        built = self.factory(self.options_model(**settings.options), environment)
        implementations = built if isinstance(built, tuple) else (built,)
        for implementation in implementations:
            implementation.descriptor = replace(implementation.descriptor,
                enabled=implementation.descriptor.enabled and settings.enabled, priority=settings.priority)
        return built

    def public_options(self, options: dict):
        """Safe public projection of a namespace.

        An options model that owns nested secrets (for example a collection of
        credentialed servers) declares its own ``public()`` projection; models
        without one keep the plain dump. Top-level ``secret_fields`` are then
        redacted either way, so a secret can never reach a public surface.
        """
        model = self.options_model(**options)
        projector = getattr(model, "public", None)
        result = projector() if callable(projector) else model.model_dump()
        for key in self.secret_fields:
            result[key + "_configured"] = bool(result.get(key))
            result[key] = ""
        return result

    def configured(self, options: dict) -> bool:
        """Return persisted configuration presence without exposing secret data.

        An options model whose "configured" meaning is richer than "these
        fields are non-empty" -- for example one that needs at least one usable
        member of a collection -- declares its own ``configured()`` predicate.
        """
        model = self.options_model(**options)
        predicate = getattr(model, "configured", None)
        if callable(predicate):
            return bool(predicate())
        validated = model.model_dump()
        for key in self.required_options:
            value = validated.get(key)
            if isinstance(value, str):
                if not value.strip():
                    return False
            elif not value:
                return False
        return True
