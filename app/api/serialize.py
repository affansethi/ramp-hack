"""Read models. Amounts are copied engine strings, or null when there is no result."""

import json
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.contracts.determination import DeterminationRead
from app.contracts.money import Money
from app.domain.moneyutil import money_json, parse_decimal
from app.models import (
    Determination,
    Document,
    Evidence,
    Fact,
    Hold,
    LiabilityLine,
    Person,
    Purchase,
    PurchaseLine,
    Question,
    RefundCandidate,
    RefundLetterDraft,
    ReviewDecision,
    Workpaper,
    WorkpaperLine,
)

_PROMPTS = {
    "user_location": "Send a seat export or a named answer for seat location.",
    "required_fact_catalog": "Catalog is still a draft, so facts are not admitted.",
    "category": "The recording did not include a supported category.",
}


def meta(settings: Settings) -> dict[str, object]:
    return {
        "data_mode": settings.data_mode.value,
        "engine_mode": settings.engine_mode.value,
        "llm_mode": settings.llm_mode.value,
        "replay": settings.llm_mode.value == "replay",
    }


def queue_for(holds: list[Hold], determinations: list[Determination]) -> str:
    if any(hold.active for hold in holds):
        return "held"
    current = [row for row in determinations if row.is_current]
    if any(row.unit_status == "DETERMINATION_ERROR" for row in current):
        return "error"
    if any(row.label == "NEEDS_HUMAN_REVIEW" for row in current):
        return "needs_human_review"
    if any(row.unit_status == "READY_FOR_REVIEW" for row in current):
        return "ready"
    if current:
        return "decided"
    return "processing"


def purchase_summary(session: Session, purchase: Purchase) -> dict[str, object]:
    holds = _holds(session, purchase.id)
    determinations = _determinations(session, purchase.id)
    queue = queue_for(holds, determinations)
    return {
        "id": purchase.id,
        "vendor": purchase.vendor,
        "invoice_number": purchase.invoice_number,
        "invoice_date": purchase.invoice_date,
        "item_status": purchase.item_status,
        "queue": queue,
        "scenario": purchase.demo_scenario,
        "extraction_mode": purchase.extraction_mode,
        "facts_version": purchase.facts_version,
        "stop_reason": stop_reason(holds, determinations),
        "pending_results": _pending_results(holds, determinations),
    }


