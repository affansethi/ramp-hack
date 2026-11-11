"""Review, fact corrections, totals, and the simulated workpaper."""

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.domain.errors import DomainError
from app.domain.evaluate import quarter_key
from app.domain.moneyutil import money_json, parse_decimal
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
    Person,
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
        "service_period",
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
    if field == "service_period":
        _apply_service_period(session, purchase, target_line, stored_value)
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


def _require_named_reviewer(session: Session, org_id: str, actor_id: str) -> None:
    person = session.get(Person, actor_id)
    if (
        person is None
        or person.org_id != org_id
        or not person.name.strip()
        or person.role != "reviewer"
    ):
        raise DomainError("FORBIDDEN", "A named reviewer is required to decide.", 403)


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
    rules_dir: str = "rules",
) -> Determination:
    _require_named_reviewer(session, org_id, actor_id)
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
            session,
            determination,
            purchase,
            approval_path,
            actor_id,
            reason,
            rules_dir=rules_dir,
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
    expected_version: int,
) -> Determination:
    _require_named_reviewer(session, org_id, actor_id)
    determination = _determination(session, org_id, determination_id)
    if determination.version != expected_version or not determination.is_current:
        raise DomainError(
            "STALE_VERSION", "This result changed. Reload it and try again.", 409
        )
    if determination.label != "NEEDS_HUMAN_REVIEW":
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
    grouped: dict[tuple[str, str], list[str]] = {}
    refund_rows: list[dict[str, object]] = []
    nhr_count = 0
    error_count = 0
    unreviewed_by_direction: dict[str, int] = {}
    for determination in session.scalars(
        select(Determination).where(Determination.org_id == org_id)
    ).all():
        if (
            not determination.is_current
            or determination.purchase_id in held_ids
            or determination.unit_status in ("REJECTED", "SUPERSEDED")
        ):
            continue
        if determination.unit_status == "DETERMINATION_ERROR":
            error_count += 1
        elif determination.label == "NEEDS_HUMAN_REVIEW":
            nhr_count += 1
        elif determination.unit_status == "READY_FOR_REVIEW":
            direction = determination.direction or "UNKNOWN"
            unreviewed_by_direction[direction] = (
                unreviewed_by_direction.get(direction, 0) + 1
            )
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
            grouped.setdefault(key, []).append(liability.amount)
        elif determination.unit_status == "APPROVED_FOR_PURSUIT":
            refund = session.scalar(
                select(RefundCandidate).where(
                    RefundCandidate.determination_id == determination.id
                )
            )
            if refund is None or refund.status != "APPROVED_FOR_PURSUIT":
                continue
            purchase = session.get(Purchase, refund.purchase_id)
            letter = session.scalar(
                select(RefundLetterDraft).where(
                    RefundLetterDraft.determination_id == determination.id
                )
            )
            refund_rows.append(
                {
                    "determination_id": determination.id,
                    "purchase_id": refund.purchase_id,
                    "vendor": purchase.vendor if purchase is not None else "",
                    "amount": {"value": refund.amount, "currency": "USD"},
                    "refund_letter_id": letter.id if letter is not None else None,
                }
            )
    groups = []
    for (jurisdiction, period), amounts in sorted(grouped.items()):
        total = sum((parse_decimal(value) for value in amounts), parse_decimal("0"))
        groups.append(
            {
                "jurisdiction": jurisdiction,
                "period": period,
                "total": {"value": f"{total:.2f}", "currency": "USD"},
                "line_count": len(amounts),
            }
        )
    refund_total = parse_decimal("0")
    for row in refund_rows:
        amount = row["amount"]
        if isinstance(amount, dict):
            refund_total += parse_decimal(str(amount["value"]))
    held: dict[str, object] = {
        "count": len(held_ids),
        "label": "purchase amount, not tax",
    }
    if held_ids:
        held["purchase_amount"] = {"value": f"{held_amount:.2f}", "currency": "USD"}
    return {
        "approved_liabilities": {
            "by_jurisdiction": groups,
            "message": None if groups else "No approved liabilities",
        },
        "refund_candidates": {
            "label": "not recovered cash",
            "count": len(refund_rows),
            "total": None
            if not refund_rows
            else {"value": f"{refund_total:.2f}", "currency": "USD"},
            "rows": refund_rows,
        },
        "held": held,
        "open_needs_human_review": {"count": nhr_count},
        "determination_errors": {"count": error_count},
        "unreviewed": {
            "by_direction": [
                {"direction": direction, "count": count}
                for direction, count in sorted(unreviewed_by_direction.items())
            ]
        },
    }


