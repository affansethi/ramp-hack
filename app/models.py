"""Tables for the demo. Money columns are decimal strings. No float, no net figure."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))


class SetupRow(Base):
    __tablename__ = "setup"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    jurisdictions: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[str] = mapped_column(String(10))
    period_end: Mapped[str] = mapped_column(String(10))
    basis: Mapped[str] = mapped_column(String(32))


class DemoSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("persons.id"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    created_at: Mapped[str] = mapped_column(String(32))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("org_id", "key", name="uq_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    key: Mapped[str] = mapped_column(String(200))
    body_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    command: Mapped[str] = mapped_column(String(80))
    object_type: Mapped[str] = mapped_column(String(80))
    object_id: Mapped[str] = mapped_column(String(36))
    object_version: Mapped[int] = mapped_column(Integer)
    before_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class SourceEvent(Base):
    __tablename__ = "source_events"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "source", "external_event_id", name="uq_source_event"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    source: Mapped[str] = mapped_column(String(32))
    external_event_id: Mapped[str] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64))
    signature_result: Mapped[str] = mapped_column(String(32))
    purchase_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    vendor: Mapped[str] = mapped_column(String(200))
    invoice_number: Mapped[str] = mapped_column(String(80))
    invoice_date: Mapped[str] = mapped_column(String(10))
    entity_name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3))
    item_status: Mapped[str] = mapped_column(String(40))
    ramp_transaction_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ramp_entity_amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ramp_ocr_taxes: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    demo_scenario: Mapped[str | None] = mapped_column(String(40), nullable=True)
    extraction_mode: Mapped[str] = mapped_column(String(32))
    facts_version: Mapped[int] = mapped_column(Integer, default=1)
    bill_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    line_number: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(400))
    quantity: Mapped[str] = mapped_column(String(32))
    unit_price: Mapped[str] = mapped_column(String(32))
    line_amount: Mapped[str] = mapped_column(String(32))
    service_period_start: Mapped[str | None] = mapped_column(String(10), nullable=True)
    service_period_end: Mapped[str | None] = mapped_column(String(10), nullable=True)
    delivery_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    location_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipping_amount: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(80))
    pages: Mapped[int] = mapped_column(Integer)


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    line_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    field_key: Mapped[str] = mapped_column(String(80))
    value: Mapped[str | None] = mapped_column(String(400), nullable=True)
    origin: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(200))
    evidence_rung: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmation: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    supersedes: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fact_id: Mapped[str] = mapped_column(ForeignKey("facts.id"), index=True)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page: Mapped[int] = mapped_column(Integer)
    passage: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(80))


class Hold(Base):
    __tablename__ = "holds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    reason: Mapped[str] = mapped_column(String(40))
    named_fact: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    releasing_fact_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hold_id: Mapped[str] = mapped_column(ForeignKey("holds.id"), index=True)
    purchase_id: Mapped[str] = mapped_column(String(36), index=True)
    fact_key: Mapped[str] = mapped_column(String(80))
    recipient: Mapped[str] = mapped_column(String(200))
    asked_at: Mapped[str] = mapped_column(String(32))
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32))


class Determination(Base):
    __tablename__ = "determinations"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "line_id",
            "jurisdiction",
            "facts_version",
            "ruleset_version",
            name="uq_determination",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    line_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    jurisdiction: Mapped[str] = mapped_column(String(8))
    facts_version: Mapped[int] = mapped_column(Integer)
    ruleset_version: Mapped[str] = mapped_column(String(32))
    label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    nhr_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    derived_direction: Mapped[bool] = mapped_column(Boolean, default=False)
    rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    location_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_tax: Mapped[str | None] = mapped_column(String(32), nullable=True)
    engine_amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    engine_treatment: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_status: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    engine_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_response: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    determination_id: Mapped[str] = mapped_column(
        ForeignKey("determinations.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    approval_path: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))


class LiabilityLine(Base):
    __tablename__ = "liability_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    determination_id: Mapped[str] = mapped_column(
        ForeignKey("determinations.id"), index=True
    )
    purchase_id: Mapped[str] = mapped_column(String(36), index=True)
    jurisdiction: Mapped[str] = mapped_column(String(8))
    period: Mapped[str] = mapped_column(String(16))
    amount: Mapped[str] = mapped_column(String(32))
    approver_id: Mapped[str] = mapped_column(String(36))
    approved_at: Mapped[str] = mapped_column(String(32))


class RefundCandidate(Base):
    __tablename__ = "refund_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    determination_id: Mapped[str] = mapped_column(
        ForeignKey("determinations.id"), index=True
    )
    purchase_id: Mapped[str] = mapped_column(String(36), index=True)
    amount: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(40))
    approver_id: Mapped[str] = mapped_column(String(36))
    approved_at: Mapped[str] = mapped_column(String(32))


class JournalEntryDraft(Base):
    __tablename__ = "journal_entry_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    determination_id: Mapped[str] = mapped_column(
        ForeignKey("determinations.id"), index=True
    )
    purchase_id: Mapped[str] = mapped_column(String(36))
    debit_account: Mapped[str] = mapped_column(String(80))
    credit_account: Mapped[str] = mapped_column(String(80))
    amount: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))


class RefundLetterDraft(Base):
    __tablename__ = "refund_letter_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    determination_id: Mapped[str] = mapped_column(
        ForeignKey("determinations.id"), index=True
    )
    purchase_id: Mapped[str] = mapped_column(String(36))
    vendor: Mapped[str] = mapped_column(String(200))
    invoice_number: Mapped[str] = mapped_column(String(80))
    amount: Mapped[str] = mapped_column(String(32))
    basis_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))


class Workpaper(Base):
    __tablename__ = "workpapers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    entity_name: Mapped[str] = mapped_column(String(200))
    jurisdiction: Mapped[str] = mapped_column(String(8))
    period: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frozen_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class WorkpaperLine(Base):
    __tablename__ = "workpaper_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workpaper_id: Mapped[str] = mapped_column(ForeignKey("workpapers.id"), index=True)
    liability_line_id: Mapped[str] = mapped_column(ForeignKey("liability_lines.id"))


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    input_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    purchase_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    lease_until: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))
