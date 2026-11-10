"""HMAC-SHA256 check for X-Ramp-Signature. Missing secret never counts as valid."""

import hashlib
import hmac
from typing import Any

from app.domain.moneyutil import parse_decimal


def signature_matches(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header:
        return False
    supplied = header.removeprefix("sha256=")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, supplied)


class RampSimulator:
    """Synthetic records only. Missing records fail; there is no network fallback."""

    def __init__(self) -> None:
        self.transactions: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.bills: dict[str, dict[str, Any]] = {}
        from app.demo.scenarios import get_scenario, scenario_ids

        for name in scenario_ids():
            scenario = get_scenario(name)
            assert scenario is not None
            self.transactions[scenario.event_id] = dict(
                entity_amount=scenario.entity_amount,
                currency_code=scenario.currency,
                merchant_name=scenario.vendor,
                invoice_number=scenario.invoice_number,
                invoice_date=scenario.invoice_date,
                receipts=[scenario.event_id],
            )

            self.receipts[scenario.event_id] = {
                "ocr": {
                    "taxes": (
                        [{"tax_amount": scenario.ocr_taxes}]
                        if scenario.ocr_taxes is not None
                        else []
                    )
                }
            }

    def fetch(
        self, event_type: str, object_id: str, receipt_id: str | None
    ) -> dict[str, str | None]:
        records = self.bills if event_type == "bills.created" else self.transactions
        record = records[object_id]
        receipt_ids = [receipt_id] if receipt_id else record.get("receipts", [])
        taxes = [
            tax["tax_amount"]
            for rid in receipt_ids
            for tax in self.receipts[rid]["ocr"].get("taxes", [])
        ]
        for tax in taxes:
            parse_decimal(tax)
        total = (
            taxes[0]
            if len(taxes) == 1
            else str(sum((parse_decimal(tax) for tax in taxes), parse_decimal("0")))
            if taxes
            else None
        )
        return dict(
            entity_amount=record["entity_amount"],
            currency_code=record["currency_code"],
            vendor=record.get("merchant_name", record.get("vendor", "")),
            invoice_number=record.get("invoice_number", ""),
            invoice_date=record.get(
                "invoice_date", record.get("user_transaction_time", "")[:10]
            ),
            ocr_taxes=total,
        )
