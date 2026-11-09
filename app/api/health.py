"""Health endpoint: reports modes, never calls the model (architecture §4)."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import Settings
from app.db import database_ok

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    data_mode: str
    engine_mode: str
    demo_mode: str
    llm_mode: str
    llm_replay: bool
    database: str


def _health_payload(settings: Settings, database: str) -> HealthResponse:
    return HealthResponse(
        status="ok",
        data_mode=settings.data_mode.value,
        engine_mode=settings.engine_mode.value,
        demo_mode=settings.demo_mode.value,
        llm_mode=settings.llm_mode.value,
        llm_replay=settings.llm_replay,
        database=database,
    )


@router.get("/health")
@router.get("/api/v1/health", include_in_schema=False)
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    ok = database_ok(request.app.state.ctx.session_factory)
    return _health_payload(settings, "ok" if ok else "unavailable")
