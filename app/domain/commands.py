"""Review, fact corrections, totals, and the simulated workpaper."""

import csv
import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.domain.errors import DomainError
from app.domain.evaluate import quarter_key
from app.domain.moneyutil import parse_decimal
from app.domain.pipeline import enqueue_advance
from app.models import (
    AuditEvent,
    Determination,
    Document,
    Evidence,
    Fact,
    Hold,
    JournalEntryDraft,
    LiabilityLine,
    Purchase,
    PurchaseLine,
    RefundCandidate,
    RefundLetterDraft,
    ReviewDecision,
    Workpaper,
    WorkpaperLine,
)
from app.seed import ENTITY_NAME

PATH_FOR_DIRECTION = {
    "LIABILITY": "LIABILITY",
    "REFUND_CANDIDATE": "REFUND_PURSUIT",
    "CHARGED_CORRECTLY": "CHARGED_CORRECTLY",
}
FORBIDDEN_FACT_FIELDS = frozenset(
    {
        "treatment",
        "label",
        "direction",
        "tax_decision_amount",
        "expected_tax",
        "rate",
        "approval",
        "verdict",
        "engine_amount",
    }
)
ALLOWED_FACT_FIELDS = frozenset(
    {
        "vendor_name",
        "invoice_date",
        "line_description",
        "line_amount",
        "buyer_entity",
        "tax_charged",
        "tax_charged_jurisdiction",
        "delivery_method",
        "access_model",
        "service_period",
        "seat_count",
        "user_location",
        "delivery_evidence",
        "shipping",
        "bill_to",
    }
)


def add_human_fact(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    purchase_id: str,
    field: str,
    value: str,
    source: str,
    reason: str,
    expected_version: int,
    line_id: str | None,
) -> Purchase:
    if field in FORBIDDEN_FACT_FIELDS or field not in ALLOWED_FACT_FIELDS:
        raise DomainError(
            "FORBIDDEN_FIELD", "That field is not a sourced invoice fact.", 422
        )
    purchase = _purchase(session, org_id, purchase_id)
    if purchase.facts_version != expected_version:
        raise DomainError(
            "STALE_VERSION", "The purchase changed. Reload it and try again.", 409
        )
    stored_value, location_name = _split_location(field, value)
    target_line = line_id
    if target_line is None and field in (
        "user_location",
        "delivery_evidence",
        "line_amount",
        "tax_charged",
    ):
        lines = list(
            session.scalars(
                select(PurchaseLine).where(PurchaseLine.purchase_id == purchase.id)
            ).all()
        )
        if len(lines) == 1:
            target_line = lines[0].id
            if location_name and not lines[0].location_name:
                lines[0].location_name = location_name
    current = list(
        session.scalars(
            select(Fact).where(
                Fact.purchase_id == purchase.id,
                Fact.field_key == field,
                Fact.line_id == target_line,
                Fact.is_current.is_(True),
            )
        ).all()
    )
    purchase.facts_version += 1
    for old in current:
        old.is_current = False
    document = session.scalar(
        select(Document).where(Document.purchase_id == purchase.id)
    )
    fact = Fact(
        id=str(uuid4()),
        org_id=org_id,
        purchase_id=purchase.id,
        line_id=target_line,
        field_key=field,
        value=stored_value,
        origin="HUMAN_SUPPLIED",
        source=source,
        evidence_rung=2 if field == "user_location" else None,
        confirmation="CONFIRMED",
        version=purchase.facts_version,
        is_current=True,
        supersedes=current[0].id if current else None,
    )
    session.add(fact)
    session.flush()
    session.add(
        Evidence(
            id=str(uuid4()),
            fact_id=fact.id,
            document_id=document.id if document is not None else None,
            page=1,
            passage=source,
            provenance="human",
        )
    )
    _supersede_all_current(session, purchase)
    _audit(
        session,
        org_id,
        actor_id,
        "add_fact",
        "purchase",
        purchase.id,
        purchase.facts_version,
    )
    enqueue_advance(session, org_id, purchase.id)
    del reason
    return purchase