_BANNER = "Synthetic demo data"
_MARK = "DRAFT / SIMULATED"
_EMPTY_LIABILITIES = "No approved liabilities"
_REFUND_LABEL = "not recovered cash"
_NO_APPROVED = "No approved liability lines. The draft was not changed."
_OPEN_REASONS = frozenset({"NHR", "ERROR", "UNREVIEWED"})


@dataclass
class _Sections:
    message: str | None
    liabilities: list[tuple[str, str]]
    held_count: int
    open_count: int
    excluded: list[tuple[str, str]]
    refunds: list[tuple[str, str]]
    charged_correctly_count: int

    def dump(self) -> str:
        payload = {
            "message": self.message,
            "liabilities": self.liabilities,
            "held_count": self.held_count,
            "open_count": self.open_count,
            "excluded": self.excluded,
            "refunds": self.refunds,
            "charged_correctly_count": self.charged_correctly_count,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def load(cls, raw: str) -> "_Sections":
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise DomainError("NOT_FOUND", "Workpaper export is unreadable.", 404)
        message = loaded.get("message")
        if message is not None and not isinstance(message, str):
            raise DomainError("NOT_FOUND", "Workpaper export is unreadable.", 404)
        return cls(
            message=message,
            liabilities=_pairs(loaded.get("liabilities")),
            held_count=_count(loaded.get("held_count")),
            open_count=_count(loaded.get("open_count")),
            excluded=_pairs(loaded.get("excluded")),
            refunds=_pairs(loaded.get("refunds")),
            charged_correctly_count=_count(loaded.get("charged_correctly_count")),
        )


def create_workpaper(
    session: Session, org_id: str, jurisdiction: str, period: str
) -> Workpaper:
    selection = _read_snapshot(session, org_id, jurisdiction, period)
    fingerprint = _fingerprint(jurisdiction, period, selection)
    ready = _ready_with_fingerprint(session, org_id, fingerprint)
    if (
        ready is not None
        and ready.jurisdiction == jurisdiction
        and ready.period == period
    ):
        return ready
    latest = _latest_workpaper(session, org_id, jurisdiction, period)
    if latest is not None and latest.status == "DRAFT":
        if latest.fingerprint != fingerprint:
            latest.version += 1
        latest.fingerprint = fingerprint
        latest.snapshot = selection.sections.dump()
        _replace_lines(session, latest, list(selection.liability_ids))
        return latest
    prior_ready = session.scalar(
        select(Workpaper.id).where(
            Workpaper.org_id == org_id,
            Workpaper.jurisdiction == jurisdiction,
            Workpaper.period == period,
            Workpaper.status == "READY_TO_FILE_SIMULATED",
        )
    )
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
        created_after_ready=prior_ready is not None,
        snapshot=selection.sections.dump(),
        created_at=utcnow(),
    )
    session.add(workpaper)
    session.flush()
    _replace_lines(session, workpaper, list(selection.liability_ids))
    return workpaper


def mark_workpaper_ready(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    workpaper_id: str,
    expected_version: int,
    reason: str,
    fingerprint: str | None = None,
) -> Workpaper:
    workpaper = session.get(Workpaper, workpaper_id)
    if workpaper is None or workpaper.org_id != org_id:
        raise DomainError("NOT_FOUND", "Workpaper not found.", 404)
    if workpaper.version != expected_version:
        raise DomainError(
            "STALE_VERSION", "The workpaper changed. Reload it and try again.", 409
        )
    # One selection feeds the fingerprint, the linked lines, and the snapshot.
    selection = _read_snapshot(
        session, org_id, workpaper.jurisdiction, workpaper.period
    )
    server_fingerprint = _fingerprint(
        workpaper.jurisdiction, workpaper.period, selection
    )
    if fingerprint is not None and fingerprint != server_fingerprint:
        raise DomainError(
            "STALE_VERSION", "The workpaper changed. Reload it and try again.", 409
        )
    if not selection.liability_ids:
        raise DomainError("NO_APPROVED_LIABILITY_LINES", _NO_APPROVED, 422)
    if (
        workpaper.status == "READY_TO_FILE_SIMULATED"
        and workpaper.fingerprint != server_fingerprint
    ):
        raise DomainError(
            "STALE_VERSION", "This version is frozen. Open a new draft.", 409
        )
    existing = _ready_with_fingerprint(session, org_id, server_fingerprint)
    if existing is not None:
        return existing
    _replace_lines(session, workpaper, list(selection.liability_ids))
    workpaper.fingerprint = server_fingerprint
    workpaper.status = "READY_TO_FILE_SIMULATED"
    workpaper.frozen_at = utcnow()
    workpaper.snapshot = selection.sections.dump()
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


