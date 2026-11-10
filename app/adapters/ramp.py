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
    ) -> dict[str, Any]:
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
        merchant = record.get("merchant_location") or {}
        holder = record.get("card_holder") or {}
        receipt_items = (record.get("merchant_data") or {}).get("receipt") or []
        ocr_lines = [
            line
            for rid in receipt_ids
            for line in self.receipts.get(rid, {}).get("ocr", {}).get("line_items", [])
        ]
        mismatch = (
            bool(merchant)
            and bool(holder.get("location_name"))
            and (
                str(merchant.get("state") or "")
                != str(holder.get("location_name") or "").split(",")[-1].strip()
            )
        )
        return dict(
            entity_amount=record["entity_amount"],
            currency_code=record["currency_code"],
            vendor=record.get("merchant_name", record.get("vendor", "")),
            invoice_number=record.get("invoice_number", ""),
            invoice_date=_invoice_date(record),
            ocr_taxes=total,
            receipt_items=receipt_items,
            ocr_line_items=ocr_lines,
            card_holder=holder.get("location_name"),
            merchant_location_mismatch=mismatch,
            bill_to=record.get("bill_to"),
        )


def _invoice_date(record: dict[str, Any]) -> str:
    """Prefer the bill's invoice_date; else the card time's date part.

    A null or non-string time gives "" instead of raising, so a later stage
    sees a missing date rather than the fetch job failing.
    """
    invoice_date = record.get("invoice_date")
    if isinstance(invoice_date, str) and invoice_date:
        return invoice_date
    stamp = record.get("user_transaction_time")
    return stamp[:10] if isinstance(stamp, str) else ""
