"""Ingest a synthetic purchase and advance it until it stops or waits for review."""

import json
from dataclasses import asdict, dataclass, replace
from datetime import date
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.extractor import ForbiddenField, parse_extraction
from app.clock import utcnow
from app.demo.scenarios import LineInput, Scenario, get_scenario
from app.domain.errors import DomainError
from app.domain.evaluate import EngineLineRequest, evaluate_line
from app.domain.holds import (
    FactView,
    HoldDecision,
    confirmed_states,
    decide_hold,
    location_confirmation,
    undeclared,
)
from app.domain.moneyutil import differs_by_more_than_one_cent, parse_decimal
from app.domain.ruleset import Ruleset
from app.models import (
    AuditEvent,
    Determination,
    Document,
    Evidence,
    Fact,
    Hold,
    Job,
    Purchase,
    PurchaseLine,
    Question,
    SourceEvent,
)
from app.seed import ENTITY_NAME

SUPPORTED_CATEGORIES = frozenset({"REMOTE_ACCESS_SAAS", "COMPUTER_HARDWARE"})
# Mirrors the extractor's category proposals. Any other label stays out of the
# payload so it cannot fail the schema; the purchase still stores it.
_EXTRACTION_CATEGORIES = frozenset(
    {"REMOTE_ACCESS_SAAS", "COMPUTER_HARDWARE", "NO_SUPPORTED_CATEGORY"}
)


@dataclass(frozen=True)
class IngestResult:
    purchase_id: str | None
    job_id: str | None
    duplicate: bool
    source_event_id: str


def ingest_scenario(
    session: Session, scenario: Scenario, org_id: str, *, job: Job | None = None
) -> IngestResult:
    if scenario.currency != "USD":
        raise DomainError("UNSUPPORTED_CURRENCY", "Only USD is in the demo.", 422)
    # In-memory schema gate. Replay does not open a recording. A forbidden key
    # stops here, before the existing fact writer.
    try:
        parse_extraction(_printed_extraction(scenario))
    except ForbiddenField:
        return _forbidden_field_job(session, org_id, job=job)
    return _ingest(
        session,
        org_id=org_id,
        event_id=scenario.event_id,
        signature_result="DEMO",
        payload=scenario.id,
        vendor=scenario.vendor,
        invoice_number=scenario.invoice_number,
        invoice_date=scenario.invoice_date,
        entity_amount=scenario.entity_amount,
        ocr_taxes=scenario.ocr_taxes,
        category=scenario.category,
        bill_to=scenario.bill_to,
        lines=scenario.lines,
        demo_scenario=scenario.id,
        extraction_mode="replay",
        transaction_id=scenario.event_id,
    )


def _printed_extraction(scenario: Scenario) -> dict[str, object]:
    """Call A/B payload from printed fields. Place and seats stay evidence."""
    evidence: list[dict[str, object]] = [
        {"page": 1, "passage": f"Synthetic invoice {scenario.invoice_number}"}
    ]
    lines: list[dict[str, object]] = []
    ship_to: str | None = None
    for line in scenario.lines:
        lines.append(_printed_line(line))
        passage = _printed_evidence(line)
        if passage:
            evidence.append({"page": 1, "passage": passage})
        if ship_to is None and line.location_name:
            ship_to = line.location_name
    call_b: dict[str, object] = {"rationale": "Printed invoice fields."}
    if scenario.category in _EXTRACTION_CATEGORIES:
        call_b["category"] = scenario.category
    return {
        "call_a": {
            "vendor_name": scenario.vendor,
            "invoice_date": scenario.invoice_date,
            "currency": scenario.currency,
            "bill_to": scenario.bill_to or None,
            "ship_to": ship_to,
            "lines": lines,
            "evidence": evidence,
        },
        "call_b": call_b,
    }


def _printed_line(line: LineInput) -> dict[str, object]:
    printed: dict[str, object] = {
        "description": line.description,
        "quantity": line.quantity,
        "unit_price": line.line_amount,
        "line_amount": line.line_amount,
    }
    if line.tax_charged is not None:
        printed["tax_charged"] = line.tax_charged
    if line.tax_jurisdiction:
        printed["printed_tax_jurisdiction"] = line.tax_jurisdiction
    if line.service_start:
        printed["service_period_start"] = line.service_start
    if line.service_end:
        printed["service_period_end"] = line.service_end
    if line.shipping is not None:
        printed["shipping"] = line.shipping
    return printed