def review_determination(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    determination_id: str,
    action: str,
    approval_path: str | None,
    reason: str,
    expected_version: int,
) -> Determination:
    determination = _determination(session, org_id, determination_id)
    if determination.version != expected_version or not determination.is_current:
        raise DomainError(
            "STALE_VERSION", "This result changed. Reload it and try again.", 409
        )
    if _has_active_hold(session, determination.purchase_id):
        raise DomainError("ITEM_HELD", "A held item cannot be approved.", 422)
    if determination.unit_status in ("APPROVED", "APPROVED_FOR_PURSUIT", "REJECTED"):
        raise DomainError("ALREADY_DECIDED", "This result was already decided.", 422)
    if (
        determination.label != "ENGINE_SUPPORTED"
        or determination.unit_status != "READY_FOR_REVIEW"
    ):
        raise DomainError(
            "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE",
            "Only an engine-supported result waiting for review can be decided.",
            422,
        )
    if action == "reject":
        determination.unit_status = "REJECTED"
        _record_decision(session, determination, "reject", None, actor_id, reason)
        return determination
    if action != "approve":
        raise DomainError(
            "UNSUPPORTED_ACTION", "Action must be approve or reject.", 422
        )
    expected = PATH_FOR_DIRECTION.get(determination.direction or "")
    if approval_path != expected:
        raise DomainError(
            "PATH_MISMATCH", "The approval path does not match the direction.", 422
        )
    purchase = _purchase(session, org_id, determination.purchase_id)
    if determination.direction == "LIABILITY":
        _approve_liability(
            session, determination, purchase, approval_path, actor_id, reason
        )
    elif determination.direction == "REFUND_CANDIDATE":
        _approve_refund(
            session, determination, purchase, approval_path, actor_id, reason
        )
    else:
        determination.unit_status = "APPROVED"
        _record_decision(
            session, determination, "approve", approval_path, actor_id, reason
        )
    return determination


def refer_determination(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    determination_id: str,
    action: str,
    note: str,
) -> Determination:
    determination = _determination(session, org_id, determination_id)
    if not determination.is_current or determination.label != "NEEDS_HUMAN_REVIEW":
        raise DomainError(
            "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE",
            "Refer is only for a current needs-human-review result.",
            422,
        )
    if action == "send_to_advisor":
        determination.unit_status = "SENT_TO_ADVISOR"
    elif action != "leave_open":
        raise DomainError(
            "UNSUPPORTED_ACTION", "Action must be leave_open or send_to_advisor.", 422
        )
    _audit(
        session,
        org_id,
        actor_id,
        f"refer:{action}",
        "determination",
        determination.id,
        determination.version,
    )
    del note
    return determination


def collect_totals(session: Session, org_id: str) -> dict[str, object]:
    purchases = list(
        session.scalars(select(Purchase).where(Purchase.org_id == org_id)).all()
    )
    purchase_ids = {purchase.id for purchase in purchases}
    held_ids = {
        hold.purchase_id
        for hold in session.scalars(select(Hold).where(Hold.active.is_(True))).all()
        if hold.purchase_id in purchase_ids
    }
    held_amount = parse_decimal("0")
    for purchase_id in held_ids:
        for line in session.scalars(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        ).all():
            held_amount += parse_decimal(line.line_amount)
    grouped: dict[tuple[str, str], Decimal] = {}
    refund_amount = parse_decimal("0")
    refund_count = 0
    nhr_count = 0
    error_count = 0
    unreviewed_count = 0
    for determination in session.scalars(
        select(Determination).where(Determination.org_id == org_id)
    ).all():
        if not determination.is_current or determination.purchase_id in held_ids:
            continue
        if determination.unit_status == "DETERMINATION_ERROR":
            error_count += 1
        elif determination.label == "NEEDS_HUMAN_REVIEW":
            nhr_count += 1
        elif determination.unit_status == "READY_FOR_REVIEW":
            unreviewed_count += 1
        elif (
            determination.unit_status == "APPROVED"
            and determination.direction == "LIABILITY"
        ):
            liability = session.scalar(
                select(LiabilityLine).where(
                    LiabilityLine.determination_id == determination.id
                )
            )
            if liability is None:
                continue
            key = (liability.jurisdiction, liability.period)
            previous = grouped.get(key, parse_decimal("0"))
            grouped[key] = previous + parse_decimal(liability.amount)
        elif determination.unit_status == "APPROVED_FOR_PURSUIT":
            refund = session.scalar(
                select(RefundCandidate).where(
                    RefundCandidate.determination_id == determination.id
                )
            )
            if refund is None:
                continue
            refund_amount += parse_decimal(refund.amount)
            refund_count += 1
    groups = [
        {
            "jurisdiction": jurisdiction,
            "period": period,
            "amount": {"value": f"{amount:.2f}", "currency": "USD"},
        }
        for (jurisdiction, period), amount in sorted(grouped.items())
    ]
    return {
        "approved_liabilities": groups,
        "approved_liabilities_message": None if groups else "No approved liabilities",
        "refund_candidates": {
            "count": refund_count,
            "label": "not recovered cash",
            "amount": None
            if refund_count == 0
            else {"value": f"{refund_amount:.2f}", "currency": "USD"},
        },
        "held": {
            "count": len(held_ids),
            "purchase_amount": {"value": f"{held_amount:.2f}", "currency": "USD"},
            "note": "purchase amount, not tax",
        },
        "needs_human_review_count": nhr_count,
        "error_count": error_count,
        "unreviewed_count": unreviewed_count,
    }