def purchase_detail(
    session: Session, purchase: Purchase, settings: Settings
) -> dict[str, object]:
    holds = _holds(session, purchase.id)
    determinations = _determinations(session, purchase.id)
    lines = list(
        session.scalars(
            select(PurchaseLine)
            .where(PurchaseLine.purchase_id == purchase.id)
            .order_by(PurchaseLine.line_number)
        ).all()
    )
    all_facts = list(
        session.scalars(
            select(Fact)
            .where(Fact.purchase_id == purchase.id)
            .order_by(Fact.version, Fact.id)
        ).all()
    )
    facts = [fact for fact in all_facts if fact.is_current]
    documents = list(
        session.scalars(
            select(Document)
            .where(Document.purchase_id == purchase.id)
            .order_by(Document.id)
        ).all()
    )
    evidence_by_fact = _evidence_by_fact(session, [fact.id for fact in all_facts])
    current = [row for row in determinations if row.is_current]
    past = sorted(
        (row for row in determinations if not row.is_current),
        key=lambda row: (row.facts_version, row.version, row.id),
    )
    approvals = _approvals_by_determination(session, [row.id for row in determinations])
    decisions = _decisions_json(session, [row.id for row in determinations])
    questions = list(
        session.scalars(
            select(Question).where(Question.purchase_id == purchase.id)
        ).all()
    )
    queue = queue_for(holds, determinations)
    body: dict[str, object] = {
        **meta(settings),
        **purchase_summary(session, purchase),
        "entity_name": purchase.entity_name,
        "currency": purchase.currency,
        "category": purchase.category,
        "bill_to": purchase.bill_to,
        "created_at": purchase.created_at,
        "lines": [
            {
                "id": line.id,
                "line_number": line.line_number,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": money_json(line.unit_price),
                "line_amount": money_json(line.line_amount),
                "service_period_start": line.service_period_start,
                "service_period_end": line.service_period_end,
                "delivery_date": line.delivery_date,
                "location_name": line.location_name,
            }
            for line in lines
        ],
        "documents": [
            {
                "id": document.id,
                "mime_type": document.mime_type,
                "sha256": document.sha256,
                "pages": document.pages,
            }
            for document in documents
        ],
        "facts": [
            {
                "id": fact.id,
                "line_id": fact.line_id,
                "field": fact.field_key,
                "value": fact.value,
                "origin": fact.origin,
                "confirmation": fact.confirmation,
                "evidence_rung": fact.evidence_rung,
                "source": fact.source,
                "version": fact.version,
                "model_output": purchase.extraction_mode,
                "evidence": [
                    {
                        "id": item.id,
                        "document_id": item.document_id,
                        "page": item.page,
                        "passage": item.passage,
                        "provenance": item.provenance,
                    }
                    for item in evidence_by_fact.get(fact.id, [])
                ],
            }
            for fact in facts
        ],
        "holds": [
            {
                "id": hold.id,
                "reason": hold.reason,
                "named_fact": hold.named_fact,
                "active": hold.active,
            }
            for hold in holds
            if hold.active
        ],
        "questions": [
            {
                "id": question.id,
                "fact_key": question.fact_key,
                "prompt": _PROMPTS.get(
                    question.fact_key, f"Send a sourced value for {question.fact_key}."
                ),
                # Stored recipient for the held queue (#32; read-only, not a tax field).
                "recipient": question.recipient,
                "answer": question.answer,
                "state": question.state,
                "asked_at": question.asked_at,
            }
            for question in questions
        ],
        "determinations": [
            _approved_determination_json(row, session, approvals) for row in current
        ],
        "past_determinations": [
            _approved_determination_json(row, session, approvals) for row in past
        ],
        "fact_history": [
            {
                "id": fact.id,
                "line_id": fact.line_id,
                "field": fact.field_key,
                "value": fact.value,
                "origin": fact.origin,
                "confirmation": fact.confirmation,
                "evidence_rung": fact.evidence_rung,
                "source": fact.source,
                "version": fact.version,
                "is_current": fact.is_current,
                "supersedes": fact.supersedes,
                "model_output": purchase.extraction_mode,
                "evidence": [
                    {
                        "id": item.id,
                        "document_id": item.document_id,
                        "page": item.page,
                        "passage": item.passage,
                        "provenance": item.provenance,
                    }
                    for item in evidence_by_fact.get(fact.id, [])
                ],
            }
            for fact in all_facts
        ],
        "decisions": decisions,
        "approver_name": _current_approver(approvals, current),
        "approved_at": _current_approved_at(approvals, current),
        "actions": _actions(queue),
    }
    if queue == "held":
        total = sum(
            (parse_decimal(line.line_amount) for line in lines),
            start=parse_decimal("0"),
        )
        body["purchase_amount"] = money_json(f"{total:.2f}")
        body["purchase_amount_note"] = "purchase amount, not tax"
    return body


def stop_reason(holds: list[Hold], determinations: list[Determination]) -> str:
    active = next((hold for hold in holds if hold.active), None)
    if active is not None:
        if active.named_fact == "required_fact_catalog":
            return (
                "Facts are not admitted. The required-fact catalog is still a draft, "
                "so nothing is sent to the engine and there is no tax amount."
            )
        return f"Held for {active.named_fact}. No engine call and no tax amount."
    current = [row for row in determinations if row.is_current]
    nhr = next((row for row in current if row.label == "NEEDS_HUMAN_REVIEW"), None)
    if nhr is not None:
        return f"Needs human review ({nhr.nhr_reason}). No tax amount."
    error = next(
        (row for row in current if row.unit_status == "DETERMINATION_ERROR"), None
    )
    if error is not None:
        return f"Determination error ({error.error_code}). No tax amount."
    if any(row.unit_status == "READY_FOR_REVIEW" for row in current):
        return "Engine-supported result waiting for a named reviewer."
    if current:
        return "Decided. Totals include only approved current results."
    return "Waiting for the worker."


def _supported_text(supported: bool, value: str | None) -> str | None:
    if not supported:
        return None
    return value


def _supported_money(supported: bool, value: str | None) -> Money | None:
    if not supported or not value:
        return None
    return Money.model_validate({"value": value, "currency": "USD"})


def _pending_results(
    holds: list[Hold], determinations: list[Determination]
) -> list[dict[str, object]]:
    """Unreviewed engine-supported amounts shown on the list, never in a total."""
    if any(hold.active for hold in holds):
        return []
    pending: list[dict[str, object]] = []
    for row in sorted(determinations, key=lambda item: item.id):
        if not row.is_current:
            continue
        if row.unit_status != "READY_FOR_REVIEW":
            continue
        if row.label != "ENGINE_SUPPORTED":
            continue
        supported = True
        money = _supported_money(supported, row.engine_amount)
        pending.append(
            {
                "determination_id": row.id,
                "jurisdiction": row.jurisdiction,
                "direction": _supported_text(supported, row.direction),
                "engine_amount": money.model_dump(mode="json")
                if money is not None
                else None,
                "note": "unreviewed; not in any total",
            }
        )
    return pending


