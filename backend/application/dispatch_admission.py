"""Page/request-global LIVE facts sourced from the engine (DP 1.0.12 recovery
leveling).

``disabled_provider_ids`` is a durable-at-composition-time snapshot used by
the pre-existing candidate-switch-actionability feature
(``api.operational_downloads._disabled_provider_ids``).

``capacity_only_blocked_ids`` (Section 9) is different in kind: it is not a
fact this module derives or mirrors. It is a plain passthrough to
``transfers.convergence_engine.TransferEngine.capacity_only_blocked_ids`` --
the execution-admission owner's OWN positive record of which artifact ids the
REAL ``_dispatch()`` most recently reached the capacity gate for (having
already passed target validation, candidate expiry, existing-payload,
``executor.prepare()`` with no InputRequirement, and storage/pause
admission) and was rejected there. Presentation never re-derives any of
those gates; it only reads this one set.
"""
from __future__ import annotations


def disabled_provider_ids(engine) -> frozenset[str]:
    """Durable-at-composition-time provider-enablement snapshot.

    ``descriptor.enabled`` is computed once at composition time from
    AppSettings (e.g. ``bool(api_key)``) and held on the provider object the
    registry carries -- no extra DB round-trip, no per-artifact live check.
    """
    registry = getattr(engine, "registry", None)
    providers = getattr(registry, "providers", None)
    if not providers:
        return frozenset()
    return frozenset(
        provider_id
        for provider_id, provider in providers.items()
        if not getattr(getattr(provider, "descriptor", None), "enabled", True)
    )


def capacity_only_blocked_ids(engine) -> frozenset[int]:
    """The execution-admission owner's positive capacity-only-blocked set
    (Section 9). Defaults to empty when unavailable (e.g. a minimal test
    double with no engine attached) -- capacity wait is never claimed
    without the real dispatch path's own say-so."""
    getter = getattr(engine, "capacity_only_blocked_ids", None)
    return getter() if callable(getter) else frozenset()
