"""Provider-neutral request applicability parsing and classification."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
import re
from typing import Iterable
from urllib.parse import urlsplit

from transfers.models import TransferRequest


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*$", re.ASCII)
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)


class ApplicabilityClass(StrEnum):
    STATIC = "static"
    GENERIC = "generic"
    SPECIALIZED = "specialized"


class HostClaimScope(StrEnum):
    EXACT = "exact"
    DOMAIN = "domain"


@dataclass(frozen=True)
class HostClaim:
    host: str
    scope: HostClaimScope = HostClaimScope.EXACT
    schemes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProviderApplicability:
    """Canonical provider-owned applicability facts available without I/O."""

    generic_schemes: frozenset[str] = frozenset()
    specialized_hosts: tuple[HostClaim, ...] = ()


@dataclass(frozen=True)
class ProviderApplicabilityInput:
    provider_id: str
    request_types: frozenset[str]
    enabled: bool = True
    applicability: ProviderApplicability | None = None


@dataclass(frozen=True)
class UrlApplicabilityView:
    """Normalized routing-only URL components; the transfer endpoint stays untouched."""

    scheme: str
    hostname: str
    port: int | None
    is_ip: bool


@dataclass(frozen=True)
class ApplicabilityMatch:
    provider_id: str
    classification: ApplicabilityClass


def _normalize_scheme(value: str) -> str:
    scheme = str(value or "").strip().casefold()
    if not _SCHEME_RE.fullmatch(scheme):
        raise ValueError("Invalid URL scheme")
    return scheme


def _normalize_hostname(value: str) -> tuple[str, bool]:
    raw = str(value or "").strip().rstrip(".")
    if not raw:
        raise ValueError("URL hostname is missing")

    candidate = raw.strip("[]")
    try:
        address = ip_address(candidate)
    except ValueError:
        try:
            ascii_host = candidate.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise ValueError("Invalid internationalized hostname") from exc
        if len(ascii_host) > 253:
            raise ValueError("Hostname is too long")
        labels = ascii_host.split(".")
        if any(not label or len(label) > 63 or not _DNS_LABEL_RE.fullmatch(label) for label in labels):
            raise ValueError("Invalid DNS hostname")
        return ascii_host, False
    return address.compressed.casefold(), True


def parse_url_applicability(request: TransferRequest) -> UrlApplicabilityView | None:
    """Return a normalized URL view, or None when the request is not URL-shaped."""

    if not isinstance(request, TransferRequest) or not isinstance(request.payload, str):
        return None

    raw = request.payload
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return None

    explicit_scheme = str(parsed.scheme or "")
    kind = str(request.kind or "").casefold()
    if not explicit_scheme:
        return None

    try:
        scheme = _normalize_scheme(explicit_scheme)
    except ValueError:
        return None
    if scheme != kind:
        return None

    try:
        hostname_value = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if hostname_value is None:
        return None

    try:
        hostname, is_ip = _normalize_hostname(hostname_value)
    except ValueError:
        return None

    return UrlApplicabilityView(scheme, hostname, port, is_ip)


def _normalized_claim(claim: HostClaim) -> tuple[str, bool, frozenset[str]] | None:
    if not isinstance(claim, HostClaim):
        return None
    try:
        host, is_ip = _normalize_hostname(claim.host)
        schemes = frozenset(_normalize_scheme(item) for item in claim.schemes)
    except (TypeError, ValueError):
        return None
    return host, is_ip, schemes


def _specialized_match(view: UrlApplicabilityView, claim: HostClaim) -> bool:
    normalized = _normalized_claim(claim)
    if normalized is None:
        return False
    claim_host, claim_is_ip, schemes = normalized
    if schemes and view.scheme not in schemes:
        return False
    if claim.scope == HostClaimScope.EXACT:
        return view.hostname == claim_host
    if claim.scope != HostClaimScope.DOMAIN:
        return False
    if view.is_ip or claim_is_ip:
        return False
    return view.hostname == claim_host or view.hostname.endswith("." + claim_host)


def _generic_match(
    view: UrlApplicabilityView,
    item: ProviderApplicabilityInput,
) -> bool:
    facts = item.applicability
    if facts is None:
        # Compatibility for existing providers that have only the canonical
        # request-type declaration. Explicit applicability replaces this legacy
        # URL-generic interpretation and can therefore be specialized-only.
        return view.scheme in {str(kind).casefold() for kind in item.request_types}
    try:
        schemes = {_normalize_scheme(scheme) for scheme in facts.generic_schemes}
    except (TypeError, ValueError):
        return False
    return view.scheme in schemes


def classify_provider_applicability(
    request: TransferRequest,
    providers: Iterable[ProviderApplicabilityInput],
) -> tuple[ApplicabilityMatch, ...]:
    """Classify enabled providers without provider identity or runtime-state knowledge."""

    inputs = tuple(
        item for item in providers
        if item.enabled and request.kind in item.request_types
    )
    if not inputs:
        return ()

    if request.kind in {"magnet", "torrent"}:
        return tuple(
            ApplicabilityMatch(item.provider_id, ApplicabilityClass.STATIC)
            for item in inputs
        )

    view = parse_url_applicability(request)
    if view is None:
        # Non-URL forms retain their static request-type capability semantics.
        # A URL-shaped payload with the same declared kind but a malformed URL
        # is rejected below when a provider explicitly declares URL facts.
        kind = str(request.kind or "").casefold()
        payload_marks_url = (
            isinstance(request.payload, str)
            and request.payload.lstrip().casefold().startswith(kind + ":")
        )
        explicit_url_kind = payload_marks_url or any(
            item.applicability is not None
            and (
                kind in {
                    str(scheme).casefold() for scheme in item.applicability.generic_schemes
                }
                or any(
                    kind in {str(scheme).casefold() for scheme in claim.schemes}
                    for claim in item.applicability.specialized_hosts
                )
            )
            for item in inputs
        )
        if explicit_url_kind:
            return ()
        return tuple(
            ApplicabilityMatch(item.provider_id, ApplicabilityClass.STATIC)
            for item in inputs
        )

    specialized: list[ApplicabilityMatch] = []
    generic: list[ApplicabilityMatch] = []
    for item in inputs:
        facts = item.applicability
        if facts is not None and any(
            _specialized_match(view, claim) for claim in facts.specialized_hosts
        ):
            specialized.append(ApplicabilityMatch(item.provider_id, ApplicabilityClass.SPECIALIZED))
        elif _generic_match(view, item):
            generic.append(ApplicabilityMatch(item.provider_id, ApplicabilityClass.GENERIC))

    return tuple(specialized or generic)