def workpaper_document(session: Session, workpaper: Workpaper) -> dict[str, object]:
    return _document(workpaper, _sections_for(session, workpaper))


def export_workpaper(
    session: Session,
    org_id: str,
    workpaper_id: str,
    export_format: str,
    *,
    data_mode: str,
    engine_mode: str,
) -> tuple[str, str]:
    workpaper = session.get(Workpaper, workpaper_id)
    if workpaper is None or workpaper.org_id != org_id:
        raise DomainError("NOT_FOUND", "Workpaper not found.", 404)
    if export_format not in ("csv", "json"):
        raise DomainError("UNKNOWN_FORMAT", "Export format must be csv or json.", 422)
    sections = _sections_for(session, workpaper)
    document = _document(workpaper, sections)
    document["data_mode"] = data_mode
    document["engine_mode"] = engine_mode
    if export_format == "json":
        body = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return "application/json", body
    return "text/csv", _render_csv(workpaper, sections, data_mode, engine_mode)


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
    rules_dir: str = "rules",
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
            basis_text=_filing_basis_text(rules_dir, purchase, determination),
            status="DRAFT",
        )
    )


_REFUND_LETTER_BASIS: dict[tuple[str, str | None], str] = {
    ("CA", "REMOTE_ACCESS_SAAS"): "ca_saas_vendor_tax"
}


def _filing_basis_text(
    rules_dir: str, purchase: Purchase, determination: Determination
) -> str:
    path = Path(rules_dir) / "filing-basis.txt"
    if not path.exists():
        return ""
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            current = line[1:-1]
            sections[current] = []
            continue
        if current and line and not line.startswith("#"):
            sections[current].append(line)
    # Only a basis written for a vendor refund letter may enter one. The NY form
    # note and the WA journal note serve other drafts, so a refund in those
    # states keeps an empty basis and the unsigned note.
    key = _REFUND_LETTER_BASIS.get((determination.jurisdiction, purchase.category))
    if key is None:
        return ""
    text = " ".join(sections.get(key, []))
    lowered = text.lower()
    if "was sent" in lowered:
        return ""
    # "filed" or "paid" only in a sentence that also says simulated (RG-8).
    for sentence in re.split(r"[.!?]", lowered):
        if re.search(r"\b(filed|paid)\b", sentence) and "simulated" not in sentence:
            return ""
    return text


