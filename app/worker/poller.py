"""In-process poller. One thread, leases in the jobs table, no duplicate effects."""

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.extractor import PROMPT_VERSION, SCHEMA_VERSION
from app.clock import utcnow
from app.context import AppContext
from app.domain.pipeline import advance_purchase, ingest_fetched_record, ingest_import
from app.domain.ruleset import load_ruleset
from app.models import Document, IdempotencyKey, Job

log = logging.getLogger("taxleak.worker")
_lock = threading.Lock()
_IMPORT_RECORDINGS = (
    Path(__file__).resolve().parents[2] / "fixtures/recordings/imports.json"
)


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


def _claimable(now: str) -> ColumnElement[bool]:
    return or_(
        Job.status == "QUEUED",
        and_(Job.status == "RUNNING", Job.lease_until < now),
    )


def _claim(session: Session) -> Job | None:
    now = utcnow()
    lease = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
    candidate = (
        select(Job.id)
        .where(_claimable(now))
        .order_by(Job.created_at, Job.id)
        .limit(1)
        .scalar_subquery()
    )
    # Recheck eligibility in the UPDATE, including after another caller claims
    # the selected row. Only the caller receiving a row owns this attempt.
    job = session.scalar(
        update(Job)
        .where(Job.id == candidate, _claimable(now))
        .values(
            status="RUNNING",
            lease_until=lease.isoformat().replace("+00:00", "Z"),
            attempts=Job.attempts + 1,
            error_code=None,
        )
        .returning(Job)
    )
    session.commit()
    return job


def _advance(ctx: AppContext, session: Session, job: Job) -> None:
    if job.purchase_id is None:
        raise ValueError("advance job requires a purchase")
    advance_purchase(session, job.purchase_id, load_ruleset(ctx.rules_dir))


def _fetch(ctx: AppContext, session: Session, job: Job) -> None:
    if job.purchase_id is not None:
        return
    payload = json.loads(job.input_payload or "{}")
    record = ctx.ramp.fetch(
        payload["event_type"], payload["object_id"], payload["receipt_id"]
    )
    ingest_fetched_record(session, job, record)


def _import(ctx: AppContext, session: Session, job: Job) -> None:
    del ctx  # Import admission never advances the purchase or loads tax rules.
    # Intake predates input_payload: its committed 202 response is the durable,
    # org-scoped association between this job and the stored document.
    receipts = session.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.org_id == job.org_id, IdempotencyKey.status_code == 202
        )
    )
    import_id = None
    for receipt in receipts:
        response = json.loads(receipt.response_body)
        if response.get("job_id") == job.id:
            import_id = response.get("import_id")
            break
    document = session.get(Document, import_id) if isinstance(import_id, str) else None
    if document is None:
        raise ValueError("import job has no stored document")
    if document.purchase_id is not None:
        # Completed admission is a no-op after lease recovery.
        job.purchase_id = document.purchase_id
        return
    ingest_import(session, job, document, _load_import_replay(document))


def _load_import_replay(document: Document) -> object | None:
    """Read raw recorded output, retaining identity metadata for admission.

    The optional manifest maps the existing extractor cache keys to payloads.
    No recording is supplied or synthesized here. Provider/model must be pinned
    by the owner; an absent manifest, pin, or key is a replay miss.
    """
    provider = os.environ.get("MODEL_PROVIDER")
    model = os.environ.get("MODEL_NAME")
    if not provider or not model:
        return None
    try:
        saved = json.loads(_IMPORT_RECORDINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(saved, dict):
        raise ValueError("import replay manifest must be an object")
    key = f"{document.sha256}:{provider}:{model}:{PROMPT_VERSION}:{SCHEMA_VERSION}"
    return saved.get(key)


_HANDLERS: dict[str, Callable[[AppContext, Session, Job], None]] = {
    "advance": _advance,
    "fetch": _fetch,
    "import": _import,
}


def _owned(job: Job) -> ColumnElement[bool]:
    return and_(
        Job.id == job.id,
        Job.status == "RUNNING",
        Job.attempts == job.attempts,
        Job.lease_until == job.lease_until,
    )


def _poll(ctx: AppContext) -> int:
    session = ctx.session_factory()
    job: Job | None = None
    error_code = "ADVANCE_FAILED"
    claim: tuple[str, int, str | None] | None = None
    try:
        job = _claim(session)
        if job is None:
            return 0
        claim = (job.id, job.attempts, job.lease_until)
        # Lock and fence this attempt before writing results. A stale worker
        # cannot complete or fail an attempt reclaimed by another worker.
        owned = session.scalar(
            update(Job).where(_owned(job)).values(status="RUNNING").returning(Job.id)
        )
        if owned is None:
            session.rollback()
            return 0
        handler = _HANDLERS.get(job.kind)
        if handler is None:
            error_code = "UNKNOWN_JOB_KIND"
            raise ValueError(f"unknown job kind: {job.kind}")
        if job.kind != "advance":
            error_code = "FETCH_FAILED" if job.kind == "fetch" else "JOB_FAILED"
        handler(ctx, session, job)
        if job.status != "FAILED":
            job.status = "SUCCEEDED"
        job.lease_until = None
        session.commit()
        return 1
    except Exception:
        log.exception("job %s failed", claim[0] if claim else None)
        session.rollback()
        if claim is not None:
            _note_failure(ctx, *claim, error_code=error_code)
        return 0
    finally:
        session.close()


def _note_failure(
    ctx: AppContext,
    job_id: str,
    attempt: int,
    lease_until: str | None,
    *,
    error_code: str,
) -> None:
    with ctx.session_factory() as session:
        session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == "RUNNING",
                Job.attempts == attempt,
                Job.lease_until == lease_until,
            )
            .values(
                error_code=error_code,
                status="FAILED" if attempt >= 3 else "QUEUED",
                lease_until=None,
            )
        )
        session.commit()


def run_until_stopped(ctx: AppContext, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            poll_once(ctx)
        except Exception:
            log.exception("poll loop failed")
        stop.wait(0.5)
