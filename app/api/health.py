"""Health endpoint: reports modes and storage, never calls the model.

Architecture §4: database, storage, and modes. When storage is not enabled
and no Azure credential is configured, storage is ``skipped`` and
DefaultAzureCredential is not constructed. ``unavailable`` is reserved for a
configured check that failed. The HTTP status stays 200.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.adapters import storage
from app.config import Settings
from app.db import database_ok

router = APIRouter(tags=["health"])

StorageReport = Literal["ok", "unavailable", "skipped"]


class HealthResponse(BaseModel):
    status: str
    data_mode: str
    engine_mode: str
    demo_mode: str
    llm_mode: str
    llm_replay: bool
    database: str
    storage: StorageReport


def _storage_report() -> StorageReport:
    if storage.storage_enabled():
        return "ok" if storage.check_container() else "unavailable"
    if not storage.credentials_configured():
        return "skipped"
    try:
        reachable = storage.probe_container()
    except Exception:
        return "unavailable"
    return "ok" if reachable else "unavailable"


def _health_payload(
    settings: Settings, database: str, storage_status: StorageReport
) -> HealthResponse:
    return HealthResponse(
        status="ok",
        data_mode=settings.data_mode.value,
        engine_mode=settings.engine_mode.value,
        demo_mode=settings.demo_mode.value,
        llm_mode=settings.llm_mode.value,
        llm_replay=settings.llm_replay,
        database=database,
        storage=storage_status,
    )


@router.get("/health")
@router.get("/api/v1/health", include_in_schema=False)
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    ok = database_ok(request.app.state.ctx.session_factory)
    return _health_payload(settings, "ok" if ok else "unavailable", _storage_report())
