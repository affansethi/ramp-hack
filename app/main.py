"""FastAPI app factory. Serves the API and the static frontend from one
origin. Startup mode guards run here: the app refuses to start unless
DATA_MODE=synthetic and ENGINE_MODE=mock_reviewed_ruleset.

Run:
    uvicorn app.main:app --reload                                   # local dev
    gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app     # hosted

Exactly one worker process: the background job poller runs in-process.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import health, routes
from app.config import Settings, load_settings
from app.context import build_context
from app.contracts.errors import ErrorBody, ErrorResponse
from app.domain.errors import DomainError
from app.worker.poller import run_until_stopped

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None, *, start_worker: bool = True
) -> FastAPI:
    if settings is None:
        settings = load_settings()
    ctx = build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import threading

        stop = threading.Event()
        thread = threading.Thread(
            target=run_until_stopped,
            args=(app.state.ctx, stop),
            name="taxleak-poller",
            daemon=True,
        )
        thread.start()
        yield
        stop.set()
        thread.join(timeout=2)

    app = FastAPI(
        title="Tax Leak",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan if start_worker else None,
    )
    app.state.settings = settings
    app.state.ctx = ctx
    app.add_exception_handler(DomainError, _domain_error)
    app.include_router(health.router)
    app.include_router(routes.router, prefix="/api/v1")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(exc, DomainError):
        raise exc
    payload = ErrorResponse(
        error=ErrorBody(
            code=exc.code,
            message=exc.message,
            field_errors=exc.field_errors,
            retryable=exc.retryable,
            correlation_id=uuid4().hex,
        )
    )
    return JSONResponse(status_code=exc.status, content=payload.model_dump(mode="json"))


app = create_app()
