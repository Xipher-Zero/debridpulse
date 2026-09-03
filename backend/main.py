import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.auth_config_routes import router as auth_config_router
from api.auth_routes import router as auth_router
from api.routes import router
from api.settings_validation_routes import router as settings_validation_router
from auth.middleware import enforce_authentication, enforce_general_web_security
from auth.policy import (
    interactive_auth_enabled,
    oidc_auth_enabled,
    password_auth_enabled,
    password_auth_ready,
)
from auth.sessions import CSRF_HEADER, session_store
from core.branding import APP_METADATA_TITLE, APP_NAME, APP_SHORT_NAME
from core.config import get_settings as _get_log_settings
from core.logging_utils import configure_logging, log_startup_banner, sanitize_exception, sanitize_log_value
from core.scheduler import start_scheduler, stop_scheduler
from core.version import read_version
from db.database import DatabaseMaintenanceActive
from application.dependencies import get_application
from services.maintenance_gate import ApplicationMaintenanceActive
from transfers.errors import TransferError

_log_cfg = _get_log_settings()
configure_logging(
    getattr(_log_cfg, "log_level", "INFO"),
    bool(getattr(_log_cfg, "log_pretty", False)),
    getattr(_log_cfg, "log_format", "plain"),
)
logger = logging.getLogger("debridpulse.main")

# persistence initialization on startup


async def _prepare_startup_settings_and_migrate():
    """Establish one sanitized settings authority before migration decisions.

    v1.0.12 migration can mint durable executor mutation authority, so it must
    never interpret a stale pre-sanitization ``aria2_mode``.  Keep the legacy
    tolerant load/repair behavior, but fail closed if a safe effective settings
    object cannot be established before the ownership-sensitive migration.
    """
    try:
        from core.config import get_settings, apply_settings, save_settings
        from core.config_validator import validate_and_sanitise

        raw = get_settings()
        cfg = validate_and_sanitise(raw)
        if cfg is not raw:
            save_settings(cfg)
            apply_settings(cfg)
    except Exception as exc:
        detail = sanitize_exception(exc)
        logger.error(
            "Configuration validation failed before ownership-sensitive migration: %s",
            detail,
        )
        raise RuntimeError(
            "Configuration validation failed before ownership-sensitive migration"
        ) from exc

    from db.migrations.v112 import migrate
    await migrate(
        external_executor=cfg.aria2_mode == "external",
        globally_paused=cfg.paused,
    )
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v1.0.12 migration owns database classification and the legacy backup
    # boundary. No current initializer may touch a predecessor database first.
    # Sanitized settings are authoritative before this ownership-sensitive step.
    cfg = await _prepare_startup_settings_and_migrate()

    password_enabled = password_auth_enabled(cfg)
    oidc_enabled = oidc_auth_enabled(cfg)
    interactive_enabled = interactive_auth_enabled(cfg)
    auth_mechanisms = []
    if password_enabled:
        auth_mechanisms.append("password")
    if oidc_enabled:
        auth_mechanisms.append("oidc")
    log_startup_banner(
        logger,
        version=read_version(),
        mode="Docker / Unraid",
        database="SQLite",
        download_client=("aria2 builtin" if getattr(cfg, "aria2_mode", "builtin") == "builtin" else "aria2 external"),
        web_ui=f"http://0.0.0.0:{getattr(cfg, 'port', 8080)}",
        auth=("+".join(auth_mechanisms) if auth_mechanisms else "disabled"),
    )
    if not interactive_enabled:
        logger.warning("Interactive authentication is disabled; DebridPulse is intentionally operating in open mode")
    if password_enabled and not password_auth_ready(cfg):
        logger.error("Username & Password authentication is enabled but not fully configured; that mechanism is unavailable")
    if oidc_enabled:
        from auth.oidc import oidc_auth_ready
        if not oidc_auth_ready(cfg):
            logger.error("OpenID Connect is enabled but its local configuration is incomplete; OIDC is unavailable and protected access remains fail-closed unless another configured mechanism is usable")

    from application.composition import application as default_application
    application = getattr(app.state, "application", default_application)
    app.state.application = application
    await application.engine.initialize()
    await application.engine.recover_postprocessing()
    await application.start_integrations()
    try:
        await application.recover()
    except Exception as exc:
        logger.warning("Startup reconciliation deferred: %s", sanitize_exception(exc))
    await start_scheduler(application)
    session_store.start_cleanup()
    try:
        yield
    finally:
        logger.info("Shutting down %s...", APP_NAME)
        try:
            await session_store.stop_cleanup()
        finally:
            try:
                await stop_scheduler()
            finally:
                try:
                    await application.stop_integrations()
                except Exception as exc:
                    logger.warning("Integration shutdown failed: %s", sanitize_exception(exc))


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or str(scope.get("method") or "").upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        # Pre-authentication parsers must have tighter ceilings than torrent/form
        # application requests. Auth settings are intentionally bounded because
        # the transition state machine reads their body before route validation.
        path = str(scope.get("path") or "")
        if scope.get("path") == "/login":
            limit = min(self.max_bytes, 64 * 1024)
        elif path in {
            "/api/settings",
            "/api/settings/validate-alldebrid",
            "/api/settings/validate-aria2",
            "/api/settings/validate-discord",
            "/api/auth/config",
            "/api/auth/oidc/verify-config",
            "/api/auth/api-token",
        }:
            limit = min(self.max_bytes, 1024 * 1024)
        else:
            limit = self.max_bytes
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"")
        try:
            if raw_length and int(raw_length) > limit:
                response = Response(content="Request body too large", status_code=413)
                await response(scope, receive, send)
                return
        except ValueError:
            pass
        seen = 0
        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _RequestBodyTooLarge
            return message
        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            response = Response(content="Request body too large", status_code=413)
            await response(scope, receive, send)


