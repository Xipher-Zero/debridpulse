from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet, OctKey

from auth.models import Principal
from auth.oidc_version import oidc_configuration_version_from_config
from auth.policy import oidc_auth_enabled, safe_return_path


OIDC_CALLBACK_PATH = "/auth/oidc/callback"
OIDC_CORRELATION_COOKIE = "__Host-debridpulse-oidc"
OIDC_TRANSACTION_TTL_SECONDS = 600


class OidcError(Exception):
    """Base class for sanitized OIDC failures safe to map at the HTTP boundary."""


class OidcConfigurationError(OidcError):
    pass


class OidcProtocolError(OidcError):
    pass


class OidcAuthorizationError(OidcError):
    pass


@dataclass(frozen=True, slots=True)
class OidcConfiguration:
    issuer: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    callback_url: str
    provider_name: str
    allow_all: bool
    allowed_subjects: tuple[str, ...]
    allowed_emails: tuple[str, ...]
    allowed_groups: tuple[str, ...]
    group_claim: str


@dataclass(frozen=True, slots=True)
class OidcDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_endpoint_auth_methods: tuple[str, ...]
    signing_algorithms: tuple[str, ...]
    userinfo_endpoint: str = ""


@dataclass(frozen=True, slots=True)
class OidcTransaction:
    nonce: str
    code_verifier: str
    correlation_fingerprint: bytes
    created_at: float
    expires_at: float
    return_to: str
    config: OidcConfiguration
    discovery: OidcDiscovery


