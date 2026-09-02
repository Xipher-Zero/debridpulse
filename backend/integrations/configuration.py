"""Namespace persistence and legacy input translation driven by definitions."""
from integrations.definition import IntegrationSettings
from transfers.settings import normalize_transfer_settings


def normalize_settings(settings, definitions, *, previous=None, supplied_fields=None, clear_legacy_secrets=()):
    namespaces = dict(getattr(previous, "integrations", {}) or {})
    namespaces.update(dict(settings.integrations or {}))
    translated = {}
    for definition in definitions:
        raw = namespaces.get(definition.id)
        entry = raw if isinstance(raw, IntegrationSettings) else IntegrationSettings(**(raw or {}))
        older = getattr(previous, "integrations", {}).get(definition.id) if previous is not None else None
        old_options = older.options if isinstance(older, IntegrationSettings) else dict((older or {}).get("options", {}))
        options = {**old_options, **entry.options}
        clears = set(entry.clear_secrets)
        for legacy, option in definition.legacy_fields:
            if raw is None or (supplied_fields is not None and legacy in supplied_fields):
                options[option] = getattr(settings, legacy)
            if legacy in clear_legacy_secrets and option in definition.secret_fields:
                clears.add(option)
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
        for legacy, option in definition.legacy_fields:
            translated[legacy] = validated[option]
    return normalize_transfer_settings(settings.model_copy(update={**translated, "integrations": namespaces}),
        previous=previous, supplied_fields=supplied_fields)


def public_integrations(settings, definitions):
    known = {definition.id: definition for definition in definitions}
    result = {}
    for identity, entry in settings.integrations.items():
        definition = known.get(identity)
        result[identity] = {"enabled": entry.enabled, "priority": entry.priority,
                            "name": definition.name if definition else None,
                            "options": definition.public_options(entry.options) if definition else {}}
    return result
