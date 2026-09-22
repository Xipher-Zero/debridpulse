"""Read-only legacy view of ``GET /settings`` for pre-canonical API clients.

``integrations.<id>``, ``transfer_policy`` and ``execution_runtime_limits`` are
the settings authorities. This module derives the historical flat names from
them, at response time only, so an external reader that still expects
``aria2_split`` or ``max_concurrent_downloads`` keeps working.

It is compatibility *output*, not an authority: nothing here is persisted,
nothing reads it back, ``PUT /settings`` ignores the names it emits, and the
DebridPulse frontend never consumes it. The name tables are the migration-input
tables themselves, so the two directions cannot drift apart.
"""
from transfers.runtime_limits import LEGACY_INPUT_FIELDS as RUNTIME_LIMIT_FIELDS
from transfers.settings import LEGACY_INPUT_FIELDS as TRANSFER_POLICY_FIELDS

# Historical duplicate names that were never migration input (the flat
# concurrency setting used to be mirrored under two names).
_OUTPUT_ONLY_ALIASES = {"aria2_max_active_downloads": "max_concurrent_executions"}


def legacy_settings_projection(settings, definitions) -> dict:
    view: dict = {}
    for legacy, canonical in TRANSFER_POLICY_FIELDS.items():
        view[legacy] = getattr(settings.transfer_policy, canonical)
    for legacy, canonical in _OUTPUT_ONLY_ALIASES.items():
        view[legacy] = getattr(settings.transfer_policy, canonical)
    for legacy, canonical in RUNTIME_LIMIT_FIELDS.items():
        view[legacy] = getattr(settings.execution_runtime_limits, canonical)
    for definition in definitions:
        entry = settings.integrations.get(definition.id)
        if entry is None:
            continue
        options = definition.public_options(entry.options)
        for legacy, option in definition.legacy_fields:
            if option in options:
                view[legacy] = options[option]
            if option in definition.secret_fields:
                # Secret-safe: the value is redacted and only its presence is exposed.
                view[legacy + "_configured"] = options[option + "_configured"]
    return view