def _printed_evidence(line: LineInput) -> str:
    parts: list[str] = []
    if line.location_name or line.location_state:
        where = line.location_name or line.location_state or ""
        parts.append(f"Printed location {where}")
    if line.seats is not None:
        parts.append(f"Printed seat count {line.seats}")
    if line.delivery_date:
        parts.append(f"Printed delivery date {line.delivery_date}")
    if line.tax_jurisdiction:
        parts.append(f"Tax jurisdiction printed as {line.tax_jurisdiction}")
    return ". ".join(parts)


def _forbidden_field_job(
    session: Session, org_id: str, *, job: Job | None = None
) -> IngestResult:
    """Record the refusal without a purchase, fact, determination, or amount."""
    if job is not None:
        job.status = "FAILED"
        job.error_code = "FORBIDDEN_FIELD"
        return IngestResult(None, job.id, False, job.id)
    job = Job(
        id=str(uuid4()),
        org_id=org_id,
        kind="advance",
        purchase_id=None,
        status="FAILED",
        lease_until=None,
        attempts=0,
        error_code="FORBIDDEN_FIELD",
        created_at=utcnow(),
    )
    session.add(job)
    session.flush()
    return IngestResult(None, job.id, False, job.id)


def ingest_webhook(
    session: Session,
    org_id: str,
    *,
    event_id: str,
    event_type: str,
    object_id: str,
    receipt_id: str | None = None,
    signature_ok: bool,
    demo_scenario: str | None = None,
    raw_payload: str | None = None,
) -> IngestResult:
    if not signature_ok:
        raise DomainError("BAD_SIGNATURE", "X-Ramp-Signature did not match.", 401)
    existing = _find_event(session, org_id, event_id)
    if existing is not None:
        job = session.get(Job, existing.id)
        return IngestResult(
            existing.purchase_id, job.id if job else None, True, existing.id
        )
    payload = json.dumps(
        dict(
            event_type=event_type,
            object_id=object_id,
            receipt_id=receipt_id,
            demo_scenario=demo_scenario,
        )
    )
    event = SourceEvent(
        id=str(uuid4()),
        org_id=org_id,
        source="ramp",
        external_event_id=event_id,
        payload_hash=_hash(raw_payload if raw_payload is not None else payload),
        signature_result="OK",
        purchase_id=None,
        created_at=utcnow(),
    )
    session.add(event)
    # A fetch job shares its SourceEvent id; its input survives worker restarts.
    session.add(
        Job(
            id=event.id,
            org_id=org_id,
            kind="fetch",
            input_payload=payload,
            purchase_id=None,
            status="QUEUED",
            attempts=0,
            created_at=utcnow(),
        )
    )
    session.flush()
    _stage_audit(session, org_id, "ingest", event.id, 1, object_type="source_event")
    return IngestResult(None, event.id, False, event.id)


