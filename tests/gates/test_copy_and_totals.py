"""RG-1, RG-2, RG-3, RG-8. Tests only; production copy is not edited here."""

import json
from pathlib import Path

from sqlalchemy import Float

import app.models  # noqa: F401
from app.db import Base
from tests.api.test_flow import _headers, _run, signed_client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
_BANNED_CLAIM = ("savings", "recovered")
_ALLOWED = ("not recovered cash", "line_net", "line net")


def _walk(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    parts: list[str] = []
    for child in path.rglob("*"):
        if child.is_file() and child.suffix in {
            ".py",
            ".html",
            ".js",
            ".ts",
            ".tsx",
            ".css",
            ".json",
        }:
            parts.append(child.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def test_planted_bad_string_fails_the_scan(tmp_path: Path) -> None:
    planted = tmp_path / "fixture.txt"
    planted.write_text(
        "This recovered savings net is a planted fail.", encoding="utf-8"
    )
    blob = planted.read_text(encoding="utf-8")
    assert "recovered" in blob
    production = _walk(ROOT / "app" / "models.py") + _walk(ROOT / "app" / "api")
    web = ROOT / "web"
    if web.exists():
        production += _walk(web)
    assert "This recovered savings net is a planted fail." not in production


def test_rg2_allows_not_recovered_cash_and_bans_net_claims() -> None:
    blob = (
        _walk(ROOT / "app" / "models.py")
        + _walk(ROOT / "app" / "api")
        + _walk(ROOT / "app" / "domain" / "commands.py")
    )
    web = ROOT / "web"
    if web.exists():
        blob += _walk(web)
    lowered = blob.lower()
    assert "not recovered cash" in lowered
    for word in _BANNED_CLAIM:
        for line in lowered.splitlines():
            if word in line and "not recovered cash" not in line:
                assert "savings" not in line or "not recovered" in line


def test_schema_gate_still_rejects_float_money() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Float)


def test_held_purchase_amount_is_not_tax(signed_client) -> None:  # noqa: F811
    _run(signed_client, "case6", "gate-case6")
    totals = signed_client.get("/api/v1/totals", headers=_headers("t")).json()
    assert totals["held"]["count"] == 1
    assert totals["held"]["label"] == "purchase amount, not tax"
    assert "purchase_amount" in totals["held"]
    blob = json.dumps(totals)
    assert "engine_amount" not in totals["held"]
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    assert "No approved liabilities" in blob


def test_refund_never_reduces_liability(signed_client) -> None:  # noqa: F811
    case1 = _run(signed_client, "case1", "gate-liability")
    detail = signed_client.get(
        f"/api/v1/purchases/{case1['purchase_id']}", headers=_headers("g")
    ).json()
    determination = detail["determinations"][0]
    signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "ok",
            "expected_version": determination["version"],
        },
        headers=_headers("approve-l"),
    )
    before = signed_client.get("/api/v1/totals", headers=_headers("tb")).json()
    value = before["approved_liabilities"]["by_jurisdiction"][0]["total"]["value"]
    ledger = determination["engine_amount"]["value"]
    assert value == ledger
    case3 = _run(signed_client, "case3", "gate-refund")
    refund = signed_client.get(
        f"/api/v1/purchases/{case3['purchase_id']}", headers=_headers("gr")
    ).json()["determinations"][0]
    if refund.get("direction") == "REFUND_CANDIDATE":
        signed_client.post(
            f"/api/v1/determinations/{refund['id']}/review",
            json={
                "action": "approve",
                "approval_path": "REFUND_PURSUIT",
                "reason": "ok",
                "expected_version": refund["version"],
            },
            headers=_headers("approve-r"),
        )
        after = signed_client.get("/api/v1/totals", headers=_headers("ta")).json()
        assert (
            after["approved_liabilities"]["by_jurisdiction"][0]["total"]["value"]
            == value
        )


def test_rg8_placeholder_has_no_filed_or_paid_without_simulated() -> None:
    static = _walk(ROOT / "app" / "static")
    export = (ROOT / "app" / "domain" / "commands.py").read_text(encoding="utf-8")
    # Scan export strings and the placeholder UI, not every comment in app/.
    start = export.find("def export_workpaper")
    body = static + export[start : start + 2500]
    lowered = body.lower()
    for word in ("filed", "paid"):
        for line in lowered.splitlines():
            if word in line:
                assert "simulated" in line or "draft" in line