def _evidence_by_fact(
    session: Session, fact_ids: list[str]
) -> dict[str, list[Evidence]]:
    grouped: dict[str, list[Evidence]] = {}
    if not fact_ids:
        return grouped
    rows = session.scalars(
        select(Evidence).where(Evidence.fact_id.in_(fact_ids)).order_by(Evidence.id)
    ).all()
    for row in rows:
        grouped.setdefault(row.fact_id, []).append(row)
    return grouped


def _rule_source_url(
    supported: bool, engine_response: dict[str, Any] | None
) -> str | None:
    """Copy the link stored with the result. Never read the current ruleset."""
    if not supported or engine_response is None:
        return None
    value = engine_response.get("rule_source_url")
    if isinstance(value, str):
        return value
    return None


def _stored_object(raw: str | None) -> dict[str, Any] | None:
    """Parse a stored engine-trail JSON object. Null when the row has none."""
    if not raw:
        return None
    loaded: Any = json.loads(raw)
    if not isinstance(loaded, dict):
        raise TypeError("engine trail must be a JSON object")
    return {str(key): value for key, value in loaded.items()}


def _determination_json(
    row: Determination, session: Session | None = None
) -> dict[str, object]:
    # A rejected result keeps its frozen engine snapshot but shows no amount,
    # rate, code, reference, treatment or direction outside that snapshot.
    supported = row.label == "ENGINE_SUPPORTED" and row.unit_status != "REJECTED"
    letter = None
    if session is not None:
        letter = session.scalar(
            select(RefundLetterDraft).where(
                RefundLetterDraft.determination_id == row.id
            )
        )
    engine_response = _stored_object(row.engine_response)
    body = DeterminationRead(
        id=row.id,
        line_id=row.line_id,
        jurisdiction=row.jurisdiction,
        facts_version=row.facts_version,
        ruleset_version=row.ruleset_version,
        label=row.label,
        nhr_reason=row.nhr_reason,
        direction=_supported_text(supported, row.direction),
        derived_direction=row.derived_direction if supported else False,
        rate=_supported_text(supported, row.rate),
        location_code=_supported_text(supported, row.location_code),
        expected_tax=_supported_money(supported, row.expected_tax),
        engine_amount=_supported_money(supported, row.engine_amount),
        rule_reference=_supported_text(supported, row.rule_reference),
        engine_treatment=_supported_text(supported, row.engine_treatment),
        unit_status=row.unit_status,
        version=row.version,
        error_code=row.error_code,
        engine_request=_stored_object(row.engine_request),
        engine_response=engine_response,
        refund_letter_id=letter.id if letter is not None else None,
    )
    payload = cast(dict[str, object], body.model_dump(mode="json"))
    payload["rule_source_url"] = _rule_source_url(supported, engine_response)
    return payload


def _approved_determination_json(
    row: Determination,
    session: Session | None,
    approvals: dict[str, dict[str, str | None]],
) -> dict[str, object]:
    """Current or past determination plus its stored approver, if any."""
    payload = _determination_json(row, session)
    approval = approvals.get(row.id, {})
    payload["approver_name"] = approval.get("approver_name")
    payload["approved_at"] = approval.get("approved_at")
    return payload


def _approvals_by_determination(
    session: Session, determination_ids: list[str]
) -> dict[str, dict[str, str | None]]:
    """Stored approver per determination from liability and refund lines."""
    approvals: dict[str, dict[str, str | None]] = {}
    if not determination_ids:
        return approvals
    liabilities = list(
        session.scalars(
            select(LiabilityLine).where(
                LiabilityLine.determination_id.in_(determination_ids)
            )
        ).all()
    )
    refunds = list(
        session.scalars(
            select(RefundCandidate).where(
                RefundCandidate.determination_id.in_(determination_ids)
            )
        ).all()
    )
    actor_ids = {row.approver_id for row in liabilities} | {
        row.approver_id for row in refunds
    }
    names = _person_names(session, sorted(actor_ids))
    for row in liabilities:
        approvals[row.determination_id] = {
            "approver_name": names.get(row.approver_id),
            "approved_at": row.approved_at,
        }
    for refund in refunds:
        approvals[refund.determination_id] = {
            "approver_name": names.get(refund.approver_id),
            "approved_at": refund.approved_at,
        }
    return approvals