try:
    _MAX_REQUEST_BODY_BYTES = max(1024 * 1024, min(100 * 1024 * 1024, int(os.getenv("DEBRIDPULSE_MAX_REQUEST_BYTES", str(20 * 1024 * 1024)))))
except ValueError:
    _MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024


app = FastAPI(
    title=APP_METADATA_TITLE,
    description=(
        "Self-hosted DebridPulse runtime built around the Universal Transfer Core for "
        "direct links, magnets, and torrent files. The current multi-provider model "
        "includes AllDebrid and General HTTP(S) acquisition providers with aria2 execution.\n\n"
        "## API structure\n\n"
        "| Prefix | Description |\n"
        "|--------|-------------|\n"
        f"| `/api/` | Native {APP_SHORT_NAME} REST API |\n\n"
        "Interactive docs: `/docs` (Swagger UI) · `/redoc` (ReDoc) · `/openapi.json`"
    ),
    version=read_version(),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(TransferError)
async def transfer_error_handler(_request: Request, exc: TransferError):
    return JSONResponse(status_code=409, content={"detail": exc.error.message, "error": exc.error.as_dict()})


_MUTATING_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DATABASE_WIPE_PATH = "/api/admin/database/wipe"
# These routes own a stronger maintenance admission inside the endpoint. Wrapping
# them in application_operation() here would put the outer request and downstream
# endpoint in different Starlette tasks and make maintenance wait on its own request.
_SELF_MAINTAINED_MUTATION_PATHS = {"/api/settings"}
_AUTH_MUTATION_PATHS = {
    "/login",
    "/api/auth/logout",
    "/api/auth/oidc/verify-config",
}


@app.middleware("http")
async def application_mutation_admission_middleware(request: Request, call_next):
    """Serialize application state changes against destructive maintenance."""
    if (
        request.method.upper() in _MUTATING_HTTP_METHODS
        and request.url.path != _DATABASE_WIPE_PATH
        and request.url.path not in _SELF_MAINTAINED_MUTATION_PATHS
        and request.url.path not in _AUTH_MUTATION_PATHS
    ):
        try:
            async with get_application(request).application_operation():
                return await call_next(request)
        except ApplicationMaintenanceActive:
            return Response(
                content="Application maintenance in progress",
                status_code=503,
                headers={"Retry-After": "2"},
            )
    return await call_next(request)


@app.exception_handler(PermissionError)
async def permission_error_handler(_request: Request, _exc: PermissionError):
    """Do not turn service-layer authorization failures into HTTP 500 responses."""
    return Response(content="Forbidden", status_code=403)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_request: Request, exc: RequestValidationError):
    """Return useful validation locations without reflecting submitted secrets.

    Pydantic validation errors include the invalid ``input`` by default. That is
    unsafe for password/client-secret fields because a rejected credential could
    otherwise be copied into browser, proxy, or diagnostic logs as response
    content. Keep only stable structural metadata and a generic message.
    """
    detail = []
    for error in exc.errors():
        detail.append(
            {
                "type": str(error.get("type") or "validation_error"),
                "loc": list(error.get("loc") or ()),
                "msg": "Invalid request value",
            }
        )
    return JSONResponse(
        status_code=422,
        content={"detail": detail},
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(DatabaseMaintenanceActive)
async def database_maintenance_handler(_request: Request, _exc: DatabaseMaintenanceActive):
    """Fail closed rather than queue stale request work behind a destructive wipe."""
    return Response(
        content="Database maintenance in progress",
        status_code=503,
        headers={"Retry-After": "2"},
    )


@app.exception_handler(ApplicationMaintenanceActive)
async def application_maintenance_handler(_request: Request, _exc: ApplicationMaintenanceActive):
    """Reject new mutation/execution work while destructive maintenance owns admission."""
    return Response(
        content="Application maintenance in progress",
        status_code=503,
        headers={"Retry-After": "2"},
    )


_cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", CSRF_HEADER],
    )

