"""Neutral live executor-runtime capability limits (DP 1.0.12 canonical
architecture correction, Workstream C, specification section 4.4).

Download bandwidth is a neutral execution capability, not an aria2-native
policy concept: the canonical owner of the DESIRED (configured) value is this
universal namespace, mirroring ``transfers.settings.TransferSettings``'s
one-way legacy-field translation pattern. The EFFECTIVE (actually-applied)
value and any apply failure are reported by the concrete executor/integration
runtime that received the injected desired value (specification section
2.7) -- this module owns only the desired/configured side.
"""
from pydantic import BaseModel, Field


class ExecutionRuntimeLimits(BaseModel):
    max_download_bytes_per_second: int = Field(default=0, ge=0)


_LEGACY_FIELDS = {
    "aria2_max_download_limit": "max_download_bytes_per_second",
}


def normalize_runtime_limits(settings, *, previous=None, supplied_fields=None):
    """One-way migration: ``aria2_max_download_limit`` is legacy input only.

    Canonical saves never regenerate it as an authoritative value and never
    mirror the canonical value back onto the legacy flat field (specification
    section 9.2) -- ``execution_runtime_limits`` is the sole persisted
    authority after this translation.
    """
    older = getattr(previous, "execution_runtime_limits", None)
    entry = settings.execution_runtime_limits
    options = older.model_dump() if older is not None else {}
    if entry is not None:
        options.update(entry.model_dump(exclude_unset=True))
    for legacy, canonical in _LEGACY_FIELDS.items():
        if (entry is None and older is None) or (supplied_fields is not None and legacy in supplied_fields):
            options[canonical] = getattr(settings, legacy)
    limits = ExecutionRuntimeLimits(**options)
    return settings.model_copy(update={"execution_runtime_limits": limits})