class OidcTransactionStore:
    """Bounded one-time OIDC transaction state kept only in process memory."""

    def __init__(
        self,
        *,
        ttl_seconds: float = OIDC_TRANSACTION_TTL_SECONDS,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._entries: OrderedDict[bytes, OidcTransaction] = OrderedDict()

    def _fingerprint(self, value: str) -> bytes:
        return hmac.new(
            self._key,
            str(value or "").encode("utf-8", errors="surrogatepass"),
            hashlib.sha256,
        ).digest()

    def create(
        self,
        *,
        state: str,
        correlation: str,
        nonce: str,
        code_verifier: str,
        return_to: str,
        config: OidcConfiguration,
        discovery: OidcDiscovery,
    ) -> None:
        now = self._clock()
        self.cleanup()
        key = self._fingerprint(state)
        self._entries[key] = OidcTransaction(
            nonce=str(nonce),
            code_verifier=str(code_verifier),
            correlation_fingerprint=self._fingerprint(correlation),
            created_at=now,
            expires_at=now + self.ttl_seconds,
            return_to=safe_return_path(return_to),
            config=config,
            discovery=discovery,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def consume(self, state: str, correlation: str) -> OidcTransaction | None:
        if not state or not correlation:
            return None
        key = self._fingerprint(state)
        transaction = self._entries.pop(key, None)
        if transaction is None:
            return None
        now = self._clock()
        if transaction.expires_at <= now:
            return None
        if not secrets.compare_digest(
            transaction.correlation_fingerprint,
            self._fingerprint(correlation),
        ):
            return None
        return transaction

    def cleanup(self) -> int:
        now = self._clock()
        expired = [key for key, item in self._entries.items() if item.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        self.cleanup()
        return len(self._entries)


def _https_origin(value: str, *, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise OidcConfigurationError(f"{field} is required")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise OidcConfigurationError(f"{field} is invalid") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OidcConfigurationError(f"{field} must be an HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise OidcConfigurationError(f"{field} must not contain user information")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise OidcConfigurationError(f"{field} must be scheme://host[:port] only")
    return raw.rstrip("/")


def _https_endpoint(value: str, *, field: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise OidcProtocolError(f"OIDC discovery returned an invalid {field}") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OidcProtocolError(f"OIDC discovery returned a non-HTTPS {field}")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise OidcProtocolError(f"OIDC discovery returned an invalid {field}")
    return raw


def _normalize_issuer(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise OidcConfigurationError("OIDC issuer URL is invalid") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OidcConfigurationError("OIDC issuer URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise OidcConfigurationError("OIDC issuer URL must not contain user information")
    if parsed.query or parsed.fragment:
        raise OidcConfigurationError("OIDC issuer URL must not contain a query or fragment")
    # Issuer identifiers are exact strings under OIDC. Do not normalize away a
    # trailing slash; providers such as authentik legitimately publish one.
    return raw


def effective_public_base_url(settings) -> str:
    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    if not configured:
        configured = str(getattr(settings, "public_base_url", "") or "").strip()
    return _https_origin(configured, field="Public Base URL")


def oidc_callback_url(settings) -> str:
    return effective_public_base_url(settings) + OIDC_CALLBACK_PATH


def oidc_configuration(settings) -> OidcConfiguration:
    if not oidc_auth_enabled(settings):
        raise OidcConfigurationError("OpenID Connect is disabled")
    issuer = _normalize_issuer(getattr(settings, "oidc_issuer_url", ""))
    client_id = str(getattr(settings, "oidc_client_id", "") or "").strip()
    if not client_id:
        raise OidcConfigurationError("OIDC Client ID is required")
    scopes: list[str] = []
    for item in getattr(settings, "oidc_scopes", []) or []:
        value = str(item or "").strip()
        if value and value not in scopes:
            scopes.append(value)
    if "openid" not in scopes:
        scopes.insert(0, "openid")
    group_claim = str(getattr(settings, "oidc_group_claim", "groups") or "groups").strip()
    if not group_claim:
        raise OidcConfigurationError("OIDC group claim must not be empty")
    return OidcConfiguration(
        issuer=issuer,
        client_id=client_id,
        client_secret=str(getattr(settings, "oidc_client_secret", "") or ""),
        scopes=tuple(scopes),
        callback_url=oidc_callback_url(settings),
        provider_name=str(getattr(settings, "oidc_provider_name", "") or "OpenID Connect").strip()
        or "OpenID Connect",
        allow_all=bool(getattr(settings, "oidc_allow_all", False)),
        allowed_subjects=tuple(
            str(item or "").strip()
            for item in (getattr(settings, "oidc_allowed_subjects", []) or [])
            if str(item or "").strip()
        ),
        allowed_emails=tuple(
            str(item or "").strip().casefold()
            for item in (getattr(settings, "oidc_allowed_emails", []) or [])
            if str(item or "").strip()
        ),
        allowed_groups=tuple(
            str(item or "").strip()
            for item in (getattr(settings, "oidc_allowed_groups", []) or [])
            if str(item or "").strip()
        ),
        group_claim=group_claim,
    )


def oidc_auth_ready(settings) -> bool:
    if not oidc_auth_enabled(settings):
        return False
    try:
        oidc_configuration(settings)
    except OidcConfigurationError:
        return False
    return True


def _discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def _metadata_string_array(
    data: Mapping[str, Any],
    field: str,
    *,
    required: bool,
) -> tuple[str, ...] | None:
    raw = data.get(field)
    if raw is None:
        if required:
            raise OidcProtocolError(f"OIDC discovery is missing required {field} metadata")
        return None
    if not isinstance(raw, list) or not raw:
        raise OidcProtocolError(f"OIDC discovery returned invalid {field} metadata")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise OidcProtocolError(f"OIDC discovery returned invalid {field} metadata")
        value = item.strip()
        if value not in values:
            values.append(value)
    return tuple(values)


def _parse_discovery(config: OidcConfiguration, data: Mapping[str, Any]) -> OidcDiscovery:
    discovered_issuer = str(data.get("issuer") or "").strip()
    if not discovered_issuer or discovered_issuer != config.issuer:
        raise OidcProtocolError("OIDC discovery issuer does not match the configured issuer")

    authorization_endpoint = _https_endpoint(
        str(data.get("authorization_endpoint") or ""),
        field="authorization endpoint",
    )
    token_endpoint = _https_endpoint(
        str(data.get("token_endpoint") or ""),
        field="token endpoint",
    )
    jwks_uri = _https_endpoint(str(data.get("jwks_uri") or ""), field="JWKS URI")

    response_types = _metadata_string_array(data, "response_types_supported", required=True)
    if response_types is None or "code" not in response_types:
        raise OidcProtocolError("OIDC provider does not advertise Authorization Code flow support")

    pkce = _metadata_string_array(data, "code_challenge_methods_supported", required=False)
    if pkce is not None and "S256" not in pkce:
        raise OidcProtocolError("OIDC provider does not advertise PKCE S256 support")

    methods = _metadata_string_array(
        data,
        "token_endpoint_auth_methods_supported",
        required=False,
    )
    if methods is None:
        # OIDC Discovery 1.0 defines this default when the member is omitted.
        methods = ("client_secret_basic",)

    advertised_algorithms = _metadata_string_array(
        data,
        "id_token_signing_alg_values_supported",
        required=True,
    )
    algorithms = tuple(alg for alg in (advertised_algorithms or ()) if alg != "none")
    if not algorithms:
        raise OidcProtocolError("OIDC provider advertises no usable ID-token signing algorithm")

    raw_userinfo = str(data.get("userinfo_endpoint") or "").strip()
    userinfo_endpoint = (
        _https_endpoint(raw_userinfo, field="UserInfo endpoint")
        if raw_userinfo
        else ""
    )

    return OidcDiscovery(
        issuer=config.issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        jwks_uri=jwks_uri,
        token_endpoint_auth_methods=methods,
        signing_algorithms=algorithms,
        userinfo_endpoint=userinfo_endpoint,
    )


async def discover_oidc(
    config: OidcConfiguration,
    *,
    client: httpx.AsyncClient | None = None,
) -> OidcDiscovery:
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=8.0, follow_redirects=False)
    try:
        response = await http.get(
            _discovery_url(config.issuer),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise OidcProtocolError("OIDC discovery did not return a JSON object")
        return _parse_discovery(config, data)
    except OidcError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcProtocolError("OIDC discovery is unavailable") from exc
    finally:
        if owns_client:
            await http.aclose()


def _token_endpoint_auth_method(config: OidcConfiguration, discovery: OidcDiscovery) -> str:
    supported = set(discovery.token_endpoint_auth_methods)
    if not config.client_secret:
        if supported and "none" not in supported:
            raise OidcConfigurationError("OIDC provider requires client authentication")
        return "none"
    if not supported or "client_secret_basic" in supported:
        return "client_secret_basic"
    if "client_secret_post" in supported:
        return "client_secret_post"
    raise OidcConfigurationError("OIDC provider does not support a compatible client-secret method")


async def begin_oidc_login(settings, *, return_to: str = "/") -> tuple[str, str]:
    config = oidc_configuration(settings)
    discovery = await discover_oidc(config)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(48)
    correlation = secrets.token_urlsafe(32)
    client = AsyncOAuth2Client(
        client_id=config.client_id,
        client_secret=config.client_secret or None,
        scope=" ".join(config.scopes),
        redirect_uri=config.callback_url,
        code_challenge_method="S256",
        token_endpoint_auth_method=_token_endpoint_auth_method(config, discovery),
    )
    try:
        authorization_url, generated_state = client.create_authorization_url(
            discovery.authorization_endpoint,
            state=state,
            code_verifier=code_verifier,
            nonce=nonce,
        )
    finally:
        await client.aclose()
    if generated_state != state:
        raise OidcProtocolError("OIDC state generation was inconsistent")
    oidc_transaction_store.create(
        state=state,
        correlation=correlation,
        nonce=nonce,
        code_verifier=code_verifier,
        return_to=return_to,
        config=config,
        discovery=discovery,
    )
    return authorization_url, correlation


async def _fetch_json(url: str) -> Mapping[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcProtocolError("OIDC key set is unavailable") from exc
    if not isinstance(data, dict):
        raise OidcProtocolError("OIDC key set did not return a JSON object")
    return data


def _claims_need_userinfo(
    config: OidcConfiguration,
    claims: Mapping[str, Any],
) -> bool:
    if config.allow_all:
        return False
    if config.allowed_emails:
        email = str(claims.get("email") or "").strip()
        if not email or claims.get("email_verified") is not True:
            return True
    if config.allowed_groups and config.group_claim not in claims:
        return True
    return False


async def _fetch_userinfo(
    endpoint: str,
    access_token: str,
    expected_subject: str,
) -> Mapping[str, Any]:
    if not endpoint or not access_token:
        raise OidcProtocolError("OIDC UserInfo is required but unavailable")
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.get(
                endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcProtocolError("OIDC UserInfo is unavailable") from exc
    if not isinstance(data, dict):
        raise OidcProtocolError("OIDC UserInfo did not return a JSON object")
    subject = str(data.get("sub") or "").strip()
    if not subject or not secrets.compare_digest(subject, str(expected_subject)):
        raise OidcProtocolError("OIDC UserInfo subject does not match the ID token")
    return data


def _merge_userinfo_claims(
    config: OidcConfiguration,
    id_claims: Mapping[str, Any],
    userinfo: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(id_claims)

    # Treat email + email_verified as one authorization-bearing claim pair. If
    # the ID token does not already contain a verified email, UserInfo may
    # replace that pair only when UserInfo itself supplies both the address and
    # an explicit boolean true verification claim. Never attach UserInfo's
    # verification bit to a different email retained from the ID token.
    if config.allowed_emails:
        id_email = str(id_claims.get("email") or "").strip()
        if not (id_email and id_claims.get("email_verified") is True):
            userinfo_email = str(userinfo.get("email") or "").strip()
            if userinfo_email and userinfo.get("email_verified") is True:
                merged["email"] = userinfo["email"]
                merged["email_verified"] = True
    else:
        for key in ("email", "email_verified"):
            if key not in merged and key in userinfo:
                merged[key] = userinfo[key]

    for key in ("name", "preferred_username", "nickname", "given_name", "family_name", config.group_claim):
        if key not in merged and key in userinfo:
            merged[key] = userinfo[key]
    return merged


def _validate_authorized_party(claims: Mapping[str, Any], client_id: str) -> None:
    audience = claims.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
        audiences = audience
    else:
        raise OidcProtocolError("OIDC ID token contains an invalid audience")
    if client_id not in audiences:
        raise OidcProtocolError("OIDC ID token audience is invalid")
    azp = claims.get("azp")
    if len(audiences) > 1 and not azp:
        raise OidcProtocolError("OIDC ID token is missing the authorized party")
    if azp is not None and azp != client_id:
        raise OidcProtocolError("OIDC ID token authorized party is invalid")


def _id_token_algorithm(encoded: str) -> str:
    try:
        header_segment = str(encoded).split(".", 1)[0]
        if not header_segment:
            raise ValueError("missing JOSE header")
        padding = "=" * (-len(header_segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_segment + padding))
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OidcProtocolError("OIDC ID token header is invalid") from exc
    if not isinstance(header, dict):
        raise OidcProtocolError("OIDC ID token header is invalid")
    algorithm = str(header.get("alg") or "").strip()
    if not algorithm or algorithm == "none":
        raise OidcProtocolError("OIDC ID token signing algorithm is invalid")
    return algorithm


async def validate_id_token(
    token_response: Mapping[str, Any],
    transaction: OidcTransaction,
) -> Mapping[str, Any]:
    encoded = str(token_response.get("id_token") or "")
    if not encoded:
        raise OidcProtocolError("OIDC token response did not include an ID token")

    algorithm = _id_token_algorithm(encoded)
    if algorithm not in transaction.discovery.signing_algorithms:
        raise OidcProtocolError("OIDC ID token uses an unadvertised signing algorithm")

    try:
        if algorithm in {"HS256", "HS384", "HS512"}:
            if not transaction.config.client_secret:
                raise OidcProtocolError("OIDC MAC-signed ID token requires a client secret")
            verification_key = OctKey.import_key(
                transaction.config.client_secret,
                {"use": "sig"},
            )
        else:
            jwks = await _fetch_json(transaction.discovery.jwks_uri)
            verification_key = KeySet.import_key_set(dict(jwks))

        token = jwt.decode(
            encoded,
            verification_key,
            algorithms=[algorithm],
        )
        claims = CodeIDToken(
            token.claims,
            token.header,
            options={
                "iss": {"essential": True, "value": transaction.config.issuer},
                "sub": {"essential": True},
                "aud": {"essential": True},
                "exp": {"essential": True},
                "iat": {"essential": True},
            },
            params={
                "nonce": transaction.nonce,
                "client_id": transaction.config.client_id,
                "access_token": token_response.get("access_token"),
            },
        )
        claims.validate(leeway=60)
    except OidcError:
        raise
    except Exception as exc:
        raise OidcProtocolError("OIDC ID token validation failed") from exc
    if claims.get("nonce") != transaction.nonce:
        raise OidcProtocolError("OIDC ID token nonce is invalid")
    _validate_authorized_party(claims, transaction.config.client_id)
    if algorithm in {"HS256", "HS384", "HS512"} and not isinstance(claims.get("aud"), str):
        # OIDC Core leaves MAC validation behavior unspecified for multi-audience
        # ID tokens. Reject rather than guessing which party shares the secret.
        raise OidcProtocolError("OIDC MAC-signed ID token requires a single audience")
    return dict(claims)


def authorize_oidc_claims(
    config: OidcConfiguration,
    claims: Mapping[str, Any],
) -> Principal:
    subject = str(claims.get("sub") or "").strip()
    issuer = str(claims.get("iss") or "").strip()
    if not subject or issuer != config.issuer:
        raise OidcAuthorizationError("OIDC identity is not authorized")

    if not config.allow_all:
        if not (config.allowed_subjects or config.allowed_emails or config.allowed_groups):
            raise OidcAuthorizationError("OIDC identity is not authorized")

        if config.allowed_subjects:
            composite = f"{issuer}|{subject}"
            if composite not in config.allowed_subjects:
                raise OidcAuthorizationError("OIDC identity is not authorized")

        if config.allowed_emails:
            email = str(claims.get("email") or "").strip().casefold()
            if claims.get("email_verified") is not True or email not in config.allowed_emails:
                raise OidcAuthorizationError("OIDC identity is not authorized")

        if config.allowed_groups:
            raw_groups = claims.get(config.group_claim)
            if isinstance(raw_groups, str):
                groups = {raw_groups}
            elif isinstance(raw_groups, (list, tuple, set)):
                groups = {str(item) for item in raw_groups}
            else:
                raise OidcAuthorizationError("OIDC identity is not authorized")
            if not groups.intersection(config.allowed_groups):
                raise OidcAuthorizationError("OIDC identity is not authorized")

    display_name = str(
        claims.get("name")
        or claims.get("preferred_username")
        or claims.get("email")
        or subject
    )
    public_claims = {
        key: claims[key]
        for key in (
            "iss",
            "sub",
            "email",
            "email_verified",
            "name",
            "preferred_username",
            "nickname",
            "given_name",
            "family_name",
            config.group_claim,
        )
        if key in claims
    }
    return Principal.oidc_session(
        f"{issuer}|{subject}",
        display_name=display_name,
        claims=public_claims,
        credential_version=oidc_configuration_version_from_config(config),
    )


async def complete_oidc_login(
    *,
    state: str,
    code: str,
    correlation: str,
) -> tuple[Principal, str]:
    transaction = oidc_transaction_store.consume(state, correlation)
    if transaction is None:
        raise OidcProtocolError("OIDC login transaction is invalid or expired")
    if not code:
        raise OidcProtocolError("OIDC callback did not include an authorization code")

    client = AsyncOAuth2Client(
        client_id=transaction.config.client_id,
        client_secret=transaction.config.client_secret or None,
        scope=" ".join(transaction.config.scopes),
        redirect_uri=transaction.config.callback_url,
        state=state,
        code_challenge_method="S256",
        token_endpoint_auth_method=_token_endpoint_auth_method(
            transaction.config,
            transaction.discovery,
        ),
    )
    try:
        token_response = await client.fetch_token(
            transaction.discovery.token_endpoint,
            grant_type="authorization_code",
            code=code,
            code_verifier=transaction.code_verifier,
        )
    except Exception as exc:
        raise OidcProtocolError("OIDC authorization-code exchange failed") from exc
    finally:
        await client.aclose()
    claims = await validate_id_token(token_response, transaction)
    if _claims_need_userinfo(transaction.config, claims):
        userinfo = await _fetch_userinfo(
            transaction.discovery.userinfo_endpoint,
            str(token_response.get("access_token") or ""),
            str(claims.get("sub") or ""),
        )
        claims = _merge_userinfo_claims(transaction.config, claims, userinfo)
    principal = authorize_oidc_claims(transaction.config, claims)
    return principal, transaction.return_to


oidc_transaction_store = OidcTransactionStore()
