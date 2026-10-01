"""Multimeta registration.

Nothing to tune: every bound it applies to a descriptor is an internal safety
limit (``providers.multimeta.metalink``), never operator policy, and every
choice about the sources a descriptor names belongs to the existing lifecycle.
"""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class MultimetaOptions(BaseModel):
    """Multimeta deliberately has no provider-specific tuning."""


def build(options, environment):
    from providers.multimeta.provider import MultimetaProvider
    return MultimetaProvider(staged_input=getattr(environment, "staged_input", None))


definition = IntegrationDefinition(
    # ``multimeta`` is the durable provider identity and never changes; the
    # name is the ONE operator-facing label, read both by the Provider Status
    # panel and by the transfer-list provider badge.
    "multimeta", "provider", "Multimeta", MultimetaOptions, build,
    presentation=IntegrationPresentation(
        status_name="Multimeta",
        static_status="healthy",
        # The sixth member of the Network Sources group, inside the reserved
        # STANDARD band documented on ``general_http``.
        display_order=915,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
    ),
)