def _current_approver(
    approvals: dict[str, dict[str, str | None]], current: list[Determination]
) -> str | None:
    for row in current:
        if row.unit_status in ("APPROVED", "APPROVED_FOR_PURSUIT"):
            name = approvals.get(row.id, {}).get("approver_name")
            if isinstance(name, str):
                return name
    return None


def _current_approved_at(
    approvals: dict[str, dict[str, str | None]], current: list[Determination]
) -> str | None:
    for row in current:
        if row.unit_status in ("APPROVED", "APPROVED_FOR_PURSUIT"):
            stamped = approvals.get(row.id, {}).get("approved_at")
            if isinstance(stamped, str):
                return stamped
    return None


def _decisions_json(
    session: Session, determination_ids: list[str]
) -> list[dict[str, object]]:
    """Stored review decisions with the deciding person's name."""
    if not determination_ids:
        return []
    rows = list(
        session.scalars(
            select(ReviewDecision)
            .where(ReviewDecision.determination_id.in_(determination_ids))
            .order_by(ReviewDecision.created_at, ReviewDecision.id)
        ).all()
    )
    names = _person_names(session, sorted({row.actor_id for row in rows}))
    return [
        {
            "id": row.id,
            "determination_id": row.determination_id,
            "determination_version": row.version,
            "version": row.version,
            "action": row.action,
            "approval_path": row.approval_path,
            "reason": row.reason,
            "created_at": row.created_at,
            "actor_id": row.actor_id,
            "actor_name": names.get(row.actor_id),
        }
        for row in rows
    ]


def _person_names(session: Session, person_ids: list[str]) -> dict[str, str | None]:
    if not person_ids:
        return {}
    rows = list(session.scalars(select(Person).where(Person.id.in_(person_ids))).all())
    return {row.id: row.name for row in rows}


def workpaper_detail(session: Session, workpaper: Workpaper) -> dict[str, object]:
    """Workpaper read: frozen document plus line links and version history."""
    from app.domain.commands import workpaper_document

    document = workpaper_document(session, workpaper)
    lines = list(
        session.scalars(
            select(WorkpaperLine)
            .where(WorkpaperLine.workpaper_id == workpaper.id)
            .order_by(WorkpaperLine.id)
        ).all()
    )
    liability_ids = [line.liability_line_id for line in lines]
    liabilities: dict[str, LiabilityLine] = {}
    if liability_ids:
        for row in session.scalars(
            select(LiabilityLine).where(LiabilityLine.id.in_(liability_ids))
        ).all():
            liabilities[row.id] = row
    names = _person_names(
        session, sorted({row.approver_id for row in liabilities.values()})
    )
    linked = [
        {
            "liability_line_id": line.liability_line_id,
            "determination_id": linked_row.determination_id,
            "purchase_id": linked_row.purchase_id,
            "jurisdiction": linked_row.jurisdiction,
            "period": linked_row.period,
            "amount": {"value": linked_row.amount, "currency": "USD"},
            "approver_name": names.get(linked_row.approver_id),
            "approved_at": linked_row.approved_at,
        }
        for line in lines
        if (linked_row := liabilities.get(line.liability_line_id)) is not None
    ]
    versions = list(
        session.scalars(
            select(Workpaper)
            .where(
                Workpaper.org_id == workpaper.org_id,
                Workpaper.jurisdiction == workpaper.jurisdiction,
                Workpaper.period == workpaper.period,
            )
            .order_by(Workpaper.version, Workpaper.id)
        ).all()
    )
    return {
        **document,
        "lines": linked,
        "versions": [
            {
                "id": row.id,
                "version": row.version,
                "status": row.status,
                "fingerprint": row.fingerprint,
                "frozen_at": row.frozen_at,
            }
            for row in versions
        ],
    }


def _actions(queue: str) -> list[str]:
    if queue == "ready":
        return ["approve", "reject"]
    if queue == "needs_human_review":
        return ["refer"]
    return []


def _holds(session: Session, purchase_id: str) -> list[Hold]:
    return list(
        session.scalars(select(Hold).where(Hold.purchase_id == purchase_id)).all()
    )


def _determinations(session: Session, purchase_id: str) -> list[Determination]:
    return list(
        session.scalars(
            select(Determination).where(Determination.purchase_id == purchase_id)
        ).all()
    )