def create_workpaper(
    session: Session, org_id: str, jurisdiction: str, period: str
) -> Workpaper:
    fingerprint, liability_ids = _liability_fingerprint(
        session, org_id, jurisdiction, period
    )
    if not liability_ids:
        raise DomainError(
            "NO_APPROVED_LIABILITY_LINES",
            "No approved liabilities. Nothing was filed and no zero was written.",
            422,
        )
    latest = _latest_workpaper(session, org_id, jurisdiction, period)
    if (
        latest is not None
        and latest.status == "READY_TO_FILE_SIMULATED"
        and latest.fingerprint == fingerprint
    ):
        return latest
    if latest is not None and latest.status == "DRAFT":
        _replace_lines(session, latest, liability_ids)
        latest.fingerprint = fingerprint
        return latest
    version = 1 if latest is None else latest.version + 1
    workpaper = Workpaper(
        id=str(uuid4()),
        org_id=org_id,
        entity_name=ENTITY_NAME,
        jurisdiction=jurisdiction,
        period=period,
        version=version,
        status="DRAFT",
        fingerprint=fingerprint,
        frozen_at=None,
        created_at=utcnow(),
    )
    session.add(workpaper)
    session.flush()
    _replace_lines(session, workpaper, liability_ids)
    return workpaper


def mark_workpaper_ready(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    workpaper_id: str,
    expected_version: int,
    reason: str,
) -> Workpaper:
    workpaper = session.get(Workpaper, workpaper_id)
    if workpaper is None or workpaper.org_id != org_id:
        raise DomainError("NOT_FOUND", "Workpaper not found.", 404)
    if workpaper.version != expected_version:
        raise DomainError(
            "STALE_VERSION", "The workpaper changed. Reload it and try again.", 409
        )
    fingerprint, liability_ids = _liability_fingerprint(
        session, org_id, workpaper.jurisdiction, workpaper.period
    )
    if workpaper.status == "READY_TO_FILE_SIMULATED":
        if workpaper.fingerprint == fingerprint:
            return workpaper
        raise DomainError(
            "STALE_VERSION", "This version is frozen. Open a new draft.", 409
        )
    if not liability_ids:
        raise DomainError(
            "NO_APPROVED_LIABILITY_LINES",
            "No approved liabilities. Nothing was filed and no zero was written.",
            422,
        )
    _replace_lines(session, workpaper, liability_ids)
    workpaper.fingerprint = fingerprint
    workpaper.status = "READY_TO_FILE_SIMULATED"
    workpaper.frozen_at = utcnow()
    _audit(
        session,
        org_id,
        actor_id,
        "mark_ready",
        "workpaper",
        workpaper.id,
        workpaper.version,
    )
    del reason
    return workpaper