def ingest_fetched_record(
    session: Session, job: Job, record: dict[str, str | None]
) -> None:
    event = session.get(SourceEvent, job.id)
    if event is None:
        raise ValueError("missing source event")
    _stage_audit(session, job.org_id, "fetch", event.id, 1, object_type="source_event")
    if record["currency_code"] != "USD":
        return
    amount = record["entity_amount"]
    tax = record["ocr_taxes"]
    if not isinstance(amount, str) or (tax is not None and not isinstance(tax, str)):
        raise ValueError("Ramp amounts must be decimal strings")
    parse_decimal(amount)
    if tax is not None:
        parse_decimal(tax)
    inputs = json.loads(job.input_payload or "{}")
    transaction_id = inputs["object_id"]
    if inputs["event_type"] == "bills.created":
        transaction_id = "bill:" + transaction_id
    existing = _purchase_for_ramp_object(session, job, transaction_id)
    if existing is not None:
        # Amount and OCR feed the cross-check. A new value versions the purchase,
        # supersedes the current result, and queues that check again.
        _refresh_fetched_inputs(session, existing, amount, tax)
        _stage_audit(
            session,
            job.org_id,
            "deduplicate",
            existing.id,
            existing.facts_version,
        )
        event.purchase_id = job.purchase_id = existing.id
        return
    vendor = record["vendor"] or ""
    invoice_number = record["invoice_number"] or ""
    invoice_date = record["invoice_date"] or ""
    matched, ambiguous = _invoice_candidates(
        session, job.org_id, vendor, invoice_number, invoice_date, amount
    )
    if matched is not None:
        _link_exact_invoice(session, matched, event, tax, job=job)
        return
    scenario = (
        get_scenario(inputs["demo_scenario"]) if inputs.get("demo_scenario") else None
    )
    if scenario is not None:
        # Existing owner-provided demo extraction remains a labeled replay.
        result = ingest_scenario(
            session,
            replace(scenario, entity_amount=amount, ocr_taxes=tax),
            job.org_id,
            job=job,
        )
    else:
        line = LineInput(
            "Simulated Ramp record",
            amount,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        result = _ingest(
            session,
            org_id=job.org_id,
            event_id=event.external_event_id,
            signature_result="OK",
            payload=job.input_payload or "",
            vendor=record["vendor"] or "",
            invoice_number=record["invoice_number"] or "",
            invoice_date=record["invoice_date"] or "",
            entity_amount=amount,
            ocr_taxes=tax,
            category=None,
            bill_to="",
            lines=(line,),
            demo_scenario=None,
            extraction_mode="replay_miss",
            transaction_id=transaction_id,
        )
    event.purchase_id = job.purchase_id = result.purchase_id
    if result.purchase_id is not None:
        for other in ambiguous:
            _record_ambiguous(session, job.org_id, result.purchase_id, other.id)


def _purchase_for_ramp_object(
    session: Session, job: Job, transaction_id: str
) -> Purchase | None:
    """The canonical purchase for a Ramp card or bill, including a linked one.

    A merged purchase keeps one ramp_transaction_id, but every fetch job that
    resolved to it records its purchase_id, so a later event for the other
    linked object resolves here instead of creating a second purchase.
    """
    direct = session.scalar(
        select(Purchase).where(
            Purchase.org_id == job.org_id,
            Purchase.ramp_transaction_id == transaction_id,
        )
    )
    if direct is not None:
        return direct
    prior = session.scalars(
        select(Job)
        .where(
            Job.org_id == job.org_id,
            Job.kind == "fetch",
            Job.id != job.id,
            Job.purchase_id.is_not(None),
        )
        .order_by(Job.created_at)
    ).all()
    for row in prior:
        inputs = json.loads(row.input_payload or "{}")
        object_id = inputs.get("object_id")
        if not object_id:
            continue
        if inputs.get("event_type") == "bills.created":
            object_id = "bill:" + object_id
        if object_id == transaction_id and row.purchase_id is not None:
            return session.get(Purchase, row.purchase_id)
    return None


def _refresh_fetched_inputs(
    session: Session, purchase: Purchase, amount: str, tax: str | None
) -> None:
    changed = False
    if purchase.ramp_entity_amount != amount:
        purchase.ramp_entity_amount = amount
        changed = True
    if purchase.ramp_ocr_taxes != tax:
        purchase.ramp_ocr_taxes = tax
        changed = True
    if not changed:
        return
    purchase.facts_version += 1
    _supersede_current(session, purchase)
    _enqueue(session, purchase.org_id, purchase.id)


def advance_purchase(session: Session, purchase_id: str, ruleset: Ruleset) -> None:
    purchase = session.get(Purchase, purchase_id)
    if purchase is None:
        return
    already = session.scalars(
        select(Determination).where(
            Determination.purchase_id == purchase.id,
            Determination.facts_version == purchase.facts_version,
            Determination.ruleset_version == ruleset.version,
            Determination.is_current.is_(True),
        )
    ).all()
    if already:
        return
    _supersede_current(session, purchase)
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
    _apply_cross_check(purchase, lines, facts)
    _stage_audit(
        session, purchase.org_id, "cross_check", purchase.id, purchase.facts_version
    )
    if not purchase.category or purchase.category not in SUPPORTED_CATEGORIES:
        if (
            purchase.category not in SUPPORTED_CATEGORIES
            and purchase.category is not None
        ):
            _clear_holds(session, purchase, release_version=purchase.facts_version)
            _stage_audit(
                session,
                purchase.org_id,
                "classify",
                purchase.id,
                purchase.facts_version,
            )
            _item_nhr(session, purchase, ruleset, "NO_SUPPORTED_CATEGORY", lines)
            return
        _stage_audit(
            session, purchase.org_id, "classify", purchase.id, purchase.facts_version
        )
        _set_hold(session, purchase, HoldDecision("MISSING_REQUIRED_FACT", "category"))
        purchase.item_status = "HELD"
        return
    _stage_audit(
        session, purchase.org_id, "classify", purchase.id, purchase.facts_version
    )
    hold = _first_hold(purchase.category, lines, facts, ruleset)
    _stage_audit(
        session, purchase.org_id, "hold_check", purchase.id, purchase.facts_version
    )
    if hold is not None:
        _set_hold(session, purchase, hold)
        purchase.item_status = "HELD"
        return
    _clear_holds(session, purchase, release_version=purchase.facts_version)
    states = _states_for_purchase(lines, facts)
    _stage_audit(
        session,
        purchase.org_id,
        "jurisdiction_set",
        purchase.id,
        purchase.facts_version,
    )
    if undeclared(states):
        _item_nhr(session, purchase, ruleset, "UNDECLARED_JURISDICTION", lines)
        return
    if not states:
        _set_hold(
            session, purchase, HoldDecision("MISSING_REQUIRED_FACT", "user_location")
        )
        purchase.item_status = "HELD"
        return
    _stage_audit(
        session, purchase.org_id, "engine_call", purchase.id, purchase.facts_version
    )
    for line in lines:
        line_states = _states_for_line(line, facts)
        for jurisdiction in line_states:
            _evaluate_one(
                session, purchase, line, jurisdiction, facts, ruleset, len(states)
            )
    _stage_audit(
        session, purchase.org_id, "response_check", purchase.id, purchase.facts_version
    )
    purchase.item_status = "DETERMINED"


def _ingest(
    session: Session,
    *,
    org_id: str,
    event_id: str,
    signature_result: str,
    payload: str,
    vendor: str,
    invoice_number: str,
    invoice_date: str,
    entity_amount: str,
    ocr_taxes: str | None,
    category: str | None,
    bill_to: str,
    lines: tuple[LineInput, ...],
    demo_scenario: str | None,
    extraction_mode: str,
    transaction_id: str,
) -> IngestResult:
    existing = _find_event(session, org_id, event_id)
    if existing is not None and existing.purchase_id is not None:
        return IngestResult(existing.purchase_id, None, True, existing.id)
    if existing is not None:
        event = existing
    else:
        event = SourceEvent(
            id=str(uuid4()),
            org_id=org_id,
            source="ramp",
            external_event_id=event_id,
            payload_hash=_hash(payload),
            signature_result=signature_result,
            purchase_id=None,
            created_at=utcnow(),
        )
        session.add(event)
        session.flush()
    matched, ambiguous = _invoice_candidates(
        session, org_id, vendor, invoice_number, invoice_date, entity_amount
    )
    if matched is not None:
        event.signature_result = signature_result
        linked = _link_exact_invoice(session, matched, event, ocr_taxes, job=None)
        return IngestResult(matched.id, linked.id, True, event.id)
    purchase = Purchase(
        id=str(uuid4()),
        org_id=org_id,
        vendor=vendor,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        entity_name=ENTITY_NAME,
        currency="USD",
        item_status="INTAKEN",
        ramp_transaction_id=transaction_id,
        ramp_entity_amount=entity_amount,
        ramp_ocr_taxes=ocr_taxes,
        category=category,
        demo_scenario=demo_scenario,
        extraction_mode=extraction_mode,
        facts_version=1,
        bill_to=bill_to or None,
        created_at=utcnow(),
    )
    session.add(purchase)
    document = Document(
        id=str(uuid4()),
        purchase_id=purchase.id,
        sha256=_hash(invoice_number),
        mime_type="application/json",
        pages=1,
    )
    session.add(document)
    stored_lines: list[PurchaseLine] = []
    for number, line in enumerate(lines, start=1):
        row = PurchaseLine(
            id=str(uuid4()),
            purchase_id=purchase.id,
            line_number=number,
            description=line.description,
            quantity=line.quantity,
            unit_price=line.line_amount,
            line_amount=line.line_amount,
            service_period_start=line.service_start,
            service_period_end=line.service_end,
            delivery_date=line.delivery_date,
            location_name=line.location_name,
            shipping_amount=line.shipping,
        )
        session.add(row)
        stored_lines.append(row)
    session.flush()
    _write_facts(session, purchase, document, stored_lines, lines, bill_to)
    _stage_audit(session, org_id, "deduplicate", purchase.id, purchase.facts_version)
    _stage_audit(session, org_id, "extract", purchase.id, purchase.facts_version)
    _stage_audit(session, org_id, "admit_facts", purchase.id, purchase.facts_version)
    event.purchase_id = purchase.id
    event.signature_result = signature_result
    for other in ambiguous:
        _record_ambiguous(session, org_id, purchase.id, other.id)
    job = _enqueue(session, org_id, purchase.id)
    return IngestResult(purchase.id, job.id, False, event.id)


def _write_facts(
    session: Session,
    purchase: Purchase,
    document: Document,
    stored_lines: list[PurchaseLine],
    inputs: tuple[LineInput, ...],
    bill_to: str,
) -> None:
    header = {
        "vendor_name": purchase.vendor,
        "invoice_date": purchase.invoice_date,
        "buyer_entity": purchase.entity_name,
    }
    if purchase.category == "REMOTE_ACCESS_SAAS":
        header["delivery_method"] = "electronic"
        header["access_model"] = "remote"
    for key, value in header.items():
        if not value:
            # Missing is not an empty string: no fact, so the hold check names it.
            continue
        _add_fact(
            session,
            purchase,
            document,
            line_id=None,
            key=key,
            value=value,
            confirmation="CONFIRMED",
            rung=None,
            passage=f"Synthetic invoice {purchase.invoice_number}",
        )
    if bill_to:
        _add_fact(
            session,
            purchase,
            document,
            line_id=None,
            key="bill_to",
            value=bill_to,
            confirmation="UNCONFIRMED",
            rung=None,
            passage="Bill-to is context only and does not confirm use or delivery.",
        )
    for stored, incoming in zip(stored_lines, inputs, strict=True):
        _add_fact(
            session,
            purchase,
            document,
            stored.id,
            "line_description",
            incoming.description,
            "CONFIRMED",
            None,
            incoming.description,
        )
        _add_fact(
            session,
            purchase,
            document,
            stored.id,
            "line_amount",
            incoming.line_amount,
            "CONFIRMED",
            None,
            incoming.line_amount,
        )
        if incoming.tax_charged is not None:
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "tax_charged",
                incoming.tax_charged,
                "CONFIRMED",
                None,
                f"Tax charged printed as {incoming.tax_charged}",
            )
        if incoming.tax_jurisdiction:
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "tax_charged_jurisdiction",
                incoming.tax_jurisdiction,
                "CONFIRMED",
                None,
                f"Tax jurisdiction printed as {incoming.tax_jurisdiction}",
            )
        if incoming.service_start and incoming.service_end:
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "service_period",
                f"{incoming.service_start}/{incoming.service_end}",
                "CONFIRMED",
                None,
                f"{incoming.service_start} to {incoming.service_end}",
            )
        if incoming.seats is not None:
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "seat_count",
                str(incoming.seats),
                "CONFIRMED",
                None,
                f"{incoming.seats} seats",
            )
        if incoming.location_state:
            confirmation = location_confirmation(
                rung=incoming.location_rung,
                seats=incoming.seats,
                seats_covered=incoming.seats_covered,
                named_single_office=incoming.named_single_office,
                from_bill_to=False,
                low_confidence=incoming.low_confidence,
                recorded_confirmation=incoming.recorded_confirmation,
            )
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "user_location",
                incoming.location_state,
                confirmation,
                incoming.location_rung,
                incoming.location_name or incoming.location_state,
            )
        if (
            purchase.category == "COMPUTER_HARDWARE"
            and incoming.location_state
            and incoming.delivery_date
        ):
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "delivery_evidence",
                incoming.location_state,
                "CONFIRMED",
                None,
                f"Delivered {incoming.delivery_date} to {incoming.location_name}",
            )
        if incoming.shipping is not None:
            _add_fact(
                session,
                purchase,
                document,
                stored.id,
                "shipping",
                incoming.shipping,
                "CONFIRMED",
                None,
                f"Shipping printed as {incoming.shipping}",
            )


