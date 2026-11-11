"""RG-1-style gate: open work never changes an amount in GET /totals.

Held, needs-human-review, unreviewed, error, rejected and superseded units
carry counts only. Only an approved liability line (or an approved refund
pursuit row, kept separate) may change an amount. This test approves one
liability, snapshots every amount-bearing part of the totals, then adds one
unit of each open kind plus adversarial rows carrying junk engine amounts,
and requires every amount to be byte-identical.
"""

import json
import re
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.models import Determination, LiabilityLine
from app.seed import ORG_ID
from tests.api.conftest import assert_refund_candidates_stay_separate
from tests.api.test_flow import GOOD_ENV, ROOT, _headers, _run, login


def _signed_rules(dest: Path) -> None:
    dest.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        text = (ROOT / "rules" / name).read_text(encoding="utf-8")
        text = text.replace("review_status: candidate", "review_status: reviewed")
        text = text.replace("status: candidate", "status: signed")
        text = re.sub(r"^(\s*reviewed_by:)\s*$", r"\1 AF", text, flags=re.MULTILINE)
        text = re.sub(r"^(\s*signed_off_by:)\s*$", r"\1 AF", text, flags=re.MULTILINE)
        (dest / name).write_text(text, encoding="utf-8")


def _amounts(totals: dict[str, object]) -> str:
    liabilities = totals["approved_liabilities"]
    assert isinstance(liabilities, dict)
    candidates = totals["refund_candidates"]
    assert isinstance(candidates, dict)
    pre_close = totals["pre_close"]
    assert isinstance(pre_close, dict)
    rows = pre_close["jurisdictions"]
    assert isinstance(rows, list)
    return json.dumps(
        {
            "approved": liabilities["by_jurisdiction"],
            "refunds": {"total": candidates["total"], "rows": candidates["rows"]},
            "pre_close": [
                {
                    "jurisdiction": row["jurisdiction"],
                    "liabilities": row["approved_liabilities"],
                }
                for row in rows
            ],
        },
        sort_keys=True,
    )


def _totals(client: TestClient) -> dict[str, object]:
    response = client.get("/api/v1/totals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_open_work_never_changes_a_total_amount(tmp_path: Path) -> None:
    _signed_rules(tmp_path / "rules")
    database = tmp_path / "gate.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(tmp_path / "rules"),
        ramp_webhook_secret="test-secret",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        login(client)
        case1 = _run(client, "case1", "gate-case1")
        case1_id = case1["purchase_id"]
        assert isinstance(case1_id, str)
        detail = client.get(f"/api/v1/purchases/{case1_id}").json()
        determination = detail["determinations"][0]
        assert determination["direction"] == "LIABILITY"
        approved = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json={
                "action": "approve",
                "approval_path": "LIABILITY",
                "reason": "Reviewed",
                "expected_version": determination["version"],
            },
            headers=_headers("gate-approve"),
        )
        assert approved.status_code == 200, approved.text
        before_amounts = _amounts(_totals(client))
        # One unit of each open kind: unreviewed refund, NHR transition, hold.
        _run(client, "case3", "gate-case3")
        _run(client, "case4", "gate-case4")
        held = _run(client, "case6", "gate-case6")
        held_id = held["purchase_id"]
        assert isinstance(held_id, str)
        session = client.app.state.ctx.session_factory()
        try:
            junk = [
                ("DETERMINATION_ERROR", "NEEDS_HUMAN_REVIEW", None, "999.01"),
                ("NHR_ENGINE", "NEEDS_HUMAN_REVIEW", None, "999.02"),
                ("REJECTED", "ENGINE_SUPPORTED", "LIABILITY", "999.03"),
                ("SUPERSEDED", "ENGINE_SUPPORTED", "LIABILITY", "999.04"),
            ]
            for index, (status, label, direction, amount) in enumerate(junk):
                session.add(
                    Determination(
                        id=str(uuid4()),
                        org_id=ORG_ID,
                        purchase_id=case1_id,
                        line_id=None,
                        jurisdiction="WA",
                        facts_version=900 + index,
                        ruleset_version=f"gate-{index}",
                        label=label,
                        nhr_reason=None,
                        direction=direction,
                        derived_direction=False,
                        rate=None,
                        location_code=None,
                        expected_tax=None,
                        engine_amount=amount,
                        rule_reference=None,
                        engine_treatment=None,
                        unit_status=status,
                        version=1,
                        is_current=True,
                        error_code="GATE" if status == "DETERMINATION_ERROR" else None,
                    )
                )
            # An approved-looking liability on a held purchase must not leak.
            held_determination_id = str(uuid4())
            session.add(
                Determination(
                    id=held_determination_id,
                    org_id=ORG_ID,
                    purchase_id=held_id,
                    line_id=None,
                    jurisdiction="WA",
                    facts_version=950,
                    ruleset_version="gate-held",
                    label="ENGINE_SUPPORTED",
                    nhr_reason=None,
                    direction="LIABILITY",
                    derived_direction=True,
                    rate=None,
                    location_code=None,
                    expected_tax=None,
                    engine_amount="999.05",
                    rule_reference="WA-RAS-001",
                    engine_treatment=None,
                    unit_status="APPROVED",
                    version=1,
                    is_current=True,
                    error_code=None,
                )
            )
            session.add(
                LiabilityLine(
                    id=str(uuid4()),
                    determination_id=held_determination_id,
                    purchase_id=held_id,
                    jurisdiction="WA",
                    period="2026Q4",
                    amount="999.05",
                    approver_id="gate",
                    approved_at="2026-11-10T00:00:00Z",
                )
            )
            session.commit()
        finally:
            session.close()
        after = _totals(client)
        assert _amounts(after) == before_amounts
        raw = json.dumps(after)
        for junk_amount in ("999.01", "999.02", "999.03", "999.04", "999.05"):
            assert junk_amount not in raw
        by_direction = after["unreviewed_by_direction"]
        assert isinstance(by_direction, dict)
        assert set(by_direction) == {
            "LIABILITY",
            "REFUND_CANDIDATE",
            "CHARGED_CORRECTLY",
        }
        assert all(isinstance(count, int) for count in by_direction.values())
        assert "amount" not in json.dumps(by_direction)
        hold_rate = after["hold_rate"]
        assert isinstance(hold_rate, dict)
        percent = hold_rate["percent"]
        assert isinstance(percent, str)
        assert "amount" not in json.dumps(hold_rate)
        assert_refund_candidates_stay_separate(after)
