"""One guard for every state-changing request, including routes added later.

GET, HEAD, OPTIONS, and POST /webhooks/ramp are unchanged. Login issues
the CSRF token and does not require it. The session cookie is not the token.
"""

import secrets
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.domain.errors import DomainError
from app.models import DemoSession

COOKIE = "taxleak_session"
CSRF_HEADER = "X-CSRF-Token"
_MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_WEBHOOK_PATH = "/api/v1/webhooks/ramp"
_LOGIN_PATH = "/api/v1/demo/login"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def domain_error_response(exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "field_errors": exc.field_errors,
                "retryable": exc.retryable,
                "correlation_id": uuid4().hex,
            }
        },
    )


def enforce_mutation_guards(request: Request) -> None:
    """Refuse a mutation before its command runs. Reads do not reach here."""
    if request.method not in _MUTATIONS or request.url.path == _WEBHOOK_PATH:
        return
    if request.headers.get("x-requested-with") != "taxleak":
        raise DomainError("FORBIDDEN", "Missing X-Requested-With: taxleak.", 403)
    _require_origin(request)
    if request.url.path != _LOGIN_PATH:
        _require_csrf(request)


def _require_origin(request: Request) -> None:
    allowlist: tuple[str, ...] = request.app.state.settings.origin_allowlist
    if not allowlist:
        return
    origin = request.headers.get("origin")
    if origin not in allowlist:
        raise DomainError("FORBIDDEN", "Origin is not allowed.", 403)


def _require_csrf(request: Request) -> None:
    provided = request.headers.get(CSRF_HEADER)
    if not provided:
        raise DomainError("FORBIDDEN", "Missing CSRF token.", 403)
    session_id = request.cookies.get(COOKIE, "")
    if not session_id or not _token_matches(request, session_id, provided):
        raise DomainError("FORBIDDEN", "CSRF token does not match this session.", 403)


def _token_matches(request: Request, session_id: str, provided: str) -> bool:
    db = request.app.state.ctx.session_factory()
    try:
        row = db.get(DemoSession, session_id)
        expected = row.csrf_token if row is not None else None
        return _equal(provided, expected)
    finally:
        db.close()


def _equal(provided: str, expected: str | None) -> bool:
    if not expected:
        return False
    try:
        return secrets.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


class MutationGuardMiddleware:
    """ASGI middleware so a route registered later still passes the guard.

    The request body is not read here; the downstream app still owns it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            try:
                enforce_mutation_guards(Request(scope))
            except DomainError as exc:
                await domain_error_response(exc)(scope, receive, send)
                return
        await self.app(scope, receive, send)
