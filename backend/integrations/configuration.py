"""Canonical namespace persistence and the one legacy-input migration boundary.

``integrations.<id>``, ``transfer_policy`` and ``execution_runtime_limits`` are
the only runtime and persisted authorities for the settings they own.

Pre-canonical flat configuration names (``aria2_*``, ``max_concurrent_downloads``,
``alldebrid_api_key``, ...) exist in exactly one place at runtime: as the raw
input of ``migrate_legacy_settings``, which runs once while a persisted file is
loaded and folds them into the canonical namespaces. Nothing else reads them, and
nothing regenerates them.
"""
from integrations.definition import IntegrationSettings
from transfers.runtime_limits import ExecutionRuntimeLimits, LEGACY_INPUT_FIELDS as RUNTIME_LIMIT_LEGACY_FIELDS
from transfers.settings import TransferSettings, LEGACY_INPUT_FIELDS as TRANSFER_POLICY_LEGACY_FIELDS


def clamp_to_model_bounds(model, values: dict) -> dict:
    """Clamp numeric ``values`` into the ``ge``/``le`` bounds the model declares.

    Bounds are read from the model itself, so a schema bound is stated once and
    never mirrored in a second hand-maintained table.
    """
    clamped = dict(values)
    for name, value in values.items():
        field = model.model_fields.get(name)
        if field is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        for bound in field.metadata:
            if getattr(bound, "ge", None) is not None:
                value = max(bound.ge, value)
            if getattr(bound, "le", None) is not None:
                value = min(bound.le, value)
        clamped[name] = value
    return clamped


def _fill_from_legacy(namespace: dict, raw: dict, mapping, model) -> bool:
    """Take legacy values for options the canonical namespace does not carry.

    A value already present in the canonical namespace always wins; a legacy
    value is only ever the source for something canonical state has no opinion on.
    """
    filled = {option: raw[legacy] for legacy, option in mapping if legacy in raw and option not in namespace}
    namespace.update(clamp_to_model_bounds(model, filled))
    return bool(filled)


def migrate_legacy_settings(raw: dict, definitions) -> bool:
    """Fold pre-canonical flat keys of a raw persisted mapping into the
    canonical namespaces, in place, and drop the flat keys.

    This is the single translation boundary: legacy input in, canonical
    namespaces out. It returns ``True`` when any legacy key was consumed, so the
    caller persists the canonical representation and the flat keys are never
    written back.
    """
    consumed = False
    integrations = {key: dict(value) for key, value in (raw.get("integrations") or {}).items()}
    for definition in definitions:
        names = [legacy for legacy, _option in definition.legacy_fields if legacy in raw]
        if not names:
            continue
        entry = integrations.setdefault(definition.id, {})
        existing = dict(entry.get("options") or {})
        legacy_options = {option: raw[legacy] for legacy, option in definition.legacy_fields
                          if legacy in raw and option not in existing}
        if definition.legacy_upgrade is not None:
            legacy_options = definition.legacy_upgrade(legacy_options, existing)
        existing.update(clamp_to_model_bounds(definition.options_model, legacy_options))
        entry["options"] = existing
        for legacy in names:
            del raw[legacy]
        consumed = True
    if integrations:
        raw["integrations"] = integrations

    for key, mapping, model in (
        ("transfer_policy", TRANSFER_POLICY_LEGACY_FIELDS, TransferSettings),
        ("execution_runtime_limits", RUNTIME_LIMIT_LEGACY_FIELDS, ExecutionRuntimeLimits),
    ):
        names = [legacy for legacy in mapping if legacy in raw]
        if not names:
            continue
        namespace = dict(raw.get(key) or {})
        _fill_from_legacy(namespace, raw, mapping.items(), model)
        raw[key] = namespace
        for legacy in names:
            del raw[legacy]
        consumed = True
    return consumed