def _add_fact(
    session: Session,
    purchase: Purchase,
    document: Document,
    line_id: str | None,
    key: str,
    value: str,
    confirmation: str,
    rung: int | None,
    passage: str,
) -> None:
    fact = Fact(
        id=str(uuid4()),
        org_id=purchase.org_id,
        purchase_id=purchase.id,
        line_id=line_id,
        field_key=key,
        value=value,
        origin="AI_EXTRACTED"
        if purchase.extraction_mode == "replay"
        else "RAMP_RECORD",
        source="replay recording"
        if purchase.extraction_mode == "replay"
        else "ramp transaction",
        evidence_rung=rung,
        confirmation=confirmation,
        version=purchase.facts_version,
        is_current=True,
        supersedes=None,
    )
    session.add(fact)
    session.flush()
    session.add(
        Evidence(
            id=str(uuid4()),
            fact_id=fact.id,
            document_id=document.id,
            page=1,
            passage=passage,
            provenance=purchase.extraction_mode,
        )
    )


def _apply_cross_check(
    purchase: Purchase, lines: list[PurchaseLine], facts: list[Fact]
) -> None:
    line_total = sum(
        (parse_decimal(line.line_amount) for line in lines), start=parse_decimal("0")
    )
    if purchase.ramp_entity_amount and differs_by_more_than_one_cent(
        purchase.ramp_entity_amount, f"{line_total:.2f}"
    ):
        for fact in facts:
            if fact.field_key == "line_amount":
                fact.confirmation = "UNCONFIRMED"
    if purchase.ramp_ocr_taxes is None:
        return
    charged = [
        parse_decimal(fact.value)
        for fact in facts
        if fact.field_key == "tax_charged" and fact.value is not None
    ]
    if not charged:
        return
    tax_total = sum(charged, start=parse_decimal("0"))
    if differs_by_more_than_one_cent(purchase.ramp_ocr_taxes, f"{tax_total:.2f}"):
        for fact in facts:
            if fact.field_key == "tax_charged":
                fact.confirmation = "UNCONFIRMED"


