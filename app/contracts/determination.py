"""Determination read model.

Live JSON is exactly the fields on ``DeterminationRead``. ``data_mode`` and
``engine_mode`` stay on the parent response via ``meta()``.
``service_period_start`` stays on the purchase line. This model has no
``evidence_ids``.

``engine_request`` and ``engine_response`` are the stored engine trail,
returned read-only. They are null when that row has no trail.

``ILLUSTRATIVE_SPEC_CASE_1`` quotes Technical Spec v2 §7. It is illustrative
only: not a golden and not a live row. Do not assert it against API responses.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.contracts.money import Money

# Illustrative spec excerpt only. Not a live row and not an expected result.
ILLUSTRATIVE_SPEC_CASE_1: dict[str, object] = {
    "id": "det_case1",
    "line_id": "line_case1",
    "jurisdiction": "WA",
    "service_period_start": "2026-11-01",
    "facts_version": 3,
    "version": 1,
    "data_mode": "synthetic",
    "engine_mode": "mock_reviewed_ruleset",
    "label": "ENGINE_SUPPORTED",
    "nhr_reason": None,
    "direction": "LIABILITY",
    "derived_direction": True,
    "rate": "0.1055",
    "location_code": "1726",
    "expected_tax": {"value": "151.92", "currency": "USD"},
    "engine_amount": {"value": "151.92", "currency": "USD"},
    "rule_reference": "WA-RAS-001",
    "evidence_ids": ["ev_seat_export_1"],
    "unit_status": "READY_FOR_REVIEW",
}


class DeterminationRead(BaseModel):
    """Current determination as the purchase read already returns it.

    Money fields are ``{"value", "currency"}`` decimal strings. There is no
    net, savings, or recovered field.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    line_id: str | None
    jurisdiction: str
    facts_version: int
    ruleset_version: str
    label: str | None
    nhr_reason: str | None
    direction: str | None
    derived_direction: bool
    rate: str | None
    location_code: str | None
    expected_tax: Money | None
    engine_amount: Money | None
    rule_reference: str | None
    engine_treatment: str | None
    unit_status: str
    version: int
    error_code: str | None
    engine_request: dict[str, Any] | None = None
    engine_response: dict[str, Any] | None = None