def clamp_persisted_namespaces(raw: dict, definitions) -> list[str]:
    """Clamp out-of-range numeric values of the persisted canonical namespaces,
    in place, so a hand-edited or older-schema value can never block startup.

    Returns the dotted names of the corrected fields (never their values, which
    may sit next to secrets).
    """
    corrected: list[str] = []

    def clamp(namespace, model, label):
        fixed = clamp_to_model_bounds(model, namespace)
        corrected.extend(f"{label}.{name}" for name in fixed if fixed[name] != namespace[name])
        return fixed

    integrations = raw.get("integrations")
    for definition in definitions:
        entry = integrations.get(definition.id) if isinstance(integrations, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("options"), dict):
            entry["options"] = clamp(entry["options"], definition.options_model, f"integrations.{definition.id}")
    for key, model in (("transfer_policy", TransferSettings), ("execution_runtime_limits", ExecutionRuntimeLimits)):
        if isinstance(raw.get(key), dict):
            raw[key] = clamp(raw[key], model, key)
    return corrected


def _merged_namespace(model, previous, supplied):
    """Partial namespace update: only fields the caller actually set replace
    the previous values; every other field is preserved."""
    options = previous.model_dump() if previous is not None else {}
    if supplied is not None:
        options.update(supplied.model_dump(exclude_unset=True))
    return model(**options)


def normalize_settings(settings, definitions, *, previous=None):
    """Validate and complete the canonical namespaces of ``settings``.

    Merges partial namespace updates onto ``previous``, preserves stored
    secrets unless their explicit clear was requested, retains unknown
    integrations, and validates every namespace against its owner's schema.
    Legacy flat input is not accepted here -- see ``migrate_legacy_settings``.
    """
    namespaces = dict(getattr(previous, "integrations", {}) or {})
    namespaces.update(dict(settings.integrations or {}))
    for definition in definitions:
        raw = namespaces.get(definition.id)
        entry = raw if isinstance(raw, IntegrationSettings) else IntegrationSettings(**(raw or {}))
        older = getattr(previous, "integrations", {}).get(definition.id) if previous is not None else None
        old_options = older.options if isinstance(older, IntegrationSettings) else dict((older or {}).get("options", {}))
        options = {**old_options, **entry.options}
        clears = set(entry.clear_secrets)
        unknown_clears = clears - definition.secret_fields
        if unknown_clears:
            raise ValueError("Unknown integration secret clear request")
        for secret in definition.secret_fields:
            if secret in clears:
                options[secret] = ""
            elif not options.get(secret) and old_options.get(secret):
                options[secret] = old_options[secret]
        validated = definition.options_model(**options).model_dump()
        enabled = older.enabled if isinstance(older, IntegrationSettings) and "enabled" not in entry.model_fields_set else entry.enabled
        priority = older.priority if isinstance(older, IntegrationSettings) and "priority" not in entry.model_fields_set else entry.priority
        namespaces[definition.id] = IntegrationSettings(enabled=enabled, priority=priority, options=validated)
    return settings.model_copy(update={
        "integrations": namespaces,
        "transfer_policy": _merged_namespace(
            TransferSettings, getattr(previous, "transfer_policy", None), settings.transfer_policy),
        "execution_runtime_limits": _merged_namespace(
            ExecutionRuntimeLimits, getattr(previous, "execution_runtime_limits", None), settings.execution_runtime_limits),
    })


def public_integrations(settings, definitions):
    known = {definition.id: definition for definition in definitions}
    result = {}
    for identity, entry in settings.integrations.items():
        definition = known.get(identity)
        result[identity] = {
            "enabled": entry.enabled,
            "priority": entry.priority,
            "name": definition.name if definition else None,
            "kind": definition.kind if definition else None,
            "configured": definition.configured(entry.options) if definition else False,
            "presentation": definition.presentation.public() if definition else {},
            "options": definition.public_options(entry.options) if definition else {},
        }
    return result