def _first_hold(
    category: str, lines: list[PurchaseLine], facts: list[Fact], ruleset: Ruleset
) -> HoldDecision | None:
    for line in lines:
        decision = decide_hold(category, _views(facts, line.id), ruleset.catalog)
        if decision is not None:
            return decision
    return None


def _views(facts: list[Fact], line_id: str) -> dict[str, FactView]:
    views: dict[str, FactView] = {}
    for fact in facts:
        if fact.line_id not in (None, line_id):
            continue
        if fact.field_key in views and fact.line_id is None:
            continue
        views[fact.field_key] = FactView(
            key=fact.field_key,
            value=fact.value,
            present=fact.value is not None,
            confirmation=fact.confirmation,
        )
    return views


def _states_for_purchase(lines: list[PurchaseLine], facts: list[Fact]) -> list[str]:
    found: list[str] = []
    for line in lines:
        for state in _states_for_line(line, facts):
            if state not in found:
                found.append(state)
    return found


def _states_for_line(line: PurchaseLine, facts: list[Fact]) -> list[str]:
    return confirmed_states(_views(facts, line.id))


def _evaluate_one(
    session: Session,
    purchase: Purchase,
    line: PurchaseLine,
    jurisdiction: str,
    facts: list[Fact],
    ruleset: Ruleset,
    state_count: int,
) -> None:
    views = _views(facts, line.id)
    tax = views.get("tax_charged")
    if tax is None or tax.value is None:
        return
    request = EngineLineRequest(
        jurisdiction=jurisdiction,
        category=purchase.category or "",
        invoice_date=date.fromisoformat(purchase.invoice_date),
        service_period_start=_optional_date(line.service_period_start),
        delivery_date=_optional_date(line.delivery_date),
        line_amount=line.line_amount,
        tax_charged=tax.value,
        shipping=line.shipping_amount,
        location_name=line.location_name or "",
        state_count=state_count,
    )
    result = evaluate_line(ruleset, request)
    if result.outcome == "supported":
        label: str | None = "ENGINE_SUPPORTED"
        unit_status = "READY_FOR_REVIEW"
    elif result.outcome == "unsupported":
        label = "NEEDS_HUMAN_REVIEW"
        unit_status = "NHR_ENGINE"
    else:
        label = None
        unit_status = "DETERMINATION_ERROR"
    session.add(
        Determination(
            id=str(uuid4()),
            org_id=purchase.org_id,
            purchase_id=purchase.id,
            line_id=line.id,
            jurisdiction=jurisdiction,
            facts_version=purchase.facts_version,
            ruleset_version=ruleset.version,
            label=label,
            nhr_reason=result.nhr_reason,
            direction=result.direction,
            derived_direction=result.derived_direction,
            rate=result.rate,
            location_code=result.location_code,
            expected_tax=result.expected_tax,
            engine_amount=result.adjustment_amount,
            rule_reference=result.rule_reference,
            engine_treatment=result.treatment,
            unit_status=unit_status,
            version=1,
            is_current=True,
            error_code=result.error_code,
            engine_request=json.dumps(_request_json(request), sort_keys=True),
            engine_response=json.dumps(asdict(result), sort_keys=True),
        )
    )


