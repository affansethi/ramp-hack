"""SQLite-only durable claims, fenced attempts, and transactional completion."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import UniqueConstraint, select

from app.clock import utcnow
from app.config import load_settings
from app.context import AppContext, build_context
from app.demo.scenarios import get_scenario, scenario_ids
from app.domain.pipeline import ingest_scenario
from app.models import Determination, Job, ReviewDecision
from app.seed import ORG_ID
from app.worker import poller


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    return build_context(
        replace(load_settings(), database_url=f"sqlite:///{tmp_path / 'worker.db'}")
    )


def enqueue(ctx: AppContext) -> str:
    scenario = get_scenario(scenario_ids()[0])
    assert scenario is not None
    # An unsupported category produces a real, amount-free determination
    # without editing owner-authored rules, signoffs, or expected results.
    with ctx.session_factory() as session:
        result = ingest_scenario(
            session, replace(scenario, category="NO_SUPPORTED_CATEGORY"), ORG_ID
        )
        session.commit()
        assert result.job_id is not None
        return result.job_id


def read_job(ctx: AppContext, job_id: str) -> Job:
    with ctx.session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def determination_ids(ctx: AppContext) -> list[str]:
    with ctx.session_factory() as session:
        return list(session.scalars(select(Determination.id)))


def test_two_independent_pollers_claim_once(ctx: AppContext, monkeypatch):
    job_id = enqueue(ctx)
    start = Barrier(2)
    calls = []
    advance = poller.advance_purchase

    def tracked(*args):
        calls.append(args[1])
        advance(*args)

    monkeypatch.setattr(poller, "advance_purchase", tracked)

    def run():
        start.wait(timeout=5)
        # Bypass the process-local thread lock: the SQL claim must stand alone.
        return poller._poll(ctx)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: run(), range(2)))
    assert sorted(results) == [0, 1]
    assert len(calls) == 1
    job = read_job(ctx, job_id)
    assert (job.status, job.attempts, job.lease_until) == ("SUCCEEDED", 1, None)
    assert poller.poll_once(ctx) == 0
    with ctx.session_factory() as session:
        assert not list(session.scalars(select(ReviewDecision)))


def test_claim_commits_before_work_and_recovers_after_crash(
    ctx: AppContext, monkeypatch
):
    job_id = enqueue(ctx)
    advance = poller.advance_purchase

    class Crash(BaseException):
        pass

    def crash(session, purchase_id, ruleset):
        observed = read_job(ctx, job_id)
        assert observed.status == "RUNNING"
        assert observed.attempts == 1
        assert observed.lease_until > utcnow()
        advance(session, purchase_id, ruleset)
        session.flush()
        assert session.scalar(select(Determination.id)) is not None
        assert determination_ids(ctx) == []
        raise Crash

    monkeypatch.setattr(poller, "advance_purchase", crash)
    with pytest.raises(Crash):
        poller.poll_once(ctx)
    assert determination_ids(ctx) == []
    assert read_job(ctx, job_id).status == "RUNNING"
    assert poller.poll_once(ctx) == 0
    with ctx.session_factory() as session:
        job = session.get(Job, job_id)
        job.lease_until = "2000-01-01T00:00:00Z"
        session.commit()
    monkeypatch.setattr(poller, "advance_purchase", advance)
    assert poller.poll_once(ctx) == 1
    job = read_job(ctx, job_id)
    assert (job.status, job.attempts) == ("SUCCEEDED", 2)
    original = determination_ids(ctx)
    assert original
    # A second job effect for the same immutable inputs is idempotent too.
    with ctx.session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "QUEUED"
        session.commit()
    assert poller.poll_once(ctx) == 1
    assert determination_ids(ctx) == original


def test_failure_rolls_back_results_and_counts_once(ctx: AppContext, monkeypatch):
    job_id = enqueue(ctx)
    advance = poller.advance_purchase

    def fail(session, purchase_id, ruleset):
        advance(session, purchase_id, ruleset)
        session.flush()
        raise RuntimeError("synthetic failure after result write")

    monkeypatch.setattr(poller, "advance_purchase", fail)
    for attempt, status in [(1, "QUEUED"), (2, "QUEUED"), (3, "FAILED")]:
        assert poller.poll_once(ctx) == 0
        job = read_job(ctx, job_id)
        assert (job.attempts, job.status) == (attempt, status)
        assert job.error_code == "ADVANCE_FAILED"
        assert job.lease_until is None
        assert determination_ids(ctx) == []
    assert poller.poll_once(ctx) == 0
    assert read_job(ctx, job_id).attempts == 3


def test_kind_dispatch_and_unknown_kind(ctx: AppContext, monkeypatch):
    job_id = enqueue(ctx)
    with ctx.session_factory() as session:
        job = session.get(Job, job_id)
        job.kind = "unknown"
        job.purchase_id = None
        session.commit()
    assert poller.poll_once(ctx) == 0
    job = read_job(ctx, job_id)
    assert (job.status, job.attempts, job.error_code) == (
        "QUEUED",
        1,
        "UNKNOWN_JOB_KIND",
    )
    assert determination_ids(ctx) == []
    handled = []
    monkeypatch.setitem(
        poller._HANDLERS, "unknown", lambda ctx, session, job: handled.append(job.id)
    )
    assert poller.poll_once(ctx) == 1
    assert handled == [job_id]
    assert read_job(ctx, job_id).error_code is None


def test_stale_failure_cannot_change_new_claim(ctx: AppContext):
    job_id = enqueue(ctx)
    with ctx.session_factory() as session:
        first = poller._claim(session)
        assert first is not None
    with ctx.session_factory() as session:
        job = session.get(Job, job_id)
        job.lease_until = "2000-01-01T00:00:00Z"
        session.commit()
    with ctx.session_factory() as session:
        second = poller._claim(session)
        assert second is not None
    poller._note_failure(
        ctx, first.id, first.attempts, first.lease_until, error_code="ADVANCE_FAILED"
    )
    job = read_job(ctx, job_id)
    assert (job.status, job.attempts) == ("RUNNING", 2)
    assert job.error_code is None


def test_determination_identity_has_database_unique_key(ctx: AppContext):
    keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in Determination.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "org_id",
        "line_id",
        "jurisdiction",
        "facts_version",
        "ruleset_version",
    ) in keys


def test_stale_worker_does_not_run_handler(ctx: AppContext, monkeypatch):
    job_id = enqueue(ctx)
    claim = poller._claim

    def reclaimed(session):
        first = claim(session)
        with ctx.session_factory() as other:
            job = other.get(Job, job_id)
            job.lease_until = "2000-01-01T00:00:00Z"
            other.commit()
            assert claim(other) is not None
        return first

    def unexpected(*args):
        pytest.fail("stale worker ran the handler")

    monkeypatch.setattr(poller, "_claim", reclaimed)
    monkeypatch.setattr(poller, "advance_purchase", unexpected)
    assert poller.poll_once(ctx) == 0
    assert read_job(ctx, job_id).attempts == 2
    assert determination_ids(ctx) == []


def test_completion_commit_failure_rolls_back_results(ctx: AppContext, monkeypatch):
    from sqlalchemy.orm import Session

    job_id = enqueue(ctx)
    commit = Session.commit
    advance = poller.advance_purchase
    work_session = []

    def tracked(session, *args):
        work_session.append(session)
        advance(session, *args)

    def fail_completion(session):
        if work_session and session is work_session[0]:
            session.flush()
            work_session.clear()
            raise RuntimeError("synthetic commit failure")
        commit(session)

    monkeypatch.setattr(poller, "advance_purchase", tracked)
    monkeypatch.setattr(Session, "commit", fail_completion)
    assert poller.poll_once(ctx) == 0
    job = read_job(ctx, job_id)
    assert (job.status, job.attempts, job.lease_until) == ("QUEUED", 1, None)
    assert determination_ids(ctx) == []