def export_workpaper(session: Session, org_id: str, workpaper_id: str) -> str:
    workpaper = session.get(Workpaper, workpaper_id)
    if workpaper is None or workpaper.org_id != org_id:
        raise DomainError("NOT_FOUND", "Workpaper not found.", 404)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["banner", "Synthetic demo data"])
    writer.writerow(["mark", "DRAFT / SIMULATED"])
    writer.writerow(["status", workpaper.status])
    writer.writerow(["jurisdiction", workpaper.jurisdiction])
    writer.writerow(["period", workpaper.period])
    writer.writerow([])
    writer.writerow(["section", "approved liabilities"])
    writer.writerow(["invoice", "amount"])
    for link in session.scalars(
        select(WorkpaperLine).where(WorkpaperLine.workpaper_id == workpaper.id)
    ).all():
        liability = session.get(LiabilityLine, link.liability_line_id)
        if liability is None:
            continue
        purchase = session.get(Purchase, liability.purchase_id)
        invoice = purchase.invoice_number if purchase is not None else ""
        writer.writerow([_escape(invoice), _escape(liability.amount)])
    writer.writerow([])
    writer.writerow(["section", "excluded"])
    writer.writerow(["invoice", "why"])
    for purchase in session.scalars(
        select(Purchase).where(Purchase.org_id == org_id)
    ).all():
        if quarter_key(date.fromisoformat(purchase.invoice_date)) != workpaper.period:
            continue
        why = _exclusion_reason(session, purchase)
        if why is None:
            continue
        writer.writerow([_escape(purchase.invoice_number), why])
    return buffer.getvalue()


def _approve_liability(
    session: Session,
    determination: Determination,
    purchase: Purchase,
    approval_path: str | None,
    actor_id: str,
    reason: str,
) -> None:
    if not determination.engine_amount:
        raise DomainError(
            "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE", "There is no engine amount.", 422
        )
    determination.unit_status = "APPROVED"
    _record_decision(session, determination, "approve", approval_path, actor_id, reason)
    period = quarter_key(date.fromisoformat(purchase.invoice_date))
    session.add(
        LiabilityLine(
            id=str(uuid4()),
            determination_id=determination.id,
            purchase_id=purchase.id,
            jurisdiction=determination.jurisdiction,
            period=period,
            amount=determination.engine_amount,
            approver_id=actor_id,
            approved_at=utcnow(),
        )
    )
    session.add(
        JournalEntryDraft(
            id=str(uuid4()),
            determination_id=determination.id,
            purchase_id=purchase.id,
            debit_account="Use Tax Expense",
            credit_account="Use Tax Payable",
            amount=determination.engine_amount,
            status="DRAFT",
        )
    )


def _approve_refund(
    session: Session,
    determination: Determination,
    purchase: Purchase,
    approval_path: str | None,
    actor_id: str,
    reason: str,
) -> None:
    if not determination.engine_amount:
        raise DomainError(
            "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE", "There is no engine amount.", 422
        )
    determination.unit_status = "APPROVED_FOR_PURSUIT"
    _record_decision(session, determination, "approve", approval_path, actor_id, reason)
    session.add(
        RefundCandidate(
            id=str(uuid4()),
            determination_id=determination.id,
            purchase_id=purchase.id,
            amount=determination.engine_amount,
            status="APPROVED_FOR_PURSUIT",
            approver_id=actor_id,
            approved_at=utcnow(),
        )
    )
    session.add(
        RefundLetterDraft(
            id=str(uuid4()),
            determination_id=determination.id,
            purchase_id=purchase.id,
            vendor=purchase.vendor,
            invoice_number=purchase.invoice_number,
            amount=determination.engine_amount,
            basis_text="",
            status="DRAFT",
        )
    )


def _exclusion_reason(session: Session, purchase: Purchase) -> str | None:
    hold = session.scalar(
        select(Hold).where(Hold.purchase_id == purchase.id, Hold.active.is_(True))
    )
    if hold is not None:
        return "HELD"
    current = session.scalars(
        select(Determination).where(
            Determination.purchase_id == purchase.id,
            Determination.is_current.is_(True),
        )
    ).all()
    if any(item.unit_status == "DETERMINATION_ERROR" for item in current):
        return "ERROR"
    if any(item.label == "NEEDS_HUMAN_REVIEW" for item in current):
        return "NEEDS_HUMAN_REVIEW"
    if any(item.unit_status == "READY_FOR_REVIEW" for item in current):
        return "UNREVIEWED"
    return None


def _liability_fingerprint(
    session: Session, org_id: str, jurisdiction: str, period: str
) -> tuple[str, list[str]]:
    kept: list[LiabilityLine] = []
    rows = session.scalars(
        select(LiabilityLine).where(
            LiabilityLine.jurisdiction == jurisdiction,
            LiabilityLine.period == period,
        )
    ).all()
    for row in rows:
        purchase = session.get(Purchase, row.purchase_id)
        determination = session.get(Determination, row.determination_id)
        if purchase is None or determination is None or purchase.org_id != org_id:
            continue
        if determination.is_current and determination.unit_status == "APPROVED":
            kept.append(row)
    kept.sort(key=lambda row: row.id)
    payload = "|".join(f"{row.id}:{row.amount}" for row in kept)
    return sha256(payload.encode("utf-8")).hexdigest(), [row.id for row in kept]