def _request_json(request: EngineLineRequest) -> dict[str, object]:
    fields = asdict(request)
    for key in ("invoice_date", "service_period_start", "delivery_date"):
        value = getattr(request, key)
        fields[key] = value.isoformat() if value is not None else None
    return fields


def _item_nhr(
    session: Session,
    purchase: Purchase,
    ruleset: Ruleset,
    reason: str,
    lines: list[PurchaseLine],
) -> None:
    session.add(
        Determination(
            id=str(uuid4()),
            org_id=purchase.org_id,
            purchase_id=purchase.id,
            line_id=lines[0].id if lines else purchase.id,
            jurisdiction="ITEM",
            facts_version=purchase.facts_version,
            ruleset_version=ruleset.version,
            label="NEEDS_HUMAN_REVIEW",
            nhr_reason=reason,
            direction=None,
            derived_direction=False,
            rate=None,
            location_code=None,
            expected_tax=None,
            engine_amount=None,
            rule_reference=None,
            engine_treatment=None,
            unit_status="NHR_ENGINE",
            version=1,
            is_current=True,
            error_code=None,
        )
    )
    purchase.item_status = "NHR_PRE_DETERMINATION"


def _set_hold(session: Session, purchase: Purchase, decision: HoldDecision) -> None:
    current = session.scalars(
        select(Hold).where(Hold.purchase_id == purchase.id, Hold.active.is_(True))
    ).all()
    for hold in current:
        if hold.reason == decision.reason and hold.named_fact == decision.named_fact:
            return
        hold.active = False
        hold.releasing_fact_version = purchase.facts_version
    hold = Hold(
        id=str(uuid4()),
        purchase_id=purchase.id,
        reason=decision.reason,
        named_fact=decision.named_fact,
        active=True,
        releasing_fact_version=None,
    )
    session.add(hold)
    session.flush()
    session.add(
        Question(
            id=str(uuid4()),
            hold_id=hold.id,
            purchase_id=purchase.id,
            fact_key=decision.named_fact,
            recipient="spend owner",
            asked_at=utcnow(),
            answer=None,
            state="open",
        )
    )


