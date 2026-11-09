"""In-process poller. One thread, leases in the jobs table, no duplicate effects."""

import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select

from app.clock import utcnow
from app.context import AppContext
from app.domain.pipeline import advance_purchase
from app.domain.ruleset import load_ruleset
from app.models import Job

log = logging.getLogger("taxleak.worker")
_lock = threading.Lock()


class JobPoller:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def poll_once(self) -> int:
        return poll_once(self.ctx)


def poll_once(ctx: AppContext) -> int:
    if not _lock.acquire(blocking=False):
        return 0
    try:
        return _poll(ctx)
    finally:
        _lock.release()


def _poll(ctx: AppContext) -> int:
    session = ctx.session_factory()
    job_id: str | None = None
    try:
        lease = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
        now = utcnow()
        job = session.scalar(
            select(Job)
            .where(
                or_(
                    Job.status == "QUEUED",
                    and_(
                        Job.status == "RUNNING",
                        Job.lease_until.is_not(None),
                        Job.lease_until < now,
                    ),
                )
            )
            .order_by(Job.created_at)
            .limit(1)
        )
        if job is None or job.purchase_id is None:
            session.rollback()
            return 0
        job_id = job.id
        job.status = "RUNNING"
        job.lease_until = lease.isoformat().replace("+00:00", "Z")
        job.attempts += 1
        ruleset = load_ruleset(ctx.rules_dir)
        advance_purchase(session, job.purchase_id, ruleset)
        job.status = "SUCCEEDED"
        job.lease_until = None
        session.commit()
        return 1
    except Exception:
        log.exception("job %s failed", job_id)
        session.rollback()
        if job_id is not None:
            _note_failure(ctx, job_id)
        return 0
    finally:
        session.close()


def _note_failure(ctx: AppContext, job_id: str) -> None:
    session = ctx.session_factory()
    try:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.attempts += 1
        job.error_code = "ADVANCE_FAILED"
        job.status = "FAILED" if job.attempts >= 3 else "QUEUED"
        job.lease_until = None
        session.commit()
    finally:
        session.close()


def run_until_stopped(ctx: AppContext, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            poll_once(ctx)
        except Exception:
            log.exception("poll loop failed")
        stop.wait(0.5)
