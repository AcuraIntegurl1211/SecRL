from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from secrl_platform import __version__ as PLATFORM_VERSION
from secrl_platform.api.dependencies import ApiContext
from secrl_platform.agents.service import AgentServiceTransport
from secrl_platform.api.errors import ApiError, error_payload
from secrl_platform.api.routes import (
    artifacts,
    auth,
    overview,
    preflight,
    resources,
    runs,
    tasks,
)
from secrl_platform.auth.sessions import SessionStore
from secrl_platform.benchmarks.secrl import SecRLMySQLQueryExecutor
from secrl_platform.config import (
    DEFAULT_AGENT_SERVICE_ALLOWLIST,
    DEFAULT_MODEL_PROVIDER_ALLOWLIST,
    Settings,
)
from secrl_platform.models.secrets import SecretStore
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session


logger = logging.getLogger("secrl_platform.api")


def _enforce_dev_autoauth_fence(settings: Settings) -> None:
    if not settings.dev_autoauth:
        return
    if (settings.dev_autoauth_confirm or "").strip().lower() != "yes":
        raise RuntimeError(
            "SECRL_DEV_AUTOAUTH requires SECRL_DEV_AUTOAUTH_CONFIRM=yes; "
            "auto-authentication is restricted to local development use"
        )
    logger.warning(
        "Dev auto-authentication is ENABLED: every request is served as the "
        "local admin without credentials; never enable outside a private "
        "development machine"
    )


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    artifact_store: LocalArtifactStore | None = None,
    model_provider_resolver: Callable[[str, int], object] | None = None,
    agent_service_transport: AgentServiceTransport | None = None,
    agent_service_resolver: Callable[[str, int], object] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if session_factory is None:
            effective_settings = settings or Settings()
            _enforce_dev_autoauth_fence(effective_settings)
            _upgrade_database(effective_settings.database_path)
            app.state.api_context = _context(
                create_engine_and_session(effective_settings.database_path),
                LocalArtifactStore(effective_settings.artifact_dir),
                effective_settings,
                model_provider_resolver,
                agent_service_transport,
                agent_service_resolver,
                enable_environment_probe=True,
            )
        yield

    app = FastAPI(
        title="SecRL Lite API",
        version=PLATFORM_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if settings is not None:
        _enforce_dev_autoauth_fence(settings)
    if session_factory is not None:
        app.state.api_context = _context(
            session_factory,
            artifact_store or LocalArtifactStore("/tmp/secrl-lite-artifacts"),
            settings,
            model_provider_resolver,
            agent_service_transport,
            agent_service_resolver,
            enable_environment_probe=False,
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception as error:
            diagnostic = _safe_exception_context(error)
            logger.error(
                "Unhandled API error request_id=%s exception_type=%s frames=%s",
                request.state.request_id,
                diagnostic["exception_type"],
                ">".join(diagnostic["frames"]),
            )
            normalized = ApiError(500, "INTERNAL_ERROR", "Internal server error")
            response = JSONResponse(
                status_code=normalized.status_code,
                content=error_payload(normalized, request.state.request_id),
            )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError):
        return JSONResponse(
            status_code=error.status_code,
            content=error_payload(error, request.state.request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, _error: RequestValidationError):
        error = ApiError(422, "INVALID_REQUEST", "Request validation failed")
        return JSONResponse(
            status_code=error.status_code,
            content=error_payload(error, request.state.request_id),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException):
        code = "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        normalized = ApiError(error.status_code, code, "Request could not be completed")
        return JSONResponse(
            status_code=normalized.status_code,
            content=error_payload(normalized, request.state.request_id),
        )

    @app.get("/api/v1/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(resources.router, prefix="/api/v1")
    app.include_router(preflight.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(overview.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")

    def custom_openapi() -> dict:
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        schemas["ErrorEnvelope"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "details", "request_id"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {"type": "object"},
                        "request_id": {"type": "string", "format": "uuid"},
                    },
                }
            },
        }
        error_ref = {"$ref": "#/components/schemas/ErrorEnvelope"}
        for path in document.get("paths", {}).values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for status, response in operation.get("responses", {}).items():
                    if status.isdigit() and int(status) >= 400:
                        response["content"] = {
                            "application/json": {"schema": error_ref}
                        }
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
        app.openapi_schema = document
        return document

    app.openapi = custom_openapi
    return app


def _safe_exception_context(error: Exception) -> dict[str, object]:
    frames = traceback.extract_tb(error.__traceback__)[-12:]
    return {
        "exception_type": type(error).__name__,
        "frames": tuple(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in frames
        ),
    }


def _context(
    session_factory: sessionmaker[Session],
    artifact_store: LocalArtifactStore,
    settings: Settings | None,
    model_provider_resolver: Callable[[str, int], object] | None,
    agent_service_transport: AgentServiceTransport | None,
    agent_service_resolver: Callable[[str, int], object] | None,
    enable_environment_probe: bool,
) -> ApiContext:
    environment_probe_fn = None
    if (
        enable_environment_probe
        and settings is not None
        and settings.secrl_runtime_enabled
        and settings.secrl_mysql_password is not None
        and bool(settings.secrl_mysql_password.get_secret_value())
    ):
        executor = SecRLMySQLQueryExecutor(
            user=settings.secrl_mysql_user,
            password=settings.secrl_mysql_password.get_secret_value(),
            database=settings.secrl_mysql_database,
        )

        def probe(incident_ids: tuple[str, ...]) -> dict[str, bool]:
            availability: dict[str, bool] = {}
            for incident_id in incident_ids:
                result = executor.query_sql(incident_id, "SELECT 1")
                availability[incident_id] = (
                    isinstance(result, tuple)
                    and len(result) == 2
                    and result[1] is True
                )
            return availability

        environment_probe_fn = probe
    return ApiContext(
        session_factory=session_factory,
        artifact_store=artifact_store,
        sessions=SessionStore(session_factory),
        model_provider_allowlist=(
            settings.model_provider_allowlist
            if settings is not None
            else DEFAULT_MODEL_PROVIDER_ALLOWLIST
        ),
        model_provider_resolver=model_provider_resolver,
        agent_service_allowlist=(
            settings.agent_service_allowlist
            if settings is not None
            else DEFAULT_AGENT_SERVICE_ALLOWLIST
        ),
        agent_service_transport=agent_service_transport,
        agent_service_resolver=agent_service_resolver,
        secret_store=(
            SecretStore(bytes.fromhex(settings.master_key))
            if settings is not None
            else None
        ),
        secrl_runtime_enabled=bool(
            settings is not None
            and settings.secrl_runtime_enabled
            and settings.secrl_mysql_password is not None
            and bool(settings.secrl_mysql_password.get_secret_value())
        ),
        dev_autoauth=bool(settings is not None and settings.dev_autoauth),
        secrl_environment_probe=environment_probe_fn,
        runner_configured=(settings is None or settings.runner_poll_seconds > 0),
    )


def _upgrade_database(database_path: Path) -> None:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    repository_root = _migration_repository_root()
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")


def _migration_repository_root(
    *,
    module_file: Path = Path(__file__),
    working_directory: Path | None = None,
) -> Path:
    candidates = (
        Path(module_file).resolve().parents[2],
        Path(working_directory or Path.cwd()).resolve(),
    )
    for candidate in candidates:
        if (
            (candidate / "alembic.ini").is_file()
            and (candidate / "alembic" / "env.py").is_file()
        ):
            return candidate
    raise RuntimeError("Alembic migration files are not available")