def _pairs(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        raise DomainError("NOT_FOUND", "Workpaper export is unreadable.", 404)
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise DomainError("NOT_FOUND", "Workpaper export is unreadable.", 404)
        pairs.append((item[0], item[1]))
    return pairs


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError("NOT_FOUND", "Workpaper export is unreadable.", 404)
    return value


def _document(workpaper: Workpaper, sections: _Sections) -> dict[str, object]:
    return {
        "banner": _BANNER,
        "mark": _MARK,
        "id": workpaper.id,
        "entity_name": workpaper.entity_name,
        "jurisdiction": workpaper.jurisdiction,
        "period": workpaper.period,
        "version": workpaper.version,
        "status": workpaper.status,
        "fingerprint": workpaper.fingerprint,
        "frozen_at": workpaper.frozen_at,
        "created_after_ready": workpaper.created_after_ready,
        "approved_liabilities_message": sections.message,
        "approved_liabilities": [
            {"invoice": invoice, "amount": money_json(amount)}
            for invoice, amount in sections.liabilities
        ],
        "held_count": sections.held_count,
        "open_count": sections.open_count,
        "excluded": [
            {"invoice": invoice, "reason": reason}
            for invoice, reason in sections.excluded
        ],
        "refund_candidates": {
            "label": _REFUND_LABEL,
            "lines": [
                {"invoice": invoice, "amount": money_json(amount)}
                for invoice, amount in sections.refunds
            ],
        },
        "charged_correctly_count": sections.charged_correctly_count,
    }


def _render_csv(
    workpaper: Workpaper, sections: _Sections, data_mode: str, engine_mode: str
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    rows: list[list[str]] = [
        ["banner", _BANNER],
        ["mark", _MARK],
        ["data_mode", data_mode],
        ["engine_mode", engine_mode],
        ["status", workpaper.status],
        ["jurisdiction", workpaper.jurisdiction],
        ["period", workpaper.period],
        ["version", str(workpaper.version)],
        ["fingerprint", workpaper.fingerprint or ""],
        ["created_after_ready", "true" if workpaper.created_after_ready else "false"],
        ["frozen_at", workpaper.frozen_at or ""],
        [],
        ["section", "approved liabilities"],
    ]
    if sections.message is not None:
        rows.append(["message", sections.message])
    rows.append(["held_count", str(sections.held_count)])
    rows.append(["open_count", str(sections.open_count)])
    if sections.liabilities:
        rows.append(["invoice", "amount"])
        rows.extend([invoice, amount] for invoice, amount in sections.liabilities)
    rows.extend([[], ["section", "excluded"], ["invoice", "reason"]])
    rows.extend([invoice, reason] for invoice, reason in sections.excluded)
    rows.extend([[], ["section", _REFUND_LABEL]])
    if sections.refunds:
        rows.append(["invoice", "amount"])
        rows.extend([invoice, amount] for invoice, amount in sections.refunds)
    rows.extend(
        [
            [],
            ["section", "charged correctly"],
            ["count", str(sections.charged_correctly_count)],
        ]
    )
    for row in rows:
        writer.writerow([_escape(cell) for cell in row])
    return buffer.getvalue()


def _sections_for(session: Session, workpaper: Workpaper) -> _Sections:
    if workpaper.snapshot:
        return _Sections.load(workpaper.snapshot)
    return _read_snapshot(
        session, workpaper.org_id, workpaper.jurisdiction, workpaper.period
    ).sections


class _FilingPurchase(NamedTuple):
    id: str
    invoice_number: str
    invoice_date: str


class _FilingDetermination(NamedTuple):
    id: str
    purchase_id: str
    unit_status: str
    label: str | None
    direction: str | None
    is_current: bool


class _FilingLiability(NamedTuple):
    id: str
    purchase_id: str
    jurisdiction: str
    amount: str
    determination_id: str


@dataclass(frozen=True)
class _Selection:
    sections: _Sections
    liability_ids: tuple[str, ...]


# One statement so a commit cannot land between the liability read and the
# excluded, refund, and charged-correctly reads.
_SNAPSHOT_SQL = """
WITH org_purchases AS (
    SELECT id, invoice_number, invoice_date
    FROM purchases
    WHERE org_id = :org_id
)
SELECT kind, id, purchase_id, c1, c2, c3, c4
FROM (
    SELECT
        CAST('purchase' AS TEXT) AS kind,
        CAST(id AS TEXT) AS id,
        CAST(id AS TEXT) AS purchase_id,
        CAST(invoice_number AS TEXT) AS c1,
        CAST(invoice_date AS TEXT) AS c2,
        CAST(NULL AS TEXT) AS c3,
        CAST(NULL AS TEXT) AS c4
    FROM org_purchases
    UNION ALL
    SELECT
        CAST('hold' AS TEXT),
        CAST(holds.id AS TEXT),
        CAST(holds.purchase_id AS TEXT),
        CASE WHEN holds.active THEN '1' ELSE '0' END,
        CAST(NULL AS TEXT),
        CAST(NULL AS TEXT),
        CAST(NULL AS TEXT)
    FROM holds
    JOIN org_purchases ON org_purchases.id = holds.purchase_id
    UNION ALL
    SELECT
        CAST('determination' AS TEXT),
        CAST(determinations.id AS TEXT),
        CAST(determinations.purchase_id AS TEXT),
        CAST(determinations.unit_status AS TEXT),
        CAST(determinations.label AS TEXT),
        CAST(determinations.direction AS TEXT),
        CASE WHEN determinations.is_current THEN '1' ELSE '0' END
    FROM determinations
    JOIN org_purchases ON org_purchases.id = determinations.purchase_id
    UNION ALL
    SELECT
        CAST('liability' AS TEXT),
        CAST(liability_lines.id AS TEXT),
        CAST(liability_lines.purchase_id AS TEXT),
        CAST(liability_lines.jurisdiction AS TEXT),
        CAST(liability_lines.amount AS TEXT),
        CAST(liability_lines.determination_id AS TEXT),
        CAST(NULL AS TEXT)
    FROM liability_lines
    JOIN org_purchases ON org_purchases.id = liability_lines.purchase_id
    UNION ALL
    SELECT
        CAST('refund' AS TEXT),
        CAST(refund_candidates.id AS TEXT),
        CAST(refund_candidates.purchase_id AS TEXT),
        CAST(refund_candidates.amount AS TEXT),
        CAST(refund_candidates.determination_id AS TEXT),
        CAST(NULL AS TEXT),
        CAST(NULL AS TEXT)
    FROM refund_candidates
    JOIN org_purchases
        ON org_purchases.id = refund_candidates.purchase_id
) AS filing_rows
"""


def _fingerprint(jurisdiction: str, period: str, selection: _Selection) -> str:
    payload = "\n".join(
        (
            jurisdiction,
            period,
            ",".join(selection.liability_ids),
            selection.sections.dump(),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _read_snapshot(
    session: Session, org_id: str, jurisdiction: str, period: str
) -> _Selection:
    purchases: dict[str, _FilingPurchase] = {}
    active_holds: set[str] = set()
    determinations: list[_FilingDetermination] = []
    liabilities: list[_FilingLiability] = []
    refund_amounts: dict[str, str] = {}
    for kind, row_id, purchase_id, c1, c2, c3, c4 in _load_filing_rows(session, org_id):
        if kind == "purchase" and c1 is not None and c2 is not None:
            purchases[row_id] = _FilingPurchase(row_id, c1, c2)
        elif kind == "hold" and c1 == "1":
            active_holds.add(purchase_id)
        elif kind == "determination" and c1 is not None:
            determinations.append(
                _FilingDetermination(row_id, purchase_id, c1, c2, c3, c4 == "1")
            )
        elif (
            kind == "liability" and c1 is not None and c2 is not None and c3 is not None
        ):
            liabilities.append(_FilingLiability(row_id, purchase_id, c1, c2, c3))
        elif kind == "refund" and c1 is not None and c2 is not None:
            refund_amounts.setdefault(c2, c1)
    return _assemble_snapshot(
        purchases,
        active_holds,
        determinations,
        liabilities,
        refund_amounts,
        jurisdiction,
        period,
    )


def _load_filing_rows(
    session: Session, org_id: str
) -> list[tuple[str, str, str, str | None, str | None, str | None, str | None]]:
    loaded: list[
        tuple[str, str, str, str | None, str | None, str | None, str | None]
    ] = []
    for record in session.execute(text(_SNAPSHOT_SQL), {"org_id": org_id}):
        cells = list(record)
        if len(cells) != 7:
            continue
        kind, row_id, purchase_id = cells[0], cells[1], cells[2]
        if (
            not isinstance(kind, str)
            or not isinstance(row_id, str)
            or not isinstance(purchase_id, str)
        ):
            continue
        loaded.append(
            (
                kind,
                row_id,
                purchase_id,
                _optional_text(cells[3]),
                _optional_text(cells[4]),
                _optional_text(cells[5]),
                _optional_text(cells[6]),
            )
        )
    return loaded


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _assemble_snapshot(
    purchases: dict[str, _FilingPurchase],
    active_holds: set[str],
    determinations: list[_FilingDetermination],
    liabilities: list[_FilingLiability],
    refund_amounts: dict[str, str],
    jurisdiction: str,
    period: str,
) -> _Selection:
    by_id = {item.id: item for item in determinations}
    matched = [
        line
        for line in liabilities
        if _liability_in_workpaper(
            line, purchases, active_holds, by_id, jurisdiction, period
        )
    ]
    matched.sort(key=lambda line: (purchases[line.purchase_id].invoice_number, line.id))
    shown = [
        (purchases[line.purchase_id].invoice_number, line.amount) for line in matched
    ]
    by_purchase: dict[str, list[_FilingDetermination]] = {}
    for item in determinations:
        by_purchase.setdefault(item.purchase_id, []).append(item)
    excluded: list[tuple[str, str]] = []
    refunds: list[tuple[str, str]] = []
    held_count = 0
    open_count = 0
    charged_correctly_count = 0
    period_purchases = [
        purchase
        for purchase in purchases.values()
        if _invoice_quarter(purchase.invoice_date) == period
    ]
    period_purchases.sort(key=lambda purchase: (purchase.invoice_number, purchase.id))
    for purchase in period_purchases:
        current = [
            item
            for item in by_purchase.get(purchase.id, [])
            if item.is_current and item.unit_status != "SUPERSEDED"
        ]
        current.sort(key=lambda item: item.id)
        if purchase.id in active_holds:
            held_count += 1
            excluded.append((purchase.invoice_number, "HELD"))
        else:
            reasons = [
                reason
                for item in current
                if (reason := _excluded_reason(item.unit_status, item.label))
                is not None
            ]
            if any(reason in _OPEN_REASONS for reason in reasons):
                open_count += 1
            excluded.extend((purchase.invoice_number, reason) for reason in reasons)
            for item in current:
                if item.unit_status == "APPROVED_FOR_PURSUIT":
                    amount = refund_amounts.get(item.id)
                    if amount is not None:
                        refunds.append((purchase.invoice_number, amount))
                elif (
                    item.unit_status == "APPROVED"
                    and item.direction == "CHARGED_CORRECTLY"
                ):
                    charged_correctly_count += 1
        superseded = [
            item
            for item in by_purchase.get(purchase.id, [])
            if item.unit_status == "SUPERSEDED"
        ]
        excluded.extend((purchase.invoice_number, "SUPERSEDED") for _item in superseded)
    excluded.sort(key=lambda item: item)
    refunds.sort(key=lambda item: item)
    return _Selection(
        _Sections(
            message=None if shown else _EMPTY_LIABILITIES,
            liabilities=shown,
            held_count=held_count,
            open_count=open_count,
            excluded=excluded,
            refunds=refunds,
            charged_correctly_count=charged_correctly_count,
        ),
        tuple(sorted(line.id for line in matched)),
    )


def _liability_in_workpaper(
    line: _FilingLiability,
    purchases: dict[str, _FilingPurchase],
    active_holds: set[str],
    by_id: dict[str, _FilingDetermination],
    jurisdiction: str,
    period: str,
) -> bool:
    if line.jurisdiction != jurisdiction or line.purchase_id in active_holds:
        return False
    purchase = purchases.get(line.purchase_id)
    determination = by_id.get(line.determination_id)
    if purchase is None or determination is None:
        return False
    if _invoice_quarter(purchase.invoice_date) != period:
        return False
    return (
        determination.is_current
        and determination.unit_status == "APPROVED"
        and determination.direction == "LIABILITY"
    )


def _excluded_reason(unit_status: str, label: str | None) -> str | None:
    if unit_status == "DETERMINATION_ERROR":
        return "ERROR"
    if label == "NEEDS_HUMAN_REVIEW":
        return "NHR"
    if unit_status == "READY_FOR_REVIEW":
        return "UNREVIEWED"
    if unit_status == "REJECTED":
        return "REJECTED"
    return None


def _invoice_quarter(invoice_date: str) -> str:
    return quarter_key(date.fromisoformat(invoice_date))


def _ready_with_fingerprint(
    session: Session, org_id: str, fingerprint: str
) -> Workpaper | None:
    return session.scalar(
        select(Workpaper).where(
            Workpaper.org_id == org_id,
            Workpaper.status == "READY_TO_FILE_SIMULATED",
            Workpaper.fingerprint == fingerprint,
        )
    )


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


def _apply_service_period(
    session: Session, purchase: Purchase, target_line: str | None, value: str
) -> None:
    """Copy a line-scoped service_period fact onto the purchase line.

    The evaluator reads ``PurchaseLine.service_period_start/end``; without
    this copy a service-period edit would version the fact but leave the
    engine reading stale line columns.
    """
    try:
        start_raw, end_raw = value.split("/", 1)
        start = date.fromisoformat(start_raw.strip())
        end = date.fromisoformat(end_raw.strip())
    except ValueError:
        raise DomainError(
            "BAD_REQUEST", "service_period must be YYYY-MM-DD/YYYY-MM-DD.", 422
        ) from None
    if target_line is None:
        raise DomainError("BAD_REQUEST", "service_period needs a line.", 422)
    line = session.get(PurchaseLine, target_line)
    if line is None or line.purchase_id != purchase.id:
        raise DomainError("NOT_FOUND", "Purchase line not found.", 404)
    line.service_period_start = start.isoformat()
    line.service_period_end = end.isoformat()


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
    # Lock before looking up history, including when no first row exists yet.
    # Database transaction locks survive this helper until commit or rollback.
    with session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql":
            lock_identity = json.dumps([org_id, object_type, object_id])
            lock_key = int.from_bytes(
                sha256(lock_identity.encode()).digest()[:8], signed=True
            )
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
            )
        else:
            # SQLite has one writer: a no-op UPDATE acquires its transaction
            # write lock even for an object's first event (or an empty table).
            session.execute(
                text("UPDATE organizations SET id = id WHERE id = :org"),
                {"org": org_id},
            )
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
