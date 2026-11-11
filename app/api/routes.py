"""Commands and read models.

Mutations require Idempotency-Key, the demo header, and the login CSRF token.
The shared guard is MutationGuardMiddleware, so a route added later inherits it.
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session
from starlette.datastructures import QueryParams

from app.adapters import storage as storage_module
from app.adapters.ramp import signature_matches
from app.api.guards import COOKIE, new_csrf_token
from app.api.idempotency import begin, finish, stored_status
from app.api.serialize import (
    meta,
    purchase_detail,
    purchase_summary,
    workpaper_detail,
)
from app.clock import utcnow
from app.contracts.jobs import read_job
from app.demo.scenarios import get_scenario, scenario_ids
from app.domain.commands import (
    add_human_fact,
    collect_totals,
    create_workpaper,
    export_workpaper,
    mark_workpaper_ready,
    refer_determination,
    review_determination,
    workpaper_document,
)
from app.domain.errors import DomainError
from app.domain.evaluate import quarter_key
from app.domain.pipeline import ingest_scenario, ingest_webhook
from app.models import (
    AuditEvent,
    DemoSession,
    Determination,
    Document,
    Evidence,
    Fact,
    Hold,
    IdempotencyKey,
    Job,
    JournalEntryDraft,
    LiabilityLine,
    Organization,
    Person,
    Purchase,
    PurchaseLine,
    Question,
    RefundCandidate,
    RefundLetterDraft,
    ReviewDecision,
    SourceEvent,
    Workpaper,
    WorkpaperLine,
)
from app.seed import ORG_ID, PERSON_ID

router = APIRouter()


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScenarioBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str


class FactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: str
    source: str
    reason: str
    expected_version: int
    line_id: str | None = None


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    approval_path: (
        Literal["LIABILITY", "REFUND_PURSUIT", "CHARGED_CORRECTLY"] | None
    ) = None
    reason: str
    expected_version: int


class ReferBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["leave_open", "send_to_advisor"]
    note: str = ""
    expected_version: int


class WorkpaperBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jurisdiction: Literal["CA", "WA", "NY"]
    period: str


class ReadyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    reason: str
    fingerprint: str | None = None


def get_db(request: Request) -> Iterator[Session]:
    session = request.app.state.ctx.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


Db = Annotated[Session, Depends(get_db)]


def _viewer(request: Request, session: Session) -> DemoSession:
    """Actor from the session cookie. Mutation guards run before this."""
    row = session.get(DemoSession, request.cookies.get(COOKIE, ""))
    if row is None:
        raise DomainError("UNAUTHORIZED", "Sign in.", 401)
    return row


def _upgrade_legacy_login(
    session: Session, key: str, stored: dict[str, object]
) -> dict[str, object]:
    """A cached login from before CSRF stored session_id and no token.

    Persist one token on that session and replace the stored body so a later
    replay of the same Idempotency-Key does not mint another token.
    """
    current = stored.get("csrf_token")
    if isinstance(current, str) and current:
        return stored
    session_id = stored.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return stored
    demo = session.get(DemoSession, session_id)
    if demo is None or demo.org_id != ORG_ID:
        return stored
    existing = demo.csrf_token
    if isinstance(existing, str) and existing:
        token = existing
    else:
        token = new_csrf_token()
        demo.csrf_token = token
        session.flush()
    upgraded = {name: value for name, value in stored.items() if name != "session_id"}
    upgraded["csrf_token"] = token
    row = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.org_id == ORG_ID, IdempotencyKey.key == key
        )
    )
    if row is not None:
        row.response_body = json.dumps(upgraded)
    return upgraded


def _login_response(
    request: Request, session: Session, key: str, stored: dict[str, object]
) -> JSONResponse:
    """JSON carries csrf_token and never session_id. The cookie is HttpOnly."""
    stored = _upgrade_legacy_login(session, key, stored)
    body = JSONResponse(
        {key: value for key, value in stored.items() if key != "session_id"}
    )
    token = stored.get("csrf_token")
    if isinstance(token, str) and token:
        demo = session.scalar(
            select(DemoSession).where(DemoSession.csrf_token == token)
        )
        if demo is not None:
            body.set_cookie(
                COOKIE,
                demo.id,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
    return body


def _key(request: Request) -> str | None:
    return request.headers.get("idempotency-key")


@router.post("/demo/login")
def login(body: LoginBody, request: Request, session: Db) -> Response:
    canonical = body.model_dump_json()
    stored = begin(session, ORG_ID, _key(request), canonical)
    if stored is None:
        person = session.get(Person, PERSON_ID)
        if person is None:
            raise DomainError("NOT_FOUND", "Demo reviewer is not seeded.", 500)
        demo = DemoSession(
            id=str(uuid4()),
            person_id=person.id,
            org_id=person.org_id,
            created_at=utcnow(),
            csrf_token=new_csrf_token(),
        )
        session.add(demo)
        session.flush()
        stored = {
            **meta(request.app.state.settings),
            "person_id": person.id,
            "person_name": person.name,
            "csrf_token": demo.csrf_token,
        }
        finish(session, ORG_ID, _key(request) or "", canonical, stored)
    return _login_response(request, session, _key(request) or "", stored)


@router.post("/demo/reset")
def reset(request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = "{}"
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    for model in (
        WorkpaperLine,
        Workpaper,
        JournalEntryDraft,
        RefundLetterDraft,
        RefundCandidate,
        LiabilityLine,
        ReviewDecision,
        Determination,
        Evidence,
        Fact,
        Question,
        Hold,
        Document,
        PurchaseLine,
        Job,
        SourceEvent,
        Purchase,
        AuditEvent,
        IdempotencyKey,
    ):
        session.execute(delete(model))
    session.flush()
    payload: dict[str, object] = {**meta(request.app.state.settings), "reset": True}
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.get("/demo/scenarios")
def scenarios(request: Request, session: Db) -> dict[str, object]:
    _viewer(request, session)
    return {**meta(request.app.state.settings), "scenarios": scenario_ids()}


@router.post("/demo/events")
def demo_event(body: ScenarioBody, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = body.model_dump_json()
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    scenario = get_scenario(body.scenario)
    if scenario is None:
        raise DomainError("NOT_FOUND", "Unknown scenario.", 404)
    result = ingest_scenario(session, scenario, viewer.org_id)
    payload: dict[str, object] = {
        **meta(request.app.state.settings),
        "purchase_id": result.purchase_id,
        "job_id": result.job_id,
        "duplicate": result.duplicate,
        "scenario": body.scenario,
    }
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.post("/webhooks/ramp")
async def webhook(request: Request, session: Db) -> JSONResponse:
    raw = await request.body()
    secret = request.app.state.settings.ramp_webhook_secret
    header = request.headers.get("x-ramp-signature", "")
    if not signature_matches(raw, header, secret):
        raise DomainError("BAD_SIGNATURE", "X-Ramp-Signature did not match.", 401)
    key = _key(request)
    stored = begin(session, ORG_ID, key, raw.decode("utf-8"))
    if stored is not None:
        return JSONResponse(stored)
    try:
        payload_in = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainError("BAD_REQUEST", "Webhook body must be JSON.", 400) from exc
    if not isinstance(payload_in, dict):
        raise DomainError("BAD_REQUEST", "Webhook body must be an object.", 400)
    event_id = payload_in.get("id")
    transaction = payload_in.get("transaction")
    if not isinstance(event_id, str) or not isinstance(transaction, dict):
        raise DomainError(
            "BAD_REQUEST", "Webhook needs an id and a transaction object.", 400
        )
    amount = transaction.get("amount")
    if not isinstance(amount, str):
        raise DomainError(
            "BAD_REQUEST", "Transaction amount must be a decimal string.", 422
        )
    currency = transaction.get("currency_code")
    if not isinstance(currency, str):
        currency = "USD"
    when = transaction.get("user_transaction_time")
    invoice_date = (
        when[:10] if isinstance(when, str) and len(when) >= 10 else "2026-11-15"
    )
    vendor = transaction.get("merchant_name")
    if not isinstance(vendor, str):
        vendor = "Synthetic vendor"
    transaction_id = transaction.get("id")
    if not isinstance(transaction_id, str):
        transaction_id = event_id
    result = ingest_webhook(
        session,
        ORG_ID,
        event_id=event_id,
        transaction_id=transaction_id,
        vendor=vendor,
        amount=amount,
        currency=currency,
        invoice_date=invoice_date,
        signature_ok=True,
    )
    payload: dict[str, object] = {
        **meta(request.app.state.settings),
        "purchase_id": result.purchase_id,
        "job_id": result.job_id,
        "duplicate": result.duplicate,
    }
    finish(session, ORG_ID, key or "", raw.decode("utf-8"), payload)
    return JSONResponse(payload)


log = logging.getLogger("taxleak.api")

_MAX_IMPORT_BYTES = 10 * 1024 * 1024
_MAX_IMPORT_PAGES = 10
_IMPORT_MIME_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "text/csv"}
)
_IMPORT_WINDOW_SECONDS = 60.0
_IMPORT_MAX_PER_WINDOW = 10
_import_hits: dict[str, list[float]] = {}

_PNG_MAGIC = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))
_JPEG_MAGIC = bytes((0xFF, 0xD8, 0xFF))
_PAGE_RE = re.compile(rb"/Type\s*/Page(?!s)")


def _reset_import_rate_limit() -> None:
    """Clear the in-process import quota. Tests only."""
    _import_hits.clear()


def _check_import_rate_limit(org_id: str) -> None:
    now = time.monotonic()
    hits = [
        hit
        for hit in _import_hits.get(org_id, [])
        if now - hit < _IMPORT_WINDOW_SECONDS
    ]
    if len(hits) >= _IMPORT_MAX_PER_WINDOW:
        _import_hits[org_id] = hits
        raise DomainError("RATE_LIMITED", "Too many imports; retry later.", 429)
    hits.append(now)
    _import_hits[org_id] = hits


def _check_import_signature(mime_type: str, data: bytes) -> None:
    """Magic bytes must agree with the declared multipart type."""
    if mime_type == "application/pdf":
        matched = data.startswith(b"%PDF")
    elif mime_type == "image/png":
        matched = data.startswith(_PNG_MAGIC)
    elif mime_type == "image/jpeg":
        matched = data.startswith(_JPEG_MAGIC)
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            matched = False
        else:
            matched = b"\x00" not in data
    if not matched:
        raise DomainError(
            "FILE_SIGNATURE_MISMATCH",
            "File bytes disagree with the declared type.",
            422,
        )


def _import_pages(mime_type: str, data: bytes) -> int:
    """Page count without a PDF library. Uncountable is a 422, never a zero."""
    if mime_type != "application/pdf":
        return 1
    count = len(_PAGE_RE.findall(data))
    if count == 0:
        raise DomainError("UNREADABLE_DOCUMENT", "PDF page count is unreadable.", 422)
    return count


@router.post("/imports", status_code=202)
async def imports(
    request: Request,
    session: Db,
    entity_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    url: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Store one synthetic file and enqueue a kind=import job.

    Enqueued with kind=import for #49 to process. Nothing here parses
    the file, calls the model, or creates a purchase or determination.
    """
    viewer = _viewer(request, session)
    entity = session.get(Organization, entity_id)
    if entity is None or entity.id != viewer.org_id:
        raise DomainError("NOT_FOUND", "Entity not found.", 404)
    data = await file.read()
    digest = hashlib.sha256(data).hexdigest()
    declared = (file.content_type or "").split(";")[0].strip().lower()
    normalized_url = url or None
    canonical = json.dumps(
        {
            "entity_id": entity_id,
            "sha256": digest,
            "mime_type": declared,
            "filename": file.filename,
            "url": normalized_url,
        }
    )
    key = _key(request)
    stored = begin(session, viewer.org_id, key, canonical)
    if stored is not None:
        status = stored_status(session, viewer.org_id, key or "")
        return JSONResponse(stored, status_code=status)
    if normalized_url is not None:
        raise DomainError(
            "URL_NOT_ALLOWED",
            "URL ingestion is not accepted; upload the file.",
            422,
        )
    _check_import_rate_limit(viewer.org_id)
    if declared not in _IMPORT_MIME_TYPES:
        raise DomainError(
            "UNSUPPORTED_FILE_TYPE",
            "Only PDF, PNG, JPEG, or CSV uploads are accepted.",
            422,
        )
    if len(data) > _MAX_IMPORT_BYTES:
        raise DomainError("FILE_TOO_LARGE", "Document exceeds 10 MB.", 413)
    _check_import_signature(declared, data)
    pages = _import_pages(declared, data)
    if pages > _MAX_IMPORT_PAGES:
        raise DomainError("FILE_TOO_LARGE", "Document exceeds 10 pages.", 413)
    store = getattr(request.app.state, "storage", None) or storage_module
    placed = store.put_document(data, declared)
    document = Document(
        id=str(uuid4()),
        purchase_id=None,
        blob_key=placed.key,
        sha256=digest,
        mime_type=declared,
        pages=pages,
    )
    session.add(document)
    job = Job(
        id=str(uuid4()),
        org_id=viewer.org_id,
        kind="import",
        purchase_id=None,
        status="QUEUED",
        lease_until=None,
        attempts=0,
        error_code=None,
        created_at=utcnow(),
    )
    session.add(job)
    session.flush()
    log.info(
        "import stored import_id=%s job_id=%s sha256=%s size=%d mime=%s pages=%d",
        document.id,
        job.id,
        digest,
        len(data),
        declared,
        pages,
    )
    payload: dict[str, object] = {
        **meta(request.app.state.settings),
        "import_id": document.id,
        "job_id": job.id,
    }
    finish(session, viewer.org_id, key or "", canonical, payload, 202)
    return JSONResponse(payload, status_code=202)


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    row = session.get(Job, job_id)
    if row is None or row.org_id != viewer.org_id:
        raise DomainError("NOT_FOUND", "Job not found.", 404)
    body = read_job(
        id=row.id,
        status=row.status,
        purchase_id=row.purchase_id,
        attempts=row.attempts,
        error_code=row.error_code,
        stage=row.kind,
    )
    return {**meta(request.app.state.settings), **body.model_dump(mode="json")}


