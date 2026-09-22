"""Neutral live executor-runtime capability limits (DP 1.0.12 canonical
architecture correction, Workstream C, specification section 4.4).

Download bandwidth is a neutral execution capability, not an executor-native
policy concept: the canonical owner of the DESIRED (configured) value is this
universal namespace. ``transfers.runtime_coordination`` owns the global
allocation and reports the EFFECTIVE (proven enforced) value; executors only
enforce the ceiling assigned to them. This module owns only the desired side.
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
