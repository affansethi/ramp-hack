"""Each pipeline stage writes one audit event per input version."""

from sqlalchemy import func, select

from app.models import AuditEvent
from app.worker.poller import poll_once
from tests.api.test_flow import _run, signed_client  # noqa: F401
from tests.api.test_ramp_fetch import _record, _send


def test_ingest_retry_does_not_duplicate_stage_event(
    signed_client,  # noqa: F811
) -> None:
    client = signed_client
    _record(client)
    first = _send(client, "evt-audit", "transactions.cleared", "synthetic-id")
    second = _send(client, "evt-audit", "transactions.cleared", "synthetic-id")
    assert first.json() == second.json()
    with client.app.state.ctx.session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.command == "ingest")
        )
        assert count == 1


def test_advance_writes_hold_check_once(signed_client) -> None:  # noqa: F811
    _run(signed_client, "case1", "audit-case1")
    poll_once(signed_client.app.state.ctx)
    with signed_client.app.state.ctx.session_factory() as session:
        hold_checks = session.scalars(
            select(AuditEvent).where(AuditEvent.command.startswith("hold_check@"))
        ).all()
        assert len(hold_checks) == 1
        extracts = session.scalars(
            select(AuditEvent).where(AuditEvent.command == "extract")
        ).all()
        assert len(extracts) == 1


_CASE1_STAGES = [
    "deduplicate",
    "extract",
    "admit_facts",
    "cross_check",
    "classify",
    "hold_check",
    "jurisdiction_set",
    "engine_call",
    "response_check",
]


def _purchase_commands(client, purchase_id: str) -> list[str]:  # noqa: ANN001
    events = client.get(
        "/api/v1/audit-events", params={"object_id": purchase_id}
    ).json()["audit_events"]
    return [event["command"].split("@")[0] for event in events]


def test_case1_writes_every_v2_stage_in_order(signed_client) -> None:  # noqa: F811
    created = _run(signed_client, "case1", "stages-case1")
    commands = _purchase_commands(signed_client, created["purchase_id"])
    assert commands == _CASE1_STAGES
    with signed_client.app.state.ctx.session_factory() as session:
        ingest = session.scalars(
            select(AuditEvent).where(AuditEvent.command.in_(("ingest", "fetch")))
        ).all()
        assert ingest
        assert {row.object_type for row in ingest} == {"source_event"}


def test_ruleset_stages_carry_the_ruleset_version(signed_client) -> None:  # noqa: F811
    created = _run(signed_client, "case1", "stages-ruleset")
    from app.domain.ruleset import load_ruleset

    version = load_ruleset(signed_client.app.state.ctx.rules_dir).version
    events = signed_client.get(
        "/api/v1/audit-events", params={"object_id": created["purchase_id"]}
    ).json()["audit_events"]
    keyed = {event["command"] for event in events if "@" in event["command"]}
    assert keyed == {
        f"{stage}@{version}"
        for stage in (
            "classify",
            "hold_check",
            "jurisdiction_set",
            "engine_call",
            "response_check",
        )
    }


def test_new_ruleset_version_is_a_new_input_not_a_retry(
    signed_client,  # noqa: F811
) -> None:
    from dataclasses import replace

    from app.domain.pipeline import advance_purchase
    from app.domain.ruleset import load_ruleset

    created = _run(signed_client, "case1", "stages-new-ruleset")
    ctx = signed_client.app.state.ctx
    current = load_ruleset(ctx.rules_dir)
    changed = replace(current, version="0000000000000000")
    with ctx.session_factory() as session:
        advance_purchase(session, created["purchase_id"], changed)
        advance_purchase(session, created["purchase_id"], changed)
        session.commit()
    commands = [
        event["command"]
        for event in signed_client.get(
            "/api/v1/audit-events", params={"object_id": created["purchase_id"]}
        ).json()["audit_events"]
    ]
    assert commands.count(f"engine_call@{current.version}") == 1
    assert commands.count("engine_call@0000000000000000") == 1


def test_approval_writes_an_outputs_stage_once(signed_client) -> None:  # noqa: F811
    from tests.api.test_flow import _headers

    created = _run(signed_client, "case1", "stages-outputs")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers("stages-approve"),
    )
    assert approved.status_code == 200, approved.text
    events = signed_client.get(
        "/api/v1/audit-events", params={"object_id": determination["id"]}
    ).json()["audit_events"]
    outputs = [event for event in events if event["command"] == "outputs:liability"]
    assert len(outputs) == 1
    assert outputs[0]["actor_id"] == "system"


def test_database_refuses_a_second_system_stage_row(
    signed_client,  # noqa: F811
) -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    created = _run(signed_client, "case1", "stages-unique")
    ctx = signed_client.app.state.ctx
    with ctx.session_factory() as session:
        row = session.scalars(
            select(AuditEvent).where(
                AuditEvent.object_id == created["purchase_id"],
                AuditEvent.command == "extract",
            )
        ).one()
        session.add(
            AuditEvent(
                id="00000000-0000-4000-8000-00000000dupe",
                org_id=row.org_id,
                actor_id="system",
                command=row.command,
                object_type=row.object_type,
                object_id=row.object_id,
                object_version=row.object_version,
                before_hash=row.after_hash,
                after_hash=row.after_hash,
                created_at=row.created_at,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_dedupe_writer_skips_a_row_another_session_already_wrote(
    signed_client,  # noqa: F811
) -> None:
    from app.domain.commands import _audit

    created = _run(signed_client, "case1", "stages-race")
    ctx = signed_client.app.state.ctx
    purchase_id = created["purchase_id"]
    with ctx.session_factory() as first, ctx.session_factory() as second:
        org_id = first.scalars(
            select(AuditEvent.org_id).where(AuditEvent.object_id == purchase_id)
        ).first()
        assert org_id is not None
        _audit(first, org_id, "system", "race", "purchase", purchase_id, 9, dedupe=True)
        first.commit()
        _audit(
            second, org_id, "system", "race", "purchase", purchase_id, 9, dedupe=True
        )
        second.commit()
    with ctx.session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.command == "race")
        )
        assert count == 1