@router.get("/audit-events")
def audit_events(
    request: Request,
    session: Db,
    object_id: str | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    viewer = _viewer(request, session)
    query = select(AuditEvent).where(AuditEvent.org_id == viewer.org_id)
    if object_id is not None:
        query = query.where(AuditEvent.object_id == object_id)
    if cursor is not None:
        anchor = session.scalar(query.where(AuditEvent.id == cursor))
        if anchor is None:
            raise DomainError("NOT_FOUND", "Audit cursor not found.", 404)
        query = query.where(
            or_(
                AuditEvent.created_at > anchor.created_at,
                and_(
                    AuditEvent.created_at == anchor.created_at,
                    AuditEvent.id > anchor.id,
                ),
            )
        )
    rows = list(
        session.scalars(
            query.order_by(AuditEvent.created_at, AuditEvent.id).limit(101)
        ).all()
    )
    page = rows[:100]
    return {
        **meta(request.app.state.settings),
        "audit_events": [
            {
                "id": row.id,
                "actor_id": row.actor_id,
                "command": row.command,
                "object_type": row.object_type,
                "object_id": row.object_id,
                "object_version": row.object_version,
                "before_hash": row.before_hash,
                "after_hash": row.after_hash,
                "created_at": row.created_at,
            }
            for row in page
        ],
        "next_cursor": page[-1].id if len(rows) > 100 else None,
    }


@router.get("/purchases")
def purchases(request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    listing = _filtered_purchases(session, viewer.org_id, request.query_params)
    return {
        **meta(request.app.state.settings),
        "purchases": [purchase_summary(session, row) for row in listing.rows],
        "next_cursor": listing.next_cursor,
    }


@router.get("/purchases/{purchase_id}")
def purchase(purchase_id: str, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    row = session.get(Purchase, purchase_id)
    if row is None or row.org_id != viewer.org_id:
        raise DomainError("NOT_FOUND", "Purchase not found.", 404)
    return purchase_detail(session, row, request.app.state.settings)


@router.post("/purchases/{purchase_id}/facts")
def facts(
    purchase_id: str, body: FactBody, request: Request, session: Db
) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = body.model_dump_json()
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    add_human_fact(
        session,
        org_id=viewer.org_id,
        actor_id=viewer.person_id,
        purchase_id=purchase_id,
        field=body.field,
        value=body.value,
        source=body.source,
        reason=body.reason,
        expected_version=body.expected_version,
        line_id=body.line_id,
    )
    row = session.get(Purchase, purchase_id)
    if row is None:
        raise DomainError("NOT_FOUND", "Purchase not found.", 404)
    payload = purchase_detail(session, row, request.app.state.settings)
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.post("/determinations/{determination_id}/review")
def review(
    determination_id: str, body: ReviewBody, request: Request, session: Db
) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = body.model_dump_json()
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    determination = review_determination(
        session,
        org_id=viewer.org_id,
        actor_id=viewer.person_id,
        determination_id=determination_id,
        action=body.action,
        approval_path=body.approval_path,
        reason=body.reason,
        expected_version=body.expected_version,
        rules_dir=request.app.state.settings.rules_dir,
    )
    row = session.get(Purchase, determination.purchase_id)
    if row is None:
        raise DomainError("NOT_FOUND", "Purchase not found.", 404)
    payload = purchase_detail(session, row, request.app.state.settings)
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.post("/determinations/{determination_id}/refer")
def refer(
    determination_id: str, body: ReferBody, request: Request, session: Db
) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = body.model_dump_json()
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    determination = refer_determination(
        session,
        org_id=viewer.org_id,
        actor_id=viewer.person_id,
        determination_id=determination_id,
        action=body.action,
        note=body.note,
        expected_version=body.expected_version,
    )
    row = session.get(Purchase, determination.purchase_id)
    if row is None:
        raise DomainError("NOT_FOUND", "Purchase not found.", 404)
    payload = purchase_detail(session, row, request.app.state.settings)
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.get("/totals")
def totals(request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    return {
        **meta(request.app.state.settings),
        **collect_totals(session, viewer.org_id),
    }


@router.get("/drafts/journal-entries")
def journals(request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    rows = []
    for draft in session.scalars(select(JournalEntryDraft)).all():
        purchase = session.get(Purchase, draft.purchase_id)
        if purchase is None or purchase.org_id != viewer.org_id:
            continue
        rows.append(
            {
                "id": draft.id,
                "purchase_id": draft.purchase_id,
                "debit_account": draft.debit_account,
                "credit_account": draft.credit_account,
                "amount": {"value": draft.amount, "currency": "USD"},
                "status": draft.status,
            }
        )
    return {**meta(request.app.state.settings), "drafts": rows}


@router.get("/drafts/refund-letters/{letter_id}")
def refund_letter(letter_id: str, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    letter = session.get(RefundLetterDraft, letter_id)
    if letter is None:
        raise DomainError("NOT_FOUND", "Letter not found.", 404)
    purchase = session.get(Purchase, letter.purchase_id)
    if purchase is None or purchase.org_id != viewer.org_id:
        raise DomainError("NOT_FOUND", "Letter not found.", 404)
    return {
        **meta(request.app.state.settings),
        "id": letter.id,
        "vendor": letter.vendor,
        "invoice_number": letter.invoice_number,
        "amount": {"value": letter.amount, "currency": "USD"},
        "basis_text": letter.basis_text,
        "basis_note": (
            "This draft is not sent."
            if letter.basis_text
            else "Basis text is not signed. This draft is not sent."
        ),
        "status": letter.status,
    }


@router.post("/workpapers")
def workpapers(body: WorkpaperBody, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = body.model_dump_json()
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    workpaper = create_workpaper(session, viewer.org_id, body.jurisdiction, body.period)
    payload = _workpaper_json(request, session, workpaper)
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.post("/workpapers/{workpaper_id}/mark-ready")
def mark_ready(
    workpaper_id: str, body: ReadyBody, request: Request, session: Db
) -> dict[str, object]:
    viewer = _viewer(request, session)
    canonical = body.model_dump_json()
    stored = begin(session, viewer.org_id, _key(request), canonical)
    if stored is not None:
        return stored
    workpaper = mark_workpaper_ready(
        session,
        org_id=viewer.org_id,
        actor_id=viewer.person_id,
        workpaper_id=workpaper_id,
        expected_version=body.expected_version,
        reason=body.reason,
        fingerprint=body.fingerprint,
    )
    payload = _workpaper_json(request, session, workpaper)
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.get("/workpapers/{workpaper_id}")
def workpaper(workpaper_id: str, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    row = session.get(Workpaper, workpaper_id)
    if row is None or row.org_id != viewer.org_id:
        raise DomainError("NOT_FOUND", "Workpaper not found.", 404)
    return {
        **meta(request.app.state.settings),
        **workpaper_detail(session, row),
    }


@router.get("/workpapers/{workpaper_id}/export")
def export(
    workpaper_id: str,
    request: Request,
    session: Db,
    export_format: Annotated[Literal["csv", "json"], Query(alias="format")],
) -> Response:
    viewer = _viewer(request, session)
    settings = request.app.state.settings
    media_type, body = export_workpaper(
        session,
        viewer.org_id,
        workpaper_id,
        export_format,
        data_mode=settings.data_mode.value,
        engine_mode=settings.engine_mode.value,
    )
    return Response(content=body, media_type=media_type)


_PAGE_MAX = 100
_PERIOD_RE = re.compile(r"^\d{4}Q[1-4]$")
_LABELS = frozenset({"ENGINE_SUPPORTED", "NEEDS_HUMAN_REVIEW"})
_DIRECTIONS = frozenset({"LIABILITY", "REFUND_CANDIDATE", "CHARGED_CORRECTLY"})
_REVIEW_STATUSES = frozenset(
    {
        "READY_FOR_REVIEW",
        "APPROVED",
        "APPROVED_FOR_PURSUIT",
        "REJECTED",
        "DETERMINATION_ERROR",
        "NHR_ENGINE",
        "SENT_TO_ADVISOR",
        "SUPERSEDED",
    }
)


class _Listing:
    def __init__(self, rows: list[Purchase], next_cursor: str | None) -> None:
        self.rows = rows
        self.next_cursor = next_cursor


def _query_value(params: QueryParams, *names: str) -> str | None:
    found: list[str] = []
    for name in names:
        found.extend(params.getlist(name))
    if not found:
        return None
    if len(found) > 1:
        raise DomainError("BAD_REQUEST", "Duplicate query parameter.", 422)
    return found[0]


def _page_limit(params: QueryParams) -> int:
    raw = _query_value(params, "limit", "page_size", "per_page")
    if raw is None:
        return _PAGE_MAX
    try:
        limit = int(raw)
    except ValueError:
        raise DomainError("BAD_REQUEST", "Unknown page size.", 422) from None
    if limit < 1:
        raise DomainError("BAD_REQUEST", "Unknown page size.", 422)
    return min(limit, _PAGE_MAX)


def _hold_filter(params: QueryParams) -> bool | None:
    raw = _query_value(params, "hold")
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise DomainError("BAD_REQUEST", "Unknown hold value.", 422)


def _filtered_purchases(session: Session, org_id: str, params: QueryParams) -> _Listing:
    limit = _page_limit(params)
    hold = _hold_filter(params)
    period = _query_value(params, "period")
    if period is not None and not _PERIOD_RE.fullmatch(period):
        raise DomainError("BAD_REQUEST", "Unknown period.", 422)
    label = _query_value(params, "label")
    if label is not None and label not in _LABELS:
        raise DomainError("BAD_REQUEST", "Unknown label.", 422)
    direction = _query_value(params, "direction")
    if direction is not None and direction not in _DIRECTIONS:
        raise DomainError("BAD_REQUEST", "Unknown direction.", 422)
    review = _query_value(params, "review_status", "status", "unit_status")
    if review is not None and review not in _REVIEW_STATUSES:
        raise DomainError("BAD_REQUEST", "Unknown review status.", 422)
    cursor = _query_value(params, "cursor")
    if cursor == "":
        cursor = None

    rows = list(
        session.scalars(select(Purchase).where(Purchase.org_id == org_id)).all()
    )
    rows.sort(key=lambda row: (row.created_at, row.id))
    by_id = {row.id: row for row in rows}

    holds = (
        list(session.scalars(select(Hold).where(Hold.purchase_id.in_(by_id))).all())
        if by_id
        else []
    )
    held_ids = {hold.purchase_id for hold in holds if hold.active}
    determinations = (
        list(
            session.scalars(
                select(Determination).where(
                    Determination.purchase_id.in_(by_id),
                    Determination.is_current.is_(True),
                )
            ).all()
        )
        if by_id
        else []
    )
    current: dict[str, list[Determination]] = {row.id: [] for row in rows}
    for determination in determinations:
        current.setdefault(determination.purchase_id, []).append(determination)
    history: dict[str, list[Determination]] = {}
    if review == "SUPERSEDED" and by_id:
        for determination in session.scalars(
            select(Determination).where(
                Determination.purchase_id.in_(by_id),
                Determination.unit_status == "SUPERSEDED",
            )
        ).all():
            history.setdefault(determination.purchase_id, []).append(determination)

    kept: list[Purchase] = []
    for row in rows:
        if period is not None and _invoice_quarter(row.invoice_date) != period:
            continue
        active_hold = row.id in held_ids
        if hold is True and not active_hold:
            continue
        if hold is False and active_hold:
            continue
        rows_current = current.get(row.id, [])
        if label is not None and not any(item.label == label for item in rows_current):
            continue
        if direction is not None and not any(
            item.direction == direction for item in rows_current
        ):
            continue
        if review is not None:
            if review == "SUPERSEDED":
                pool = list(rows_current) + history.get(row.id, [])
                if not any(item.unit_status == "SUPERSEDED" for item in pool):
                    continue
            elif not any(item.unit_status == review for item in rows_current):
                continue
        kept.append(row)

    start = 0
    if cursor is not None:
        if cursor.isdigit():
            try:
                start = int(cursor)
            except ValueError:
                raise DomainError("BAD_REQUEST", "Unknown cursor.", 422) from None
            if start < 0 or start > len(kept):
                raise DomainError("BAD_REQUEST", "Unknown cursor.", 422)
        else:
            cursor_row = session.get(Purchase, cursor)
            if cursor_row is None or cursor_row.org_id != org_id:
                raise DomainError("BAD_REQUEST", "Unknown cursor.", 422)
            cursor_key = (cursor_row.created_at, cursor_row.id)
            start = len(kept)
            for index, row in enumerate(kept):
                if (row.created_at, row.id) > cursor_key:
                    start = index
                    break
    page = kept[start : start + limit]
    next_cursor = page[-1].id if start + limit < len(kept) else None
    return _Listing(page, next_cursor)


def _invoice_quarter(invoice_date: str) -> str | None:
    try:
        return quarter_key(date.fromisoformat(invoice_date))
    except ValueError:
        return None


def _workpaper_json(
    request: Request, session: Session, workpaper: Workpaper
) -> dict[str, object]:
    return {
        **meta(request.app.state.settings),
        **workpaper_document(session, workpaper),
    }
