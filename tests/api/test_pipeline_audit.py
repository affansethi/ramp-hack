"""Each pipeline stage writes one audit event per input version."""

from sqlalchemy import func, select

from app.models import AuditEvent
from app.worker.poller import poll_once
from tests.api.test_flow import _run, signed_client  # noqa: F401
from tests.api.test_ramp_fetch import _record, _send


def test_ingest_retry_does_not_duplicate_stage_event(signed_client) -> None:
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


def test_advance_writes_hold_check_once(signed_client) -> None:
    _run(signed_client, "case1", "audit-case1")
    poll_once(signed_client.app.state.ctx)
    with signed_client.app.state.ctx.session_factory() as session:
        hold_checks = session.scalars(
            select(AuditEvent).where(AuditEvent.command == "hold_check")
        ).all()
        assert len(hold_checks) == 1
        extracts = session.scalars(
            select(AuditEvent).where(AuditEvent.command == "extract")
        ).all()
        assert len(extracts) == 1