# ── Authentication / Browser Security ─────────────────────────────────────────
# Authentication is an outer request boundary. Browser cross-site mutation
# protection is general security and remains active even when authentication is
# intentionally disabled.

@app.middleware("http")
async def authentication_boundary_middleware(request: Request, call_next):
    return await enforce_authentication(request, call_next)


# Register the pure-ASGI body limiter after Authentication so Starlette places
# it outside the authentication middleware. The auth transition guard reads
# settings request bodies itself; no unbounded/chunked body may reach that read.
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=_MAX_REQUEST_BODY_BYTES)


@app.middleware("http")
async def general_web_security_middleware(request: Request, call_next):
    return await enforce_general_web_security(
        request,
        call_next,
        allowed_origins=_cors_origins,
    )


# ── Request-ID / Baseline Response Security Middleware ────────────────────────
# Registered last so it is the outermost HTTP middleware. This guarantees that
# responses produced directly by the body-limit, browser-security, or
# authentication boundaries receive the same correlation and baseline security
# headers as normal application responses.

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    req_id = str(request.headers.get("X-Request-ID") or "").strip()
    if not req_id or len(req_id) > 128:
        req_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Keep cross-origin referrer disclosure disabled while preserving the real
    # Origin on same-origin HTML form POSTs used by the password login flow.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")

    # Frontend migration assets must never rely on manually bumped ?v= values as
    # their only coherence boundary. Keep normal conditional caching (ETag /
    # Last-Modified) but force browser revalidation of executable/presentation
    # resources so a new container cannot silently run an older JS/CSS/HTML mix.
    path = request.url.path
    static_frontend = (
        request.method.upper() in {"GET", "HEAD"}
        and (path == "/" or path.endswith((".html", ".js", ".css")))
    )
    existing_cache = response.headers.get("Cache-Control", "")
    if static_frontend and "no-store" not in existing_cache.lower():
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# Pending OIDC callback is registered first so verified proposed configuration
# can become authoritative before the replacement application session is issued.
app.include_router(auth_config_router)
app.include_router(auth_router)
app.include_router(settings_validation_router, prefix="/api")
app.include_router(router, prefix="/api")

# ── Static files ──────────────────────────────────────────────────────────────
_here = Path(__file__).parent
_candidates = []

_env = os.getenv("STATIC_DIR", "").strip()
if _env:
    _candidates.append(Path(_env))

_candidates.append(_here.parent / "frontend" / "static")
_candidates.append(Path("/app/frontend/static"))
_candidates.append(Path("/app/static"))


def _is_valid(p: Path) -> bool:
    return p.is_dir() and (p / "index.html").exists()


_static = next((p for p in _candidates if _is_valid(p)), None)

if _static is None:
    tried = ", ".join(str(p) for p in _candidates)
    raise RuntimeError(
        f"Frontend index.html not found. Tried: [{tried}]. "
        "Fix your Docker build or set STATIC_DIR."
    )

logger.info("Serving static files from: %s", sanitize_log_value(_static))
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
