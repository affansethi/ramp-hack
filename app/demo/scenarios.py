"""Synthetic invoice inputs for the seven demo cases. Not expected tax results."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class LineInput:
    description: str
    line_amount: str
    tax_charged: str | None
    tax_jurisdiction: str | None
    service_start: str | None
    service_end: str | None
    delivery_date: str | None
    shipping: str | None
    location_name: str | None
    location_state: str | None
    location_rung: int | None
    seats: int | None
    seats_covered: int | None
    named_single_office: bool = False
    low_confidence: bool = False
    recorded_confirmation: bool = False
    quantity: str = "1"


@dataclass(frozen=True)
class Scenario:
    id: str
    event_id: str
    vendor: str
    invoice_number: str
    invoice_date: str
    entity_amount: str
    ocr_taxes: str | None
    category: str
    bill_to: str
    lines: tuple[LineInput, ...]
    currency: str = "USD"


def _saas_line(
    *,
    description: str,
    line_amount: str,
    tax_charged: str | None,
    tax_jurisdiction: str | None,
    service_start: str,
    service_end: str,
    location_name: str | None,
    location_state: str | None,
    seats: int | None,
    seats_covered: int | None,
    rung: int | None,
) -> LineInput:
    return LineInput(
        description=description,
        line_amount=line_amount,
        tax_charged=tax_charged,
        tax_jurisdiction=tax_jurisdiction,
        service_start=service_start,
        service_end=service_end,
        delivery_date=None,
        shipping=None,
        location_name=location_name,
        location_state=location_state,
        location_rung=rung,
        seats=seats,
        seats_covered=seats_covered,
    )


_CASES: dict[str, Scenario] = {
    "case1": Scenario(
        id="case1",
        event_id="evt_case1",
        vendor="Synthetic Cloud Co",
        invoice_number="SYN-1001",
        invoice_date="2026-11-15",
        entity_amount="1440.00",
        ocr_taxes="0.00",
        category="REMOTE_ACCESS_SAAS",
        bill_to="Seattle bill-to, synthetic",
        lines=(
            _saas_line(
                description="Remote-access seats, November",
                line_amount="1440.00",
                tax_charged="0.00",
                tax_jurisdiction=None,
                service_start="2026-11-01",
                service_end="2026-11-30",
                location_name="Seattle, WA",
                location_state="WA",
                seats=10,
                seats_covered=10,
                rung=1,
            ),
        ),
    ),
    "case2": Scenario(
        id="case2",
        event_id="evt_case2",
        vendor="Synthetic Cloud Co",
        invoice_number="SYN-1002",
        invoice_date="2026-11-15",
        entity_amount="1440.00",
        ocr_taxes="0.00",
        category="REMOTE_ACCESS_SAAS",
        bill_to="San Francisco bill-to, synthetic",
        lines=(
            _saas_line(
                description="Remote-access seats, November",
                line_amount="1440.00",
                tax_charged="0.00",
                tax_jurisdiction=None,
                service_start="2026-11-01",
                service_end="2026-11-30",
                location_name="San Francisco, CA",
                location_state="CA",
                seats=8,
                seats_covered=8,
                rung=1,
            ),
        ),
    ),
    "case3": Scenario(
        id="case3",
        event_id="evt_case3",
        vendor="Synthetic Cloud Co",
        invoice_number="SYN-1003",
        invoice_date="2026-12-15",
        entity_amount="962.32",
        ocr_taxes="83.00",
        category="REMOTE_ACCESS_SAAS",
        bill_to="San Francisco bill-to, synthetic",
        lines=(
            _saas_line(
                description="Remote-access seats, December",
                line_amount="962.32",
                tax_charged="83.00",
                tax_jurisdiction="CA",
                service_start="2026-12-01",
                service_end="2026-12-31",
                location_name="San Francisco, CA",
                location_state="CA",
                seats=4,
                seats_covered=4,
                rung=1,
            ),
        ),
    ),
    "case4": Scenario(
        id="case4",
        event_id="evt_case4",
        vendor="Synthetic Cloud Co",
        invoice_number="SYN-1004",
        invoice_date="2026-12-15",
        entity_amount="962.32",
        ocr_taxes="83.00",
        category="REMOTE_ACCESS_SAAS",
        bill_to="San Francisco bill-to, synthetic",
        lines=(
            _saas_line(
                description="Remote-access seats, January",
                line_amount="962.32",
                tax_charged="83.00",
                tax_jurisdiction="CA",
                service_start="2027-01-01",
                service_end="2027-01-31",
                location_name="San Francisco, CA",
                location_state="CA",
                seats=4,
                seats_covered=4,
                rung=1,
            ),
        ),
    ),
    "case5": Scenario(
        id="case5",
        event_id="evt_case5",
        vendor="Synthetic Hardware Supply",
        invoice_number="SYN-1005",
        invoice_date="2026-11-20",
        entity_amount="2100.00",
        ocr_taxes="0.00",
        category="COMPUTER_HARDWARE",
        bill_to="HQ bill-to, synthetic",
        lines=(
            LineInput(
                description="Laptop delivered in New York City",
                line_amount="2100.00",
                tax_charged="0.00",
                tax_jurisdiction=None,
                service_start=None,
                service_end=None,
                delivery_date="2026-11-20",
                shipping="0.00",
                location_name="New York City, NY",
                location_state="NY",
                location_rung=None,
                seats=None,
                seats_covered=None,
            ),
        ),
    ),
    "case6": Scenario(
        id="case6",
        event_id="evt_case6",
        vendor="Synthetic Cloud Co",
        invoice_number="SYN-1006",
        invoice_date="2026-11-18",
        entity_amount="2000.00",
        ocr_taxes=None,
        category="REMOTE_ACCESS_SAAS",
        bill_to="HQ bill-to only, synthetic",
        lines=(
            _saas_line(
                description="Remote-access seats, location missing",
                line_amount="2000.00",
                tax_charged="0.00",
                tax_jurisdiction=None,
                service_start="2026-11-01",
                service_end="2026-11-30",
                location_name=None,
                location_state=None,
                seats=12,
                seats_covered=None,
                rung=None,
            ),
        ),
    ),
}


def get_scenario(scenario_id: str) -> Scenario | None:
    if scenario_id == "case7":
        return replace(_CASES["case1"], id="case7")
    return _CASES.get(scenario_id)


def scenario_ids() -> list[str]:
    return [*_CASES.keys(), "case7"]
