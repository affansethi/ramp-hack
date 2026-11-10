"""Refund-letter drafts copy Affan's filing-basis text (#51)."""

import re

from fastapi.testclient import TestClient

from tests.api.test_flow import ROOT, _headers, _run
from tests.api.test_flow import signed_client as signed_client  # noqa: F401


def _section(name: str) -> str:
    """The signed section as written, joined line by line, not rewritten."""
    lines: list[str] = []
    current = ""
    text = (ROOT / "rules" / "filing-basis.txt").read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == name and line and not line.startswith("#"):
            lines.append(line)
    return " ".join(lines)


def test_refund_letter_copies_the_signed_ca_basis_text(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case3", "basis-case3")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    refund = detail["determinations"][0]
    assert refund["direction"] == "REFUND_CANDIDATE"
    reviewed = signed_client.post(
        f"/api/v1/determinations/{refund['id']}/review",
        json={
            "action": "approve",
            "approval_path": "REFUND_PURSUIT",
            "reason": "Pursue the vendor amount",
            "expected_version": refund["version"],
        },
        headers=_headers("basis-approve"),
    )
    assert reviewed.status_code == 200, reviewed.text
    letter_id = reviewed.json()["determinations"][0]["refund_letter_id"]
    letter = signed_client.get(f"/api/v1/drafts/refund-letters/{letter_id}").json()
    expected = _section("ca_saas_vendor_tax")
    assert expected
    assert letter["basis_text"] == expected
    assert letter["basis_note"] == "This draft is not sent."
    assert letter["status"] == "DRAFT"
    lowered = letter["basis_text"].lower()
    assert "was sent" not in lowered
    for line in lowered.split("."):
        if re.search(r"\b(filed|paid)\b", line):
            assert "simulated" in line