def enqueue_advance(session: Session, org_id: str, purchase_id: str) -> Job:
    return _enqueue(session, org_id, purchase_id)


def _clear_holds(session: Session, purchase: Purchase, release_version: int) -> None:
    holds = session.scalars(
        select(Hold).where(Hold.purchase_id == purchase.id, Hold.active.is_(True))
    ).all()
    for hold in holds:
        hold.active = False
        hold.releasing_fact_version = release_version


def _supersede_current(session: Session, purchase: Purchase) -> None:
    rows = session.scalars(
        select(Determination).where(
            Determination.purchase_id == purchase.id,
            Determination.is_current.is_(True),
        )
    ).all()
    for row in rows:
        if row.facts_version == purchase.facts_version:
            continue
        row.is_current = False
        row.unit_status = "SUPERSEDED"


def _enqueue(session: Session, org_id: str, purchase_id: str) -> Job:
    waiting = session.scalar(
        select(Job).where(
            Job.purchase_id == purchase_id,
            Job.status.in_(("QUEUED", "RUNNING")),
        )
    )
    if waiting is not None:
        return waiting
    job = Job(
        id=str(uuid4()),
        org_id=org_id,
        kind="advance",
        purchase_id=purchase_id,
        status="QUEUED",
        lease_until=None,
        attempts=0,
        error_code=None,
        created_at=utcnow(),
    )
    session.add(job)
    session.flush()
    return job


