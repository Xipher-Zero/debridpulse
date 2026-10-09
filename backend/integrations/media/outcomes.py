"""The ONE translation of Media Downloads outcomes into normalized errors.

The sandboxed worker (``executors.media.worker``) classifies every native
failure into a small outcome vocabulary; the provider (resolution) and the
executor (execution) both translate it here, so one fact never means two
things on the two halves of the integration.

Every outcome is a fact about the submitted media, its source, this machine or
DebridPulse policy -- never about a provider service that another provider
could replace. None is ``transfers.policy.provider_attributable``, so a medium
Media Downloads claimed is never handed to another provider (generic HTTP
included) because resolving or acquiring it failed.
"""
from __future__ import annotations

import re

from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin, Retryability, Stage,
)

INTEGRATION_ID = "media"

_OUTCOMES = {
    # The submitted address is not a medium an explicit extractor handles.
    "unsupported": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Retryability.NEVER, Origin.USER),
    # Authenticated media is outside this version: no credential is asked for.
    "auth_required": (Domain.RESOLUTION, Category.SOURCE_UNAVAILABLE, Retryability.NEVER, Origin.REMOTE_SOURCE),
    # Live (and not-yet-finished) media is outside this version.
    "live_unsupported": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Retryability.NEVER, Origin.REMOTE_SOURCE),
    "unavailable": (Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Retryability.NEVER, Origin.REMOTE_SOURCE),
    "geo_restricted": (Domain.RESOLUTION, Category.SOURCE_UNAVAILABLE, Retryability.NEVER, Origin.REMOTE_SOURCE),
    "no_usable_formats": (Domain.RESOLUTION, Category.NO_TRANSFER_CANDIDATE, Retryability.NEVER,
                          Origin.REMOTE_SOURCE),
    # A component would need a transport that cannot be carried by the guard.
    "transport_unsupported": (Domain.RESOLUTION, Category.NO_TRANSFER_CANDIDATE, Retryability.NEVER,
                              Origin.REMOTE_SOURCE),
    # The preferred-language subtitle exists only in formats no container
    # carries unchanged: never dropped, never converted.
    "subtitle_unembeddable": (Domain.RESOLUTION, Category.NO_TRANSFER_CANDIDATE, Retryability.NEVER,
                              Origin.REMOTE_SOURCE),
    # The link names one item inside its enclosing collection and no
    # acquisition scope was chosen: the operator must say which.
    "scope_choice_required": (Domain.REQUEST, Category.INVALID_REQUEST, Retryability.NEVER, Origin.USER),
    "network": (Domain.NETWORK, Category.CONNECTION_FAILED, Retryability.BACKOFF, Origin.REMOTE_SOURCE),
    "rate_limited": (Domain.RESOLUTION, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF,
                     Origin.REMOTE_SOURCE),
    # The origin itself answered 403 to a planned component or subtitle (never
    # the guard: its refusals stay egress_refused). Every attempt extracts
    # afresh, so a later attempt asks with newly issued addresses.
    "source_refused": (Domain.RESOLUTION, Category.CANDIDATE_REJECTED, Retryability.BACKOFF, Origin.REMOTE_SOURCE),
    "extractor_failed": (Domain.RESOLUTION, Category.RESOLUTION_FAILED, Retryability.BACKOFF, Origin.REMOTE_SOURCE),
    # The guard refused a destination (private, local, mixed or rebinding).
    "egress_refused": (Domain.SECURITY, Category.EGRESS_POLICY_VIOLATION, Retryability.NEVER,
                       Origin.SECURITY_POLICY),
    "path_refused": (Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Retryability.NEVER, Origin.SECURITY_POLICY),
    # The attempt's route ended under it (DebridPulse restarted): a fresh
    # attempt starts from zero.
    "route_revoked": (Domain.EXECUTOR, Category.TRANSFER_INTERRUPTED, Retryability.BACKOFF, Origin.LOCAL_SYSTEM),
    "identity_changed": (Domain.RESOLUTION, Category.CANDIDATE_EXPIRED, Retryability.AFTER_RERESOLUTION,
                         Origin.REMOTE_SOURCE),
    "format_unavailable": (Domain.RESOLUTION, Category.CANDIDATE_EXPIRED, Retryability.AFTER_RERESOLUTION,
                           Origin.REMOTE_SOURCE),
    # A member's fresh plan no longer produces the file its manifest named.
    "plan_changed": (Domain.RESOLUTION, Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF,
                     Origin.REMOTE_SOURCE),
    "finalization_failed": (Domain.EXECUTOR, Category.MATERIALIZATION_FAILED, Retryability.NEVER, Origin.EXECUTOR),
    "output_missing": (Domain.EXECUTOR, Category.MATERIALIZATION_FAILED, Retryability.AFTER_RERESOLUTION,
                       Origin.EXECUTOR),
    "runtime_unavailable": (Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Retryability.BACKOFF,
                            Origin.LOCAL_SYSTEM),
}


class MediaFailure(Exception):
    """One worker outcome on its way to ``outcome_error``."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


_UNKNOWN = (Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Retryability.UNKNOWN, Origin.LOCAL_SYSTEM)

OUTCOMES = frozenset(_OUTCOMES)
# Where the worker's attempt was when it failed (``worker._Phase``): bounded
# diagnostics carried in the error's context, never part of its identity.
PHASES = frozenset({"extract", "component", "subtitle", "finalize", "install"})
# A format identifier as the worker bounds it (``worker._FORMAT_ID``), applied
# again here: a record value outside it is dropped, never shortened.
FORMAT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")


def phase_context(value) -> dict:
    """The worker's failure phase as error context: only the known phase, a
    component index and a format identifier within ``FORMAT_ID``."""
    value = value if isinstance(value, dict) else {}
    if value.get("phase") not in PHASES:
        return {}
    context = {"phase": value["phase"]}
    if isinstance(value.get("component"), int) and not isinstance(value["component"], bool):
        context["component"] = value["component"]
    if isinstance(value.get("format_id"), str) and FORMAT_ID.fullmatch(value["format_id"]):
        context["format_id"] = value["format_id"]
    return context


def outcome_error(code: str, stage: Stage, *, detail: str = "", integration_id: str = INTEGRATION_ID,
                  context=None) -> NormalizedError:
    """The normalized error for one outcome; an unknown code is unmapped."""
    domain, category, retryability, origin = _OUTCOMES.get(str(code or ""), _UNKNOWN)
    known = code in _OUTCOMES
    return NormalizedError(
        domain, category, stage, retryability=retryability, origin=origin, integration_id=integration_id,
        native_code=str(code or "unknown")[:64], diagnostic=detail, context=phase_context(context),
        confidence=Confidence.HIGH if known else Confidence.LOW,
        evidence_basis=EvidenceBasis.STRUCTURED if known else EvidenceBasis.UNKNOWN,
    )
