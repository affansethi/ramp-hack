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
    AuditEvent,
    Determination,
    Fact,
    Hold,
    Purchase,
    PurchaseLine,
    Question,
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
    facts = list(
        session.scalars(
            select(Fact).where(
                Fact.purchase_id == purchase.id, Fact.is_current.is_(True)
            )
        ).all()
    )
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
                "state": question.state,
                "asked_at": question.asked_at,
            }
            for question in questions
        ],
        "determinations": [
            _determination_json(row) for row in determinations if row.is_current
        ],
        "actions": _actions(queue),
        "possible_duplicate_ids": _possible_duplicate_ids(session, purchase),
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


def _stored_object(raw: str | None) -> dict[str, Any] | None:
    """Parse a stored engine-trail JSON object. Null when the row has none."""
    if not raw:
        return None
    loaded: Any = json.loads(raw)
    if not isinstance(loaded, dict):
        raise TypeError("engine trail must be a JSON object")
    return {str(key): value for key, value in loaded.items()}


def _determination_json(row: Determination) -> dict[str, object]:
    supported = row.label == "ENGINE_SUPPORTED"
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
        engine_response=_stored_object(row.engine_response),
    )
    return cast(dict[str, object], body.model_dump(mode="json"))


def _actions(queue: str) -> list[str]:
    if queue == "ready":
        return ["approve", "reject"]
    if queue == "needs_human_review":
        return ["refer"]
    return []


def _possible_duplicate_ids(session: Session, purchase: Purchase) -> list[str]:
    prefix = "ambiguous_invoice_match:"
    rows = session.scalars(
        select(AuditEvent).where(
            AuditEvent.org_id == purchase.org_id,
            AuditEvent.object_id == purchase.id,
            AuditEvent.command.like(prefix + "%"),
        )
    ).all()
    found: list[str] = []
    for row in rows:
        other = row.command[len(prefix) :]
        if other and other not in found:
            found.append(other)
    return found


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