def _link_exact_invoice(
    session: Session,
    purchase: Purchase,
    event: SourceEvent,
    ocr_taxes: str | None,
    *,
    job: Job | None,
) -> Job:
    """Keep both source events on one purchase and reconcile a new receipt tax."""
    event.purchase_id = purchase.id
    _note_ocr_evidence(session, purchase, ocr_taxes)
    _stage_audit(
        session,
        purchase.org_id,
        "deduplicate",
        purchase.id,
        purchase.facts_version,
    )
    # Queue before the fetch job stores purchase_id. A running fetch is not
    # the advance that reruns cross-check.
    queued = _enqueue(session, purchase.org_id, purchase.id)
    if job is not None:
        job.purchase_id = purchase.id
    return queued


def _note_ocr_evidence(
    session: Session, purchase: Purchase, ocr_taxes: str | None
) -> None:
    """Store a changed OCR tax and drop results keyed by the previous version.

    A missing tax line does not erase a tax already stored. Equal decimals keep
    the original sourced string and the current approval.
    """
    if ocr_taxes is None:
        return
    stored = purchase.ramp_ocr_taxes
    if stored is not None and parse_decimal(stored) == parse_decimal(ocr_taxes):
        return
    purchase.ramp_ocr_taxes = ocr_taxes
    purchase.facts_version += 1
    # Bump first so current rows, including approvals, no longer match.
    _supersede_current(session, purchase)


def _invoice_candidates(
    session: Session,
    org_id: str,
    vendor: str,
    invoice_number: str,
    invoice_date: str,
    amount: str,
) -> tuple[Purchase | None, list[Purchase]]:
    if not vendor.strip() or not invoice_number.strip():
        return None, []
    rows = list(
        session.scalars(
            select(Purchase)
            .where(Purchase.org_id == org_id)
            .order_by(Purchase.created_at, Purchase.id)
        ).all()
    )
    same = [
        row
        for row in rows
        if row.entity_name == ENTITY_NAME
        and row.vendor.strip().casefold() == vendor.strip().casefold()
        and row.invoice_number.strip().casefold() == invoice_number.strip().casefold()
    ]
    exact = [
        row
        for row in same
        if _invoice_dates_match(row.invoice_date, invoice_date)
        and _amount_equal(row, amount)
    ]
    matched = exact[0] if exact else None
    ambiguous = [row for row in same if matched is None or row.id != matched.id]
    return matched, ambiguous


def _invoice_dates_match(stored: str, incoming: str) -> bool:
    """Both sides need a real calendar date. Two blanks are not the same date."""
    left = _present_invoice_date(stored)
    right = _present_invoice_date(incoming)
    if left is None or right is None:
        return False
    return left == right


def _present_invoice_date(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    if parsed.isoformat() != text:
        return None
    return parsed


def _amount_equal(purchase: Purchase, amount: str) -> bool:
    stored = purchase.ramp_entity_amount
    if stored is None:
        return False
    return parse_decimal(stored) == parse_decimal(amount)


def _record_ambiguous(
    session: Session, org_id: str, left_id: str, right_id: str
) -> None:
    left = session.get(Purchase, left_id)
    right = session.get(Purchase, right_id)
    if left is None or right is None:
        return
    _stage_audit(
        session,
        org_id,
        f"ambiguous_invoice_match:{right.id}",
        left.id,
        left.facts_version,
    )
    _stage_audit(
        session,
        org_id,
        f"ambiguous_invoice_match:{left.id}",
        right.id,
        right.facts_version,
    )


def _stage_audit(
    session: Session,
    org_id: str,
    command: str,
    object_id: str,
    version: int,
    object_type: str = "purchase",
) -> None:
    found = session.scalar(
        select(AuditEvent).where(
            AuditEvent.org_id == org_id,
            AuditEvent.object_type == object_type,
            AuditEvent.object_id == object_id,
            AuditEvent.command == command,
            AuditEvent.object_version == version,
        )
    )
    if found is not None:
        return
    # Same writer as fact and review commands, so before_hash stays one chain.
    # The uq_audit_stage partial index is the retry guard; a concurrent retry
    # that wins the race raises IntegrityError, which means the event exists.
    from sqlalchemy.exc import IntegrityError

    from app.domain.commands import _audit

    try:
        with session.begin_nested():
            _audit(session, org_id, "system", command, object_type, object_id, version)
    except IntegrityError:
        pass


def _find_event(session: Session, org_id: str, event_id: str) -> SourceEvent | None:
    return session.scalar(
        select(SourceEvent).where(
            SourceEvent.org_id == org_id,
            SourceEvent.source == "ramp",
            SourceEvent.external_event_id == event_id,
        )
    )


def _optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
