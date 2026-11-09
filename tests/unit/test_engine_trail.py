"""The stored engine request keeps ISO dates and the exact money strings."""

from datetime import date

from app.domain.evaluate import EngineLineRequest
from app.domain.pipeline import _request_json


def test_request_json_keeps_strings_and_iso_dates() -> None:
    request = EngineLineRequest(
        jurisdiction="WA",
        category="REMOTE_ACCESS_SAAS",
        invoice_date=date(2026, 11, 15),
        service_period_start=None,
        delivery_date=date(2026, 11, 2),
        line_amount="1440.00",
        tax_charged="0.00",
        shipping=None,
        location_name="Seattle, WA",
        state_count=1,
    )
    body = _request_json(request)
    assert body["invoice_date"] == "2026-11-15"
    assert body["service_period_start"] is None
    assert body["delivery_date"] == "2026-11-02"
    assert body["line_amount"] == "1440.00"
    assert body["tax_charged"] == "0.00"
    assert body["shipping"] is None
    assert set(body) == set(EngineLineRequest.__dataclass_fields__)
