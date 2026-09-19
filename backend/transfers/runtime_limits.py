"""Neutral live executor-runtime capability limits (DP 1.0.12 canonical
architecture correction, Workstream C, specification section 4.4).

Download bandwidth is a neutral execution capability, not an aria2-native
policy concept: the canonical owner of the DESIRED (configured) value is this
universal namespace. The EFFECTIVE (actually-applied)
value and any apply failure are reported by the concrete executor/integration
runtime that received the injected desired value (specification section
2.7) -- this module owns only the desired/configured side.
"""
from pydantic import BaseModel, Field


class ExecutionRuntimeLimits(BaseModel):
    max_download_bytes_per_second: int = Field(default=0, ge=0)


# Migration INPUT only (see ``transfers.settings.LEGACY_INPUT_FIELDS``):
# read by the load-time migration boundary and the read-only compatibility
# projection, never a runtime or persisted authority.
LEGACY_INPUT_FIELDS = {
    "aria2_max_download_limit": "max_download_bytes_per_second",
}
