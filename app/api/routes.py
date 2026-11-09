"""Commands and read models. Mutations require Idempotency-Key and a demo header."""

import json
from collections.abc import Iterator
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.ramp import signature_matches
from app.api.idempotency import begin, finish
from app.api.serialize import meta, purchase_detail, purchase_summary
from app.clock import utcnow
from app.demo.scenarios import get_scenario, scenario_ids
from app.domain.commands import (
    add_human_fact,
    collect_totals,
    create_workpaper,
    export_workpaper,
    mark_workpaper_ready,
    refer_determination,
    review_determination,
)
from app.domain.errors import DomainError
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
COOKIE = "taxleak_session"


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


class WorkpaperBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jurisdiction: Literal["CA", "WA", "NY"]
    period: str


class ReadyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    reason: str


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


def _header(request: Request) -> None:
    if request.headers.get("x-requested-with") != "taxleak":
        raise DomainError("FORBIDDEN", "Missing X-Requested-With: taxleak.", 403)


def _viewer(request: Request, session: Session) -> DemoSession:
    if request.method != "GET":
        _header(request)
    row = session.get(DemoSession, request.cookies.get(COOKIE, ""))
    if row is None:
        raise DomainError("UNAUTHORIZED", "Sign in.", 401)
    return row


def _key(request: Request) -> str | None:
    return request.headers.get("idempotency-key")


@router.post("/demo/login")
def login(body: LoginBody, request: Request, session: Db) -> Response:
    _header(request)
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
        )
        session.add(demo)
        session.flush()
        stored = {
            **meta(request.app.state.settings),
            "person_id": person.id,
            "person_name": person.name,
            "session_id": demo.id,
        }
        finish(session, ORG_ID, _key(request) or "", canonical, stored)
    response = JSONResponse(stored)
    session_id = stored.get("session_id")
    if isinstance(session_id, str):
        response.set_cookie(COOKIE, session_id, httponly=True, samesite="lax", path="/")
    return response


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
    key = _key(request)
    stored = begin(session, ORG_ID, key, raw.decode("utf-8"))
    if stored is not None:
        return JSONResponse(stored)
    secret = request.app.state.settings.ramp_webhook_secret
    header = request.headers.get("x-ramp-signature", "")
    ok = signature_matches(raw, header, secret)
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
    if not ok:
        ingest_webhook(
            session,
            ORG_ID,
            event_id=event_id,
            transaction_id=transaction_id,
            vendor=vendor,
            amount=amount,
            currency=currency,
            invoice_date=invoice_date,
            signature_ok=False,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "BAD_SIGNATURE",
                    "message": "X-Ramp-Signature did not match.",
                    "field_errors": [],
                    "retryable": False,
                    "correlation_id": uuid4().hex,
                }
            },
        )
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


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    row = session.get(Job, job_id)
    if row is None or row.org_id != viewer.org_id:
        raise DomainError("NOT_FOUND", "Job not found.", 404)
    return {
        **meta(request.app.state.settings),
        "id": row.id,
        "status": row.status,
        "purchase_id": row.purchase_id,
        "attempts": row.attempts,
        "error_code": row.error_code,
    }


@router.get("/purchases")
def purchases(request: Request, session: Db) -> dict[str, object]:
    viewer = _viewer(request, session)
    rows = list(
        session.scalars(
            select(Purchase)
            .where(Purchase.org_id == viewer.org_id)
            .order_by(Purchase.created_at)
        ).all()
    )
    return {
        **meta(request.app.state.settings),
        "purchases": [purchase_summary(session, row) for row in rows],
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
        "basis_note": "Basis text is not signed. This draft is not sent.",
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
    payload = _workpaper_json(request, workpaper)
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
    )
    payload = _workpaper_json(request, workpaper)
    finish(session, viewer.org_id, _key(request) or "", canonical, payload)
    return payload


@router.get("/workpapers/{workpaper_id}/export")
def export(workpaper_id: str, request: Request, session: Db) -> Response:
    viewer = _viewer(request, session)
    text = export_workpaper(session, viewer.org_id, workpaper_id)
    return Response(content=text, media_type="text/csv")


def _workpaper_json(request: Request, workpaper: Workpaper) -> dict[str, object]:
    return {
        **meta(request.app.state.settings),
        "id": workpaper.id,
        "entity_name": workpaper.entity_name,
        "jurisdiction": workpaper.jurisdiction,
        "period": workpaper.period,
        "version": workpaper.version,
        "status": workpaper.status,
        "fingerprint": workpaper.fingerprint,
        "frozen_at": workpaper.frozen_at,
    }
