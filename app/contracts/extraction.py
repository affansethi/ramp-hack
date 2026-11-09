"""Call A and Call B shapes. These schemas have no tax-decision fields."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

CategoryProposal = Literal[
    "REMOTE_ACCESS_SAAS", "COMPUTER_HARDWARE", "NO_SUPPORTED_CATEGORY"
]


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    passage: str


class LineExtraction(BaseModel):
    """Printed line fields. tax_charged is absent, null, or a decimal string."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    line_amount: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    shipping: str | None = None
    printed_tax_jurisdiction: str | None = None
    tax_charged: str | None = None
    tax_charged_present: bool = False


class CallA(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    bill_to: str | None = None
    ship_to: str | None = None
    lines: list[LineExtraction] = []
    evidence: list[EvidenceSpan] = []


class CallB(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: CategoryProposal | None = None
    rationale: str | None = None
    evidence_ids: list[str] = []
    unresolved_keys: list[str] = []
    question_draft: str | None = None


class CheckedExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_a: CallA
    call_b: CallB
