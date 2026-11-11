"""Idempotency-Key: same body replays; a different body conflicts."""

import json
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.domain.errors import DomainError
from app.models import IdempotencyKey


def begin(
    session: Session, org_id: str, key: str | None, canonical: str
) -> dict[str, object] | None:
    if not key:
        raise DomainError(
            "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required.", 400
        )
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    row = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.org_id == org_id, IdempotencyKey.key == key
        )
    )
    if row is None:
        return None
    if row.body_hash != digest:
        raise DomainError(
            "IDEMPOTENCY_CONFLICT",
            "This Idempotency-Key was used with a different body.",
            409,
        )
    loaded = json.loads(row.response_body)
    if not isinstance(loaded, dict):
        raise DomainError(
            "IDEMPOTENCY_CONFLICT", "Stored response was not an object.", 409
        )
    return loaded


def finish(
    session: Session,
    org_id: str,
    key: str,
    canonical: str,
    payload: dict[str, object],
    status_code: int = 200,
) -> None:
    session.add(
        IdempotencyKey(
            id=str(uuid4()),
            org_id=org_id,
            key=key,
            body_hash=sha256(canonical.encode("utf-8")).hexdigest(),
            status_code=status_code,
            response_body=json.dumps(payload),
            created_at=utcnow(),
        )
    )


def stored_status(session: Session, org_id: str, key: str) -> int:
    """HTTP status of the stored replay, so a 202 route replays as 202."""
    row = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.org_id == org_id, IdempotencyKey.key == key
        )
    )
    if row is None:
        return 200
    return row.status_code
