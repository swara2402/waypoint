"""WayPoint application entry point."""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from api.agents import router as agents_router
from api.auth import router as auth_router
from api.deps import require_api_key, require_tenant
from api.investigation import router as investigation_router
from api.knowledge_graph import router as kg_router
from api.memory import router as memory_router
from api.patterns import router as patterns_router
from api.predictions import router as predictions_router
from api.service import router as service_router
from api.connectors import router as connectors_router
from auth.security import COOKIE_NAME, LEGACY_COOKIE_NAME, bootstrap_owner, decode_access_token
from config.logging import configure_logging, get_logger
from config.settings import settings, WAYPOINT_VERSION
from database.session import close_db, init_db
from knowledge_graph.store import KnowledgeGraphStore
from memory.store import MemoryStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("app_startup", extra={"env": settings.app_env, "host": settings.app_host, "port": settings.app_port})
    await init_db()
    await bootstrap_owner()
    KnowledgeGraphStore.get()
    worker_task = None
    if settings.job_worker_enabled:
        from utils.job_queue import LocalWorker
        worker = LocalWorker(poll_interval=settings.job_poll_interval, attempts_max=settings.job_attempts_max)

        async def _run_worker():
            try:
                await worker.run()
            except asyncio.CancelledError:
                raise

        worker_task = asyncio.create_task(_run_worker())
    yield
    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except BaseException:
            pass
    try:
        await KnowledgeGraphStore.get().close()
    except Exception:
        logger.exception("knowledge_graph_close_failed")
    await close_db()


_docs_url = None if settings.is_production else "/docs"
_redoc_url = None if settings.is_production else "/redoc"
_openapi_url = None if settings.is_production else "/openapi.json"
app = FastAPI(title="WayPoint — Incident Intelligence", version=WAYPOINT_VERSION, docs_url=_docs_url, redoc_url=_redoc_url, openapi_url=_openapi_url, lifespan=lifespan)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        # Tenant identity is a server-side authentication property, never a
        # client-selected routing value. Rewrite the header before FastAPI
        # resolves route parameters so legacy endpoints that still declare
        # X-Tenant-Id cannot be tricked into reading another workspace.
        token = request.cookies.get(COOKIE_NAME) or request.cookies.get(LEGACY_COOKIE_NAME)
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if token:
            try:
                claims = decode_access_token(token)
                trusted_tenant = claims.get("tenant_id")
                supplied_tenant = request.headers.get("X-Tenant-Id")
                if trusted_tenant:
                    if supplied_tenant and supplied_tenant != trusted_tenant:
                        return JSONResponse(status_code=403, content={"detail": "Tenant context is controlled by the authenticated session"}, headers={"X-Request-ID": request_id})
                    headers = [(k, v) for k, v in request.scope.get("headers", []) if k.lower() != b"x-tenant-id"]
                    headers.append((b"x-tenant-id", str(trusted_tenant).encode("utf-8")))
                    request.scope["headers"] = headers
            except Exception:
                # Authentication dependency produces the authoritative 401.
                # Do not turn malformed credentials into an information leak.
                pass

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_body_bytes:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"}, headers={"X-Request-ID": request_id})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"}, headers={"X-Request-ID": request_id})
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cache-Control"] = "no-store"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'"
        logger.info("request_finished", extra={"request_id": request_id, "method": request.method, "path": request.url.path, "status": response.status_code, "duration_ms": round((time.perf_counter() - start) * 1000, 2)})
        return response


app.add_middleware(SecurityHeadersMiddleware)
_origins = settings.cors_origins_list
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "X-API-Key", "X-Request-ID", "Idempotency-Key"], expose_headers=["X-Request-ID", "Retry-After"])
elif not settings.is_production:
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:3000", "http://127.0.0.1:8000"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(auth_router)
app.include_router(investigation_router)
app.include_router(patterns_router)
app.include_router(memory_router)
app.include_router(kg_router)
app.include_router(agents_router)
app.include_router(predictions_router)
app.include_router(service_router)
app.include_router(connectors_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "version": WAYPOINT_VERSION, "service": "WayPoint"}


@app.get("/internal/health", tags=["meta"])
async def internal_health(
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> dict:
    kg_store = KnowledgeGraphStore.get()
    memory_store = MemoryStore.get(tenant)
    return {
        "status": "ok",
        "env": settings.app_env,
        "version": WAYPOINT_VERSION,
        "service": "WayPoint",
        "memory_scope": "tenant-bound-lazy",
        "memory_size": memory_store.size(),
        "subsystems": {
            "database": "connected",
            "memory": "lazy",
            "faiss": "tenant-scoped-lazy" if settings.enable_faiss else "disabled",
            "neo4j": "connected" if getattr(kg_store, "_driver", None) else "fallback_mode",
        },
    }


@app.get("/ready", tags=["meta"])
async def readiness() -> Response:
    checks = {"database": False}
    error: str | None = None
    try:
        from database.session import get_async_session_local
        # get_async_session_local() returns the *factory*; it has to be called
        # to get a session. Using the factory itself as the context manager
        # raised TypeError ("does not support the asynchronous context manager
        # protocol"), so /ready reported 503 even with a healthy database.
        session_local = get_async_session_local()
        async with session_local() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        error = type(exc).__name__
        logger.error("readiness_database_failed", extra={"error_type": error})
    payload = {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks}
    if error:
        payload["error"] = error
    return JSONResponse(status_code=200 if all(checks.values()) else 503, content=payload)


@app.get("/register", include_in_schema=False)
async def register_page() -> FileResponse:
    # Registration uses the same auth shell as login; the page switches to
    # workspace-creation mode based on the /register path.
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False, name="ui")
async def ui_root(request: Request):
    if not (request.cookies.get(COOKIE_NAME) or request.cookies.get(LEGACY_COOKIE_NAME)):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/app.js", include_in_schema=False)
async def ui_bundle() -> Response:
    legacy = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    bridge = (STATIC_DIR / "session-bridge.js").read_text(encoding="utf-8")
    service_settings = (STATIC_DIR / "service-settings.js").read_text(encoding="utf-8")
    return Response(bridge + "\n" + legacy + "\n" + service_settings, media_type="application/javascript")


@app.get("/api/info", tags=["meta"])
async def service_info(_api_key: str = Depends(require_api_key)) -> dict:
    return {"name": "WayPoint — Incident Intelligence", "version": WAYPOINT_VERSION, "docs": "/docs" if not settings.is_production else None, "auth": "WayPoint session cookie or tenant-bound service account", "epistemic_model": ["observed", "evidence", "inference", "confirmed"]}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.app_host, port=settings.app_port, reload=not settings.is_production, log_level=settings.app_log_level.lower())
