"""POST /questions/{id}/answers: only a sourced answer can move a held item."""

import pytest
from fastapi.testclient import TestClient

from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _run
from tests.api.test_flow import signed_client as signed_client  # noqa: F401


def _held_case6(client: TestClient) -> tuple[str, dict[str, object], int]:
    created = _run(client, "case6", "q-case6")
    purchase_id = str(created["purchase_id"])
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["queue"] == "held"
    return purchase_id, detail["questions"][0], detail["facts_version"]


def _answer(client: TestClient, question_id: object, key: str, **body: object):
    return client.post(
        f"/api/v1/questions/{question_id}/answers", json=body, headers=_headers(key)
    )


@pytest.mark.parametrize(
    ("body", "state"),
    [
        ({"answer": "I don't know"}, "dont_know"),
        ({"answer": "Seattle, WA"}, "unsourced"),
        ({"answer": "", "redirect_to": "IT lead"}, "redirected"),
    ],
)
def test_non_sourced_replies_keep_the_hold(
    signed_client: TestClient,  # noqa: F811
    body: dict[str, object],
    state: str,
) -> None:
    client = signed_client
    purchase_id, question, version = _held_case6(client)
    response = _answer(
        client, question["id"], f"a-{state}", expected_version=version, **body
    )
    assert response.status_code == 200, response.text
    poll_once(client.app.state.ctx)
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["queue"] == "held"
    assert detail["determinations"] == []
    assert detail["facts_version"] == version
    [stored] = detail["questions"]
    assert stored["state"] == state
    if state == "redirected":
        assert stored["recipient"] == "IT lead"


def test_sourced_answer_admits_the_fact_and_releases_the_hold(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    purchase_id, question, version = _held_case6(client)
    response = _answer(
        client,
        question["id"],
        "a-sourced",
        answer="Seattle, WA",
        evidence_reference="Seat export from the SSO admin, November 2026",
        expected_version=version,
    )
    assert response.status_code == 200, response.text
    assert response.json()["questions"][0]["state"] == "answered"
    poll_once(client.app.state.ctx)
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["facts_version"] == version + 1
    assert [h for h in detail["holds"] if h["active"]] == []
    location = [
        f
        for f in detail["facts"]
        if f["field"] == "user_location" and f["origin"] == "HUMAN_SUPPLIED"
    ]
    assert location and location[0]["confirmation"] == "CONFIRMED"
    assert detail["determinations"][0]["label"] == "ENGINE_SUPPORTED"
    # Answering twice is refused.
    again = _answer(
        client,
        question["id"],
        "a-again",
        answer="x",
        evidence_reference="y",
        expected_version=detail["facts_version"],
    )
    assert again.status_code == 409


def test_stale_version_and_unknown_question_are_refused(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _purchase_id, question, version = _held_case6(client)
    stale = _answer(
        client,
        question["id"],
        "a-stale",
        answer="I don't know",
        expected_version=version - 1,
    )
    assert stale.status_code == 409
    missing = _answer(
        client, "no-such-question", "a-missing", answer="x", expected_version=1
    )
    assert missing.status_code == 404
