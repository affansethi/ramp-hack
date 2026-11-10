"""Each pipeline stage writes one audit event per input version."""

import enum
import json
from hashlib import sha256

from sqlalchemy import func, select

import app.contracts.enums as contract_enums
from app.domain.pipeline import _stage_audit, advance_purchase
from app.domain.ruleset import load_ruleset
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
            select(AuditEvent).where(AuditEvent.command == "hold_check")
        ).all()
        assert len(hold_checks) == 1
        extracts = session.scalars(
            select(AuditEvent).where(AuditEvent.command == "extract")
        ).all()
        assert len(extracts) == 1


def test_each_purchase_stage_writes_one_event_per_facts_version(
    signed_client,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "audit-stages")
    purchase_id = str(created["purchase_id"])
    poll_once(signed_client.app.state.ctx)
    expected = [
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
    with signed_client.app.state.ctx.session_factory() as session:
        purchase_version = _purchase_version(session, purchase_id)
        for command in expected:
            rows = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.object_id == purchase_id,
                    AuditEvent.command == command,
                    AuditEvent.object_version == purchase_version,
                )
            ).all()
            assert len(rows) == 1, command
            row = rows[0]
            assert row.object_type == "purchase"
            identity = {
                "object_type": row.object_type,
                "object_id": row.object_id,
                "object_version": row.object_version,
            }
            assert (
                row.after_hash
                == sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
        # A retry for the same input version writes no second event.
        _stage_audit(session, row.org_id, "extract", purchase_id, purchase_version)
        session.flush()
        count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.object_id == purchase_id,
                AuditEvent.command == "extract",
                AuditEvent.object_version == purchase_version,
            )
        )
        assert count == 1
        # Re-running the worker for the same version adds no duplicate.
        ruleset = load_ruleset(signed_client.app.state.ctx.rules_dir)
        advance_purchase(session, purchase_id, ruleset)
        session.flush()
        for command in expected:
            count = session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.object_id == purchase_id,
                    AuditEvent.command == command,
                    AuditEvent.object_version == purchase_version,
                )
            )
            assert count == 1, command


def test_stage_uniqueness_index_and_no_new_enum(signed_client) -> None:  # noqa: F811
    with signed_client.app.state.ctx.session_factory() as session:
        from sqlalchemy import inspect as sa_inspect

        names = {
            index["name"]
            for index in sa_inspect(session.get_bind()).get_indexes("audit_events")
        }
        assert "uq_audit_stage" in names
    stage_names = {
        "ingest",
        "fetch",
        "deduplicate",
        "extract",
        "cross_check",
        "admit_facts",
        "classify",
        "hold_check",
        "jurisdiction_set",
        "engine_call",
        "response_check",
    }
    for name in dir(contract_enums):
        member = getattr(contract_enums, name)
        if isinstance(member, enum.EnumMeta):
            values = {item.value for item in member}
            assert not (values & stage_names), name


def _purchase_version(session, purchase_id: str) -> int:
    from app.models import Purchase

    purchase = session.get(Purchase, purchase_id)
    assert purchase is not None
    return purchase.facts_version