def _latest_workpaper(
    session: Session, org_id: str, jurisdiction: str, period: str
) -> Workpaper | None:
    rows = list(
        session.scalars(
            select(Workpaper).where(
                Workpaper.org_id == org_id,
                Workpaper.jurisdiction == jurisdiction,
                Workpaper.period == period,
            )
        ).all()
    )
    if not rows:
        return None
    return max(rows, key=lambda row: row.version)


def _replace_lines(
    session: Session, workpaper: Workpaper, liability_ids: list[str]
) -> None:
    for row in session.scalars(
        select(WorkpaperLine).where(WorkpaperLine.workpaper_id == workpaper.id)
    ).all():
        session.delete(row)
    session.flush()
    for liability_id in liability_ids:
        session.add(
            WorkpaperLine(
                id=str(uuid4()),
                workpaper_id=workpaper.id,
                liability_line_id=liability_id,
            )
        )


def _escape(value: str) -> str:
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _split_location(field: str, value: str) -> tuple[str, str | None]:
    if field == "user_location" and "," in value:
        return value.rsplit(",", 1)[1].strip(), value.strip()
    return value.strip(), None


def _purchase(session: Session, org_id: str, purchase_id: str) -> Purchase:
    purchase = session.get(Purchase, purchase_id)
    if purchase is None or purchase.org_id != org_id:
        raise DomainError("NOT_FOUND", "Purchase not found.", 404)
    return purchase


def _determination(
    session: Session, org_id: str, determination_id: str
) -> Determination:
    determination = session.get(Determination, determination_id)
    if determination is None or determination.org_id != org_id:
        raise DomainError("NOT_FOUND", "Determination not found.", 404)
    return determination


def _has_active_hold(session: Session, purchase_id: str) -> bool:
    found = session.scalar(
        select(Hold.id).where(Hold.purchase_id == purchase_id, Hold.active.is_(True))
    )
    return found is not None


def _record_decision(
    session: Session,
    determination: Determination,
    action: str,
    approval_path: str | None,
    actor_id: str,
    reason: str,
) -> None:
    session.add(
        ReviewDecision(
            id=str(uuid4()),
            determination_id=determination.id,
            version=determination.version,
            action=action,
            approval_path=approval_path,
            actor_id=actor_id,
            reason=reason,
            created_at=utcnow(),
        )
    )
    _audit(
        session,
        determination.org_id,
        actor_id,
        action,
        "determination",
        determination.id,
        determination.version,
    )


def _audit(
    session: Session,
    org_id: str,
    actor_id: str,
    command: str,
    object_type: str,
    object_id: str,
    version: int,
) -> None:
    # Flush pending command events too, so multiple writes in one transaction chain.
    session.flush()
    previous = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.org_id == org_id,
            AuditEvent.object_type == object_type,
            AuditEvent.object_id == object_id,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    identity = json.dumps(
        {
            "object_type": object_type,
            "object_id": object_id,
            "object_version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    created_at = datetime.now(UTC)
    if previous is not None:
        previous_time = datetime.fromisoformat(
            previous.created_at.replace("Z", "+00:00")
        )
        # Legacy timestamps have second precision: their trailing Z sorts after
        # fractional timestamps in the same second, so move beyond that second.
        step = (
            timedelta(microseconds=1)
            if "." in previous.created_at
            else timedelta(seconds=1)
        )
        created_at = max(created_at, previous_time + step)
    session.add(
        AuditEvent(
            id=str(uuid4()),
            org_id=org_id,
            actor_id=actor_id,
            command=command,
            object_type=object_type,
            object_id=object_id,
            object_version=version,
            before_hash=(previous.after_hash or "") if previous is not None else "",
            after_hash=sha256(identity.encode("utf-8")).hexdigest(),
            created_at=created_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
        )
    )


def _supersede_all_current(session: Session, purchase: Purchase) -> None:
    for row in session.scalars(
        select(Determination).where(
            Determination.purchase_id == purchase.id,
            Determination.is_current.is_(True),
        )
    ).all():
        row.is_current = False
        row.unit_status = "SUPERSEDED"
