"""Canonical namespace persistence and the one legacy-input migration boundary.

``integrations.<id>``, ``transfer_policy`` and ``execution_runtime_limits`` are
the only runtime and persisted authorities for the settings they own.

Pre-canonical flat configuration names (``aria2_*``, ``max_concurrent_downloads``,
``alldebrid_api_key``, ...) exist in exactly one place at runtime: as the raw
input of ``migrate_legacy_settings``, which runs once while a persisted file is
loaded and folds them into the canonical namespaces. Nothing else reads them, and
nothing regenerates them.
"""
from integrations.definition import (
    IntegrationGroupSettings, IntegrationSettings, supersede_verification_proofs,
    verification_proves,
)
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


# ── Integration groups ───────────────────────────────────────────────────────
#
# A group is an aggregate participation gate over a family of integrations. Its
# identity and operator-facing label are read from the members' own
# ``presentation.status_group`` / ``status_group_label``, so the membership of a
# group is stated exactly once -- by each member -- and never duplicated here,
# in the API or in the UI.


def known_groups(definitions) -> dict:
    """Every declared group, as ``{group id: operator-facing label}``."""
    return {definition.presentation.status_group: definition.presentation.status_group_label
            for definition in definitions if definition.presentation.status_group}


def group_members(definitions, group_id: str) -> tuple:
    return tuple(definition.id for definition in definitions
                 if definition.presentation.status_group == group_id)


def group_enabled(settings, group_id) -> bool:
    """The group's desired state. Absent means enabled: a configuration written
    before this gate existed keeps behaving exactly as it did."""
    if not group_id:
        return True
    entry = (getattr(settings, "integration_groups", None) or {}).get(group_id)
    if entry is None:
        return True
    return bool(entry.enabled if isinstance(entry, IntegrationGroupSettings) else entry.get("enabled", True))


def effective_enabled(settings, definition) -> bool:
    """Runtime participation: the member's own preference AND its group's gate.

    Derived, never stored. ``integrations.<id>.enabled`` remains the member's
    own desired state and is not replaced by this.
    """
    entry = (settings.integrations or {}).get(definition.id)
    desired = entry.enabled if isinstance(entry, IntegrationSettings) else (
        bool((entry or {}).get("enabled", definition.default_enabled)) if entry is not None
        else definition.default_enabled)
    return bool(desired) and group_enabled(settings, definition.presentation.status_group)


def effective_integration_settings(settings, definition) -> IntegrationSettings:
    """The member's namespace as RUNTIME should see it.

    A copy: the persisted namespace is never rewritten by the gate, so turning
    a group off and on again returns exactly the member preferences that were
    there before.
    """
    entry = settings.integrations[definition.id]
    gated = effective_enabled(settings, definition)
    return entry if entry.enabled == gated else entry.model_copy(update={"enabled": gated})


def normalize_groups(settings, definitions) -> dict:
    """Complete the group namespace: every declared group gets an entry."""
    groups = dict(getattr(settings, "integration_groups", None) or {})
    for group_id in known_groups(definitions):
        entry = groups.get(group_id)
        groups[group_id] = entry if isinstance(entry, IntegrationGroupSettings) else \
            IntegrationGroupSettings(**(entry or {}))
    return groups


def set_group_enabled(settings, group_id: str, enabled: bool, *, definitions=None):
    """Return ``settings`` with exactly one group gate changed.

    The one place a group gate is written. It touches no member namespace, in
    either direction -- proving that is the whole point of a gate.
    """
    from integrations.catalog import definitions as production
    known = known_groups(definitions if definitions is not None else production)
    if group_id not in known:
        raise ValueError("Unknown integration group")
    groups = dict(getattr(settings, "integration_groups", None) or {})
    groups[group_id] = IntegrationGroupSettings(enabled=bool(enabled))
    return settings.model_copy(update={"integration_groups": groups})


def public_integration_groups(settings, definitions) -> dict:
    """Safe public projection of the group gates, with their membership."""
    return {group_id: {
        "enabled": group_enabled(settings, group_id),
        "label": label,
        "members": list(group_members(definitions, group_id)),
    } for group_id, label in known_groups(definitions).items()}


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
        if "enabled" in entry.model_fields_set:
            enabled = entry.enabled
        elif isinstance(older, IntegrationSettings):
            enabled = older.enabled
        else:
            # Never decided before: the integration's own opt-in default wins.
            enabled = definition.default_enabled
        priority = older.priority if isinstance(older, IntegrationSettings) and "priority" not in entry.model_fields_set else entry.priority
        namespaces[definition.id] = IntegrationSettings(
            enabled=enabled, priority=priority, options=validated,
            verification=_carried_verification(definition, entry, older, validated))
    return settings.model_copy(update={
        "integrations": namespaces,
        "integration_groups": normalize_groups(settings, definitions),
        "transfer_policy": _merged_namespace(
            TransferSettings, getattr(previous, "transfer_policy", None), settings.transfer_policy),
        "execution_runtime_limits": _merged_namespace(
            ExecutionRuntimeLimits, getattr(previous, "execution_runtime_limits", None), settings.execution_runtime_limits),
    })


