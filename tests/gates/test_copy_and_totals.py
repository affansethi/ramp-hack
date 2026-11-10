"""RG-1, RG-2, RG-3, RG-8, and the refund never-fail case.

Tests only. Production copy, rules, and domain code are not edited here.
The planted scan fixture is not a file under rules/.
"""

import inspect
import io
import json
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Float, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.clock import utcnow
from app.db import Base
from app.domain.commands import _document, _render_csv, export_workpaper
from app.models import (
    Determination,
    Hold,
    LiabilityLine,
    Purchase,
    PurchaseLine,
    RefundCandidate,
)
from app.seed import ENTITY_NAME, ORG_ID, PERSON_ID
from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _run

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED_AMOUNT = "404.04"
_PLANTED_CLAIM = "This recovered savings net is a planted fail."
_TEXT_SUFFIXES = {
    ".py",
    ".html",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".css",
    ".json",
    ".md",
    ".txt",
}
_HELD_FIELDS = {"count", "purchase_amount", "note", "label"}
_HELD_TEXT_KEYS = ("note", "label")
_ALLOWED_CLAIM = (
    re.compile(r"not recovered cash", re.IGNORECASE),
    re.compile(r"line_net", re.IGNORECASE),
    re.compile(r"line net", re.IGNORECASE),
)
_SAVINGS_OR_RECOVERED = re.compile(
    r"\b(?:savings|recovered)(?:_[A-Za-z0-9]+)?\b",
    re.IGNORECASE,
)
_NET_WORD = re.compile(r"(?<![\w.])net(?![\w])", re.IGNORECASE)
_COMBINED_NET = re.compile(
    r"liabilit(?:y|ies)\s*-\s*refunds?",
    re.IGNORECASE,
)
_BANNED_FIELD = re.compile(
    r"^(?:net|savings|recovered)(?:_[a-z0-9]+)?$",
    re.IGNORECASE,
)
_ALLOWED_FIELDS = {"line_net", "line_net_amount"}
_FILED_OR_PAID = re.compile(r"\b(?:filed|paid)\b", re.IGNORECASE)
_SIMULATED = re.compile(r"\bsimulated\b", re.IGNORECASE)
_BARE_ZERO = re.compile(r"^0+(?:\.0+)?$")
_DOCSTRING_SKIP = {
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.COMMENT,
    tokenize.ENCODING,
}


@dataclass(frozen=True)
class _Excluded:
    name: str
    unit_status: str
    is_current: bool
    label: str | None
    hold: bool
    direction: str | None


_EXCLUSIONS = (
    _Excluded("held", "READY_FOR_REVIEW", True, "ENGINE_SUPPORTED", True, "LIABILITY"),
    _Excluded("nhr", "NHR_ENGINE", True, "NEEDS_HUMAN_REVIEW", False, None),
    _Excluded(
        "unreviewed", "READY_FOR_REVIEW", True, "ENGINE_SUPPORTED", False, "LIABILITY"
    ),
    _Excluded("rejected", "REJECTED", True, "ENGINE_SUPPORTED", False, "LIABILITY"),
    _Excluded(
        "superseded", "SUPERSEDED", False, "ENGINE_SUPPORTED", False, "LIABILITY"
    ),
    _Excluded("error", "DETERMINATION_ERROR", True, None, False, None),
)


