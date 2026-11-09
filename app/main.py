"""FastAPI app factory. Serves the API and the static frontend from one
origin. Startup mode guards run here: the app refuses to start unless
DATA_MODE=synthetic and ENGINE_MODE=mock_reviewed_ruleset.

Run:
    uvicorn app.main:app --reload                                   # local dev
    gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app     # hosted

Exactly one worker process: the background job poller runs in-process.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import health
from app.config import Settings, load_settings

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()  # raises ModeGuardError on bad/missing modes
    app = FastAPI(title="Tax Leak", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.include_router(health.router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