def _carried_verification(definition, entry, older, validated: dict) -> dict:
    """The verification evidence the namespace being saved is entitled to keep.

    ONE owner for this, here, rather than a copy in every mutation route:

    * a scoped route rebuilds ``IntegrationSettings`` from the values it wrote,
      so its entry carries no evidence and the PREVIOUS namespace is the source;
    * the load path has no previous namespace, so the persisted entry is;
    * evidence that no longer describes the configuration actually being saved
      is RETIRED here, which is what makes ``verified`` derived truth instead of
      a flag somebody has to remember to clear. A verification-relevant change
      therefore drops the proof; an unrelated change in the same namespace
      cannot, because its fingerprint did not move.

    It is never taken from client input: the only surfaces that accept a
    namespace from a request rebuild it field by field, and the whole-settings
    route carries the previous canonical namespaces forward wholesale.
    """
    carried = dict(entry.verification or {})
    if not carried and isinstance(older, IntegrationSettings):
        carried = dict(older.verification or {})
    if not carried:
        return {}
    current = definition.verification_fingerprints(validated)
    return {subject: fingerprint for subject, fingerprint in carried.items()
            if subject in current and current[subject][0] == fingerprint}


def accept_verification(settings, definition, proofs):
    """Record evidence for every subject of the SAVED configuration that one of
    ``proofs`` attests.

    The browser never states that something is verified. It may only carry back
    an opaque proof this server minted for the draft this server tested, and
    the fingerprint is re-derived HERE from the configuration that was actually
    saved -- so a proof of a different draft, a forged token, or an asserted
    boolean matches nothing and establishes nothing.
    """
    tokens = [str(proof) for proof in (proofs or []) if proof]
    entry = (settings.integrations or {}).get(definition.id)
    if not tokens or not isinstance(entry, IntegrationSettings):
        return settings
    evidence = dict(entry.verification or {})
    for subject, (fingerprint, _required) in definition.verification_fingerprints(entry.options).items():
        if evidence.get(subject) == fingerprint:
            continue
        if any(verification_proves(token, fingerprint) for token in tokens):
            evidence[subject] = fingerprint
    if evidence == (entry.verification or {}):
        return settings
    return settings.model_copy(update={"integrations": {
        **settings.integrations,
        definition.id: entry.model_copy(update={"verification": evidence})}})


def record_verification_outcome(settings, definition, fingerprint: str, ok: bool):
    """Commit or retire evidence for a subject of the CURRENT SAVED configuration.

    A Test whose material is not the saved configuration matches no subject, so
    it neither verifies nor revokes anything -- a failed Test of an unsaved
    draft can never take down a different saved configuration's proof. A Test of
    exactly the saved configuration does both: success is durable proof (nothing
    unsaved is being promoted), and failure retires a proof that has stopped
    being true rather than leaving the provider claiming ``Verified``.

    Returns the updated settings, or ``None`` when nothing changed.
    """
    if not ok:
        # The failure is the newest truth about this material, so it retires
        # every successful proof minted for it earlier. This happens BEFORE any
        # question of which saved subject matches: a proof is evidence about
        # material, so a failed Test of an unsaved draft must still stop that
        # draft's own older proof from being presented to a later Save. Without
        # it, a Save could replay a pre-failure proof and restore Verified for a
        # configuration this server has just proven broken.
        supersede_verification_proofs(fingerprint)
    entry = (settings.integrations or {}).get(definition.id)
    if not isinstance(entry, IntegrationSettings):
        return None
    matched = [subject for subject, (current, _required)
               in definition.verification_fingerprints(entry.options).items()
               if current == fingerprint]
    if not matched:
        return None
    evidence = dict(entry.verification or {})
    for subject in matched:
        if ok:
            evidence[subject] = fingerprint
        else:
            evidence.pop(subject, None)
    if evidence == (entry.verification or {}):
        return None
    return settings.model_copy(update={"integrations": {
        **settings.integrations,
        definition.id: entry.model_copy(update={"verification": evidence})}})


def public_integrations(settings, definitions):
    known = {definition.id: definition for definition in definitions}
    result = {}
    for identity, entry in settings.integrations.items():
        definition = known.get(identity)
        result[identity] = {
            # The member's own stored preference. It does NOT flip because the
            # group gate is closed: the operator's choice about this member
            # survives a master toggle, and its toggle keeps showing it.
            "enabled": entry.enabled,
            # Derived, read-only: what the runtime actually does with it.
            "effective_enabled": (effective_enabled(settings, definition)
                                  if definition else entry.enabled),
            "priority": entry.priority,
            "name": definition.name if definition else None,
            "kind": definition.kind if definition else None,
            "configured": definition.configured(entry.options) if definition else False,
            # Derived, never stored as a flag: the current saved
            # verification-relevant configuration is covered by successful test
            # evidence. The evidence itself stays internal.
            "verified": definition.verified(entry.options, entry.verification) if definition else False,
            # Derived, never stored: whether "verified" is a question this
            # integration can even be asked. An integration that declares no
            # verification subjects has nothing to prove, so a presentation
            # owner must not report it as unverified forever. This is the fact
            # that makes that distinction possible; it is not a second
            # verification authority.
            "verification_applicable": bool(definition is not None
                                            and definition.verification_subjects is not None),
            "presentation": definition.presentation.public() if definition else {},
            "options": definition.public_options(entry.options) if definition else {},
        }
    return result