def _read_tree(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    if not path.is_dir():
        return ""
    parts: list[str] = []
    for child in sorted(path.rglob("*")):
        if not child.is_file() or "node_modules" in child.parts:
            continue
        if child.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        parts.append(child.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _is_docstring(tokens: list[tokenize.TokenInfo], index: int) -> bool:
    if tokens[index].type != tokenize.STRING:
        return False
    previous = index - 1
    while previous >= 0 and tokens[previous].type in _DOCSTRING_SKIP:
        previous -= 1
    if previous < 0:
        return True
    return tokens[previous].type == tokenize.OP and tokens[previous].string == ":"


def python_scan_text(source: str) -> str:
    """Code and string literals, without comments or docstrings."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return source
    parts: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == tokenize.COMMENT or _is_docstring(tokens, index):
            if "\n" in token.string:
                parts.append("\n")
            continue
        parts.append(token.string)
    return "".join(parts)


def claim_violations(text: str) -> list[str]:
    """Combined-net, savings, and recovered claims.

    Sourced invoice line-net facts and the phrase "not recovered cash" are
    not claims.
    """
    masked = text
    for pattern in _ALLOWED_CLAIM:
        masked = pattern.sub(" ", masked)
    found: list[str] = []
    for pattern in (_SAVINGS_OR_RECOVERED, _NET_WORD, _COMBINED_NET):
        found.extend(match.group(0) for match in pattern.finditer(masked))
    return found


def rg2_text_from_root(root: Path) -> str:
    parts: list[str] = []
    models = root / "app" / "models.py"
    if models.is_file():
        parts.append(python_scan_text(models.read_text(encoding="utf-8")))
    for relative in ("app/contracts", "app/api"):
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            parts.append(python_scan_text(path.read_text(encoding="utf-8")))
    web = root / "web"
    if web.is_dir():
        parts.append(_read_tree(web))
    return "\n".join(parts)


def field_name_violation(name: str) -> str | None:
    lowered = name.lower()
    if lowered in _ALLOWED_FIELDS or lowered.endswith("line_net"):
        return None
    if _BANNED_FIELD.fullmatch(lowered):
        return lowered
    return None


def iter_model_field_names() -> list[str]:
    import app.api.health as health
    import app.api.routes as routes
    import app.api.serialize as serialize
    import app.contracts.determination as determination
    import app.contracts.errors as errors
    import app.contracts.extraction as extraction
    import app.contracts.jobs as jobs
    import app.contracts.money as money

    names: list[str] = []
    modules = (
        health,
        routes,
        serialize,
        determination,
        errors,
        extraction,
        jobs,
        money,
        app.models,
    )
    for module in modules:
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value is not BaseModel
            ):
                names.extend(value.model_fields)
    for table in Base.metadata.tables.values():
        names.extend(column.name for column in table.columns)
    return names


def rg8_violations(text: str) -> list[str]:
    """'filed' or 'paid' on a line that does not also say simulated.

    The word draft does not satisfy the check.
    """
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _FILED_OR_PAID.search(line) and not _SIMULATED.search(line):
            found.append(f"{number}:{line.strip()}")
    return found


def rg8_corpus(root: Path, export_source: str) -> str:
    parts = [_read_tree(root / "app" / "static"), export_source]
    web = root / "web"
    if web.is_dir():
        parts.append(_read_tree(web))
    return "\n".join(parts)


def _is_bare_zero(value: str) -> bool:
    return _BARE_ZERO.match(value.strip()) is not None


def held_violations(held: object) -> list[str]:
    if not isinstance(held, dict):
        return ["held is not an object"]
    problems: list[str] = []
    texts = [held[key] for key in _HELD_TEXT_KEYS if key in held]
    if "purchase amount, not tax" not in texts:
        problems.append("missing purchase amount, not tax")
    if any(text != "purchase amount, not tax" for text in texts):
        problems.append("held label is not purchase amount, not tax")
    purchase = held.get("purchase_amount")
    if held.get("count") == 0:
        # Nothing held: no purchase amount, and never a bare zero.
        if purchase is not None:
            problems.append("purchase amount with nothing held")
    elif (
        not isinstance(purchase, dict)
        or purchase.get("currency") != "USD"
        or "value" not in purchase
    ):
        problems.append("missing purchase amount")
    for key in held:
        if key not in _HELD_FIELDS:
            problems.append(key)
    return problems


def _liability_groups(payload: dict[str, object]) -> tuple[object, list[object]]:
    raw = payload.get("approved_liabilities")
    if isinstance(raw, dict):
        groups = raw.get("by_jurisdiction") or []
        return raw.get("message"), list(groups) if isinstance(groups, list) else []
    message = payload.get("approved_liabilities_message")
    return message, list(raw) if isinstance(raw, list) else []


def _group_amount(group: object) -> str | None:
    if not isinstance(group, dict):
        return None
    for key in ("amount", "total"):
        amount = group.get(key)
        if isinstance(amount, dict) and amount.get("value") is not None:
            return str(amount["value"])
    return None


def liability_total_violations(payload: dict[str, object]) -> list[str]:
    message, groups = _liability_groups(payload)
    amounts = [amount for group in groups if (amount := _group_amount(group))]
    problems: list[str] = []
    if message == "No approved liabilities":
        if groups:
            problems.append("liability total alongside No approved liabilities")
        if any(_is_bare_zero(amount) for amount in amounts):
            problems.append("bare 0.00 liability total")
        return problems
    if not groups:
        problems.append("missing No approved liabilities")
    if any(_is_bare_zero(amount) for amount in amounts):
        problems.append("bare 0.00 liability total")
    return problems


def refund_total_violations(payload: dict[str, object]) -> list[str]:
    refund = payload.get("refund_candidates")
    if not isinstance(refund, dict):
        return ["missing refund candidates"]
    problems: list[str] = []
    if refund.get("label") != "not recovered cash":
        problems.append("refund candidates must be labeled not recovered cash")
    amount = _refund_amount(refund)
    if refund.get("count") == 0 and amount is not None:
        problems.append("refund total without an approved candidate")
    if isinstance(amount, dict) and _is_bare_zero(str(amount.get("value"))):
        problems.append("bare refund zero")
    return problems


def tax_money_values(payload: dict[str, object]) -> list[str]:
    _message, groups = _liability_groups(payload)
    values = [amount for group in groups if (amount := _group_amount(group))]
    refund = payload.get("refund_candidates")
    if isinstance(refund, dict):
        amount = _refund_amount(refund)
        if isinstance(amount, dict) and amount.get("value") is not None:
            values.append(str(amount["value"]))
    return values


def _refund_amount(refund: dict[str, object]) -> object:
    return refund.get("total", refund.get("amount"))


def _open_count(totals: dict[str, object], key: str) -> int:
    """Open NHR, error, and unreviewed counts from the GET /totals shape."""
    if key == "unreviewed":
        unreviewed = totals["unreviewed"]
        assert isinstance(unreviewed, dict)
        return sum(int(row["count"]) for row in unreviewed["by_direction"])
    block = totals[key]
    assert isinstance(block, dict)
    return int(block["count"])


def ledger_copies_engine(stored: str, engine: str) -> bool:
    return stored == engine


def approved_liability_section(export: str) -> str:
    start = export.find("approved liabilities")
    assert start != -1, export
    end = export.find("section,excluded", start)
    assert end != -1, export
    return export[start:end]


def _session(client: TestClient) -> Session:
    return client.app.state.ctx.session_factory()


def _totals(client: TestClient) -> dict[str, object]:
    response = client.get("/api/v1/totals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _approve_case1(client: TestClient) -> tuple[str, str, str]:
    created = _run(client, "case1", "gate-case1")
    detail = client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    engine = determination["engine_amount"]["value"]
    approved = client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers("gate-approve-case1"),
    )
    assert approved.status_code == 200, approved.text
    return str(detail["invoice_number"]), str(engine), str(created["purchase_id"])


def _wa_export(client: TestClient, key: str) -> str:
    created = client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": "WA", "period": "2026Q4"},
        headers=_headers(key),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "DRAFT"
    exported = client.get(
        f"/api/v1/workpapers/{body['id']}/export",
        params={"format": "csv"},
        headers=_headers(f"{key}-export"),
    )
    assert exported.status_code == 200, exported.text
    return exported.text


def _plant_excluded(client: TestClient, kind: _Excluded) -> str:
    purchase_id = str(uuid4())
    line_id = str(uuid4())
    determination_id = str(uuid4())
    with _session(client) as session:
        session.add(
            Purchase(
                id=purchase_id,
                org_id=ORG_ID,
                vendor="Synthetic Exclusion Co",
                invoice_number=f"SYN-EXCL-{kind.name}",
                invoice_date="2026-11-02",
                entity_name=ENTITY_NAME,
                currency="USD",
                item_status="HELD" if kind.hold else "DETERMINED",
                ramp_transaction_id=None,
                ramp_entity_amount=None,
                ramp_ocr_taxes=None,
                category="REMOTE_ACCESS_SAAS",
                demo_scenario=None,
                extraction_mode="replay",
                facts_version=1,
                bill_to="synthetic",
                created_at=utcnow(),
            )
        )
        session.flush()
        session.add(
            PurchaseLine(
                id=line_id,
                purchase_id=purchase_id,
                line_number=1,
                description="Excluded unit",
                quantity="1",
                unit_price="50.00",
                line_amount="0.00" if kind.hold else "50.00",
                service_period_start="2026-11-01",
                service_period_end="2026-11-30",
                delivery_date=None,
                location_name=None,
                shipping_amount=None,
            )
        )
        session.add(
            Determination(
                id=determination_id,
                org_id=ORG_ID,
                purchase_id=purchase_id,
                line_id=line_id,
                jurisdiction="WA",
                facts_version=1,
                ruleset_version="seed",
                label=kind.label,
                nhr_reason="ENGINE_UNSUPPORTED" if kind.name == "nhr" else None,
                direction=kind.direction,
                derived_direction=False,
                rate=None,
                location_code=None,
                expected_tax=None,
                engine_amount=EXCLUDED_AMOUNT,
                rule_reference=None,
                engine_treatment=None,
                unit_status=kind.unit_status,
                version=1,
                is_current=kind.is_current,
                error_code="MISSING_RATE_REFERENCE" if kind.name == "error" else None,
                engine_request=None,
                engine_response=None,
            )
        )
        session.flush()
        session.add(
            LiabilityLine(
                id=str(uuid4()),
                determination_id=determination_id,
                purchase_id=purchase_id,
                jurisdiction="WA",
                period="2026Q4",
                amount=EXCLUDED_AMOUNT,
                approver_id=PERSON_ID,
                approved_at=utcnow(),
            )
        )
        if kind.hold:
            session.add(
                Hold(
                    id=str(uuid4()),
                    purchase_id=purchase_id,
                    reason="MISSING_REQUIRED_FACT",
                    named_fact="user_location",
                    active=True,
                    releasing_fact_version=None,
                )
            )
        session.commit()
    return EXCLUDED_AMOUNT


def test_planted_bad_string_fails_the_scan(tmp_path: Path) -> None:
    planted = tmp_path / "fixture.txt"
    planted.write_text(_PLANTED_CLAIM + "\n", encoding="utf-8")
    violations = {item.lower() for item in claim_violations(planted.read_text())}
    assert {"recovered", "savings", "net"} <= violations
    production = rg2_text_from_root(ROOT)
    assert _PLANTED_CLAIM not in production
    assert claim_violations(production) == []
    rules = (ROOT / "rules" / "entries.yaml").read_text(encoding="utf-8")
    assert _PLANTED_CLAIM not in rules


@pytest.mark.parametrize(
    "text",
    [
        "net = liabilities - refunds",
        'recovered_total = "100.00"',
        'savings = "10.00"',
        '"recovered": "100.00"',
    ],
)
def test_rg2_rejects_prohibited_claims(text: str) -> None:
    violations = [item.lower() for item in claim_violations(text)]
    assert violations
    if "net = liabilities" in text:
        assert "net" in violations
        assert any("liabilit" in item for item in violations)
    if "recovered_total" in text:
        assert "recovered_total" in violations
    if text.startswith("savings"):
        assert "savings" in violations


@pytest.mark.parametrize(
    "text",
    [
        "not recovered cash",
        "line_net",
        "line net",
        "sourced invoice line net 1440.00",
        "Refund candidates, not recovered cash",
    ],
)
def test_rg2_allows_line_net_and_not_recovered_cash(text: str) -> None:
    assert claim_violations(text) == []


def test_rg2_scanner_visits_models_api_and_web(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "api").mkdir(parents=True)
    (app / "contracts").mkdir()
    (app / "models.py").write_text(
        '"""There is no net, savings, or recovered field."""\n'
        "net = liabilities - refunds\n",
        encoding="utf-8",
    )
    (app / "api" / "routes.py").write_text(
        'recovered_total = "100.00"\n',
        encoding="utf-8",
    )
    (app / "contracts" / "money.py").write_text(
        "savings = '1.00'\n",
        encoding="utf-8",
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "banner.tsx").write_text("recovered cash\n", encoding="utf-8")
    violations = {
        item.lower() for item in claim_violations(rg2_text_from_root(tmp_path))
    }
    assert {"net", "recovered_total", "savings", "recovered"} <= violations


def test_rg2_web_disclaimer_is_allowed(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "banner.tsx").write_text(
        "Refund candidates: not recovered cash\n",
        encoding="utf-8",
    )
    assert claim_violations(rg2_text_from_root(tmp_path)) == []


def test_rg2_production_models_and_api_have_no_claims() -> None:
    assert claim_violations(rg2_text_from_root(ROOT)) == []
    for name in iter_model_field_names():
        assert field_name_violation(name) is None, name
    assert field_name_violation("net") == "net"
    assert field_name_violation("savings") == "savings"
    assert field_name_violation("recovered_total") == "recovered_total"
    assert field_name_violation("line_net") is None


def test_rg3_schema_gate_still_rejects_float_columns() -> None:
    from tests.gates.test_schema import test_money_columns_are_not_floats

    test_money_columns_are_not_floats()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Float)


def test_rescaled_ledger_string_is_not_the_engine_amount() -> None:
    assert ledger_copies_engine("151.92", "151.92")
    assert not ledger_copies_engine("151.920", "151.92")


@pytest.mark.parametrize(
    "snippet",
    ["<p>Draft filed</p>", "<p>Draft paid</p>", "Draft filed", "Draft paid"],
)
def test_rg8_draft_does_not_excuse_filed_or_paid(snippet: str) -> None:
    assert rg8_violations(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "DRAFT / SIMULATED",
        "READY_TO_FILE_SIMULATED",
        "Synthetic data • simulated filing",
        "simulated filed return",
        "<p>Nothing was simulated.</p>",
    ],
)
def test_rg8_allows_copy_that_says_simulated(snippet: str) -> None:
    assert rg8_violations(snippet) == []


def test_rg8_simulated_on_another_line_does_not_excuse_paid() -> None:
    assert rg8_violations("simulated filing\npaid\n")


def test_rg8_scans_web_when_present(tmp_path: Path) -> None:
    static = tmp_path / "app" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<p>Synthetic demo data</p>\n", encoding="utf-8")
    web = tmp_path / "web"
    web.mkdir()
    (web / "page.html").write_text("<p>Draft filed</p>\n", encoding="utf-8")
    assert rg8_violations(rg8_corpus(tmp_path, "DRAFT / SIMULATED\n"))


def test_rg8_scans_the_entire_export_body(tmp_path: Path) -> None:
    static = tmp_path / "app" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<p>Synthetic demo data</p>\n", encoding="utf-8")
    export_source = ("x" * 3000) + "\n<p>Draft paid</p>\n"
    assert len(export_source) > 2500
    assert not (tmp_path / "web").exists()
    assert rg8_violations(rg8_corpus(tmp_path, export_source))


def test_rg8_placeholder_has_no_filed_or_paid_when_web_is_absent() -> None:
    assert not (ROOT / "web").exists()
    placeholder = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "filed" not in placeholder.lower()
    assert "paid" not in placeholder.lower()
    # export_workpaper delegates CSV and JSON bodies to these helpers.
    parts = [inspect.getsource(fn) for fn in (export_workpaper, _document, _render_csv)]
    export_source = "\n".join(parts)
    assert "def export_workpaper" in parts[0]
    assert parts[2].strip().endswith("return buffer.getvalue()")
    scanned = rg8_corpus(ROOT, export_source)
    assert placeholder in scanned
    assert export_source in scanned
    assert rg8_violations(scanned) == []
    assert rg8_violations(placeholder) == []


def test_tax_or_engine_amount_on_a_hold_fails() -> None:
    clean = {
        "count": 1,
        "purchase_amount": {"value": "0.00", "currency": "USD"},
        "note": "purchase amount, not tax",
    }
    assert held_violations(clean) == []
    for key in ("tax_amount", "engine_amount", "expected_tax"):
        planted = dict(clean)
        planted[key] = {"value": "0.00", "currency": "USD"}
        assert held_violations(planted)


def test_no_approved_liabilities_is_not_a_bare_zero(signed_client: TestClient) -> None:
    _run(signed_client, "case6", "gate-case6-message")
    totals = _totals(signed_client)
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    assert totals["approved_liabilities"]["by_jurisdiction"] == []
    assert liability_total_violations(totals) == []
    assert "0.00" not in "No approved liabilities"
    clean = {
        "approved_liabilities": [],
        "approved_liabilities_message": "No approved liabilities",
    }
    assert liability_total_violations(clean) == []
    alongside = {
        "approved_liabilities": [
            {
                "jurisdiction": "WA",
                "period": "2026Q4",
                "amount": {"value": "0.00", "currency": "USD"},
            }
        ],
        "approved_liabilities_message": "No approved liabilities",
    }
    assert liability_total_violations(alongside)
    bare = {
        "approved_liabilities": [
            {
                "jurisdiction": "WA",
                "period": "2026Q4",
                "amount": {"value": "0.00", "currency": "USD"},
            }
        ],
        "approved_liabilities_message": None,
    }
    assert liability_total_violations(bare)


def test_held_purchase_amount_of_zero_is_not_tax(signed_client: TestClient) -> None:
    created = _run(signed_client, "case6", "gate-case6-zero")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    assert detail["queue"] == "held"
    assert detail["determinations"] == []
    assert detail["purchase_amount_note"] == "purchase amount, not tax"
    assert "engine_amount" not in json.dumps(detail["holds"])
    with _session(signed_client) as session:
        lines = list(
            session.scalars(
                select(PurchaseLine).where(
                    PurchaseLine.purchase_id == created["purchase_id"]
                )
            ).all()
        )
        assert len(lines) == 1
        lines[0].line_amount = "0.00"
        session.commit()
    refreshed = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    assert refreshed["purchase_amount"] == {"value": "0.00", "currency": "USD"}
    assert refreshed["purchase_amount_note"] == "purchase amount, not tax"
    totals = _totals(signed_client)
    assert totals["held"]["count"] == 1
    assert totals["held"]["purchase_amount"] == {"value": "0.00", "currency": "USD"}
    assert totals["held"]["label"] == "purchase amount, not tax"
    assert held_violations(totals["held"]) == []
    assert liability_total_violations(totals) == []
    draft = signed_client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": "WA", "period": "2026Q4"},
        headers=_headers("gate-held-workpaper"),
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["approved_liabilities"] == []
    assert draft.json()["approved_liabilities_message"] == "No approved liabilities"
    assert "0.00" not in json.dumps(draft.json()["excluded"])


@pytest.mark.parametrize("kind", _EXCLUSIONS, ids=lambda kind: kind.name)
def test_rg1_excludes_unit_from_totals_and_workpaper(
    signed_client: TestClient, kind: _Excluded
) -> None:
    invoice, anchor, _purchase_id = _approve_case1(signed_client)
    excluded = _plant_excluded(signed_client, kind)
    assert excluded != anchor
    if kind.name == "nhr":
        nhr = _run(signed_client, "case4", "gate-case4")
        nhr_detail = signed_client.get(f"/api/v1/purchases/{nhr['purchase_id']}").json()
        assert nhr_detail["determinations"][0]["label"] == "NEEDS_HUMAN_REVIEW"
        assert nhr_detail["determinations"][0]["engine_amount"] is None
    if kind.name == "error":
        error = _run(signed_client, "case5", "gate-case5")
        error_detail = signed_client.get(
            f"/api/v1/purchases/{error['purchase_id']}"
        ).json()
        assert error_detail["determinations"][0]["unit_status"] == "DETERMINATION_ERROR"
        assert error_detail["determinations"][0]["engine_amount"] is None
    if kind.name == "rejected":
        rejected = _run(signed_client, "case2", "gate-case2")
        rejected_detail = signed_client.get(
            f"/api/v1/purchases/{rejected['purchase_id']}"
        ).json()
        determination = rejected_detail["determinations"][0]
        assert determination["engine_amount"] is None
        decision = signed_client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json={
                "action": "reject",
                "reason": "Not in the workpaper",
                "expected_version": determination["version"],
            },
            headers=_headers("gate-reject-case2"),
        )
        assert decision.status_code == 200, decision.text
    totals = _totals(signed_client)
    assert held_violations(totals["held"]) == []
    assert liability_total_violations(totals) == []
    assert refund_total_violations(totals) == []
    assert claim_violations(json.dumps(totals)) == []
    money = tax_money_values(totals)
    assert anchor in money
    assert excluded not in money
    if kind.name == "held":
        assert totals["held"]["count"] == 1
        assert totals["held"]["purchase_amount"]["value"] == "0.00"
    elif kind.name == "nhr":
        assert _open_count(totals, "open_needs_human_review") >= 1
    elif kind.name == "unreviewed":
        assert _open_count(totals, "unreviewed") >= 1
    elif kind.name == "error":
        assert _open_count(totals, "determination_errors") >= 1
    export = _wa_export(signed_client, f"gate-workpaper-{kind.name}")
    section = approved_liability_section(export)
    assert invoice in section
    assert anchor in section
    assert excluded not in section
    assert excluded not in export
    assert f"SYN-EXCL-{kind.name}" not in section
    if kind.name == "nhr":
        assert "SYN-1004" not in section
    if kind.name == "error":
        assert "SYN-1005" not in section
    if kind.name == "rejected":
        assert "SYN-1002" not in section
    assert rg8_violations(export) == []


def test_superseded_approval_leaves_totals_and_workpaper(
    signed_client: TestClient,
) -> None:
    _invoice, anchor, purchase_id = _approve_case1(signed_client)
    assert anchor in tax_money_values(_totals(signed_client))
    detail = signed_client.get(f"/api/v1/purchases/{purchase_id}").json()
    changed = signed_client.post(
        f"/api/v1/purchases/{purchase_id}/facts",
        json={
            "field": "vendor_name",
            "value": "Synthetic Cloud Co renamed",
            "source": "Named reviewer correction",
            "reason": "Correct the printed vendor",
            "expected_version": detail["facts_version"],
        },
        headers=_headers("gate-supersede-fact"),
    )
    assert changed.status_code == 200, changed.text
    assert poll_once(signed_client.app.state.ctx) == 1
    totals = _totals(signed_client)
    assert anchor not in tax_money_values(totals)
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    assert liability_total_violations(totals) == []
    draft = signed_client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": "WA", "period": "2026Q4"},
        headers=_headers("gate-superseded-workpaper"),
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["approved_liabilities"] == []
    assert draft.json()["approved_liabilities_message"] == "No approved liabilities"
    assert "0.00" not in json.dumps(draft.json()["excluded"])


def test_refund_candidate_never_reduces_a_liability_total(
    signed_client: TestClient,
) -> None:
    invoice, anchor, _purchase_id = _approve_case1(signed_client)
    before = _totals(signed_client)
    assert tax_money_values(before) == [anchor]
    created = _run(signed_client, "case3", "gate-case3")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    assert determination["direction"] == "REFUND_CANDIDATE"
    engine = determination["engine_amount"]["value"]
    unreviewed = _totals(signed_client)
    assert _open_count(unreviewed, "unreviewed") >= 1
    assert engine not in tax_money_values(unreviewed)
    assert unreviewed["approved_liabilities"] == before["approved_liabilities"]
    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "REFUND_PURSUIT",
            "reason": "Pursue the vendor amount",
            "expected_version": determination["version"],
        },
        headers=_headers("gate-approve-case3"),
    )
    assert approved.status_code == 200, approved.text
    after = _totals(signed_client)
    assert after["approved_liabilities"] == before["approved_liabilities"]
    assert after["refund_candidates"]["count"] == 1
    assert after["refund_candidates"]["label"] == "not recovered cash"
    assert after["refund_candidates"]["total"]["value"] == engine
    assert claim_violations(json.dumps(after)) == []
    export = _wa_export(signed_client, "gate-refund-workpaper")
    section = approved_liability_section(export)
    assert invoice in section
    assert anchor in section
    assert engine not in section
    assert rg8_violations(export) == []


def test_ledger_amount_string_equals_engine_amount(signed_client: TestClient) -> None:
    _approve_case1(signed_client)
    created = _run(signed_client, "case3", "gate-ledger-case3")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "REFUND_PURSUIT",
            "reason": "Pursue the vendor amount",
            "expected_version": determination["version"],
        },
        headers=_headers("gate-ledger-approve"),
    )
    assert approved.status_code == 200, approved.text
    with _session(signed_client) as session:
        liabilities = list(session.scalars(select(LiabilityLine)).all())
        refunds = list(session.scalars(select(RefundCandidate)).all())
        assert len(liabilities) == 1
        assert len(refunds) == 1
        for row in (*liabilities, *refunds):
            stored = session.get(Determination, row.determination_id)
            assert stored is not None
            assert stored.engine_amount is not None
            assert ledger_copies_engine(row.amount, stored.engine_amount)
