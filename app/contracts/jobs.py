"""GET /jobs/{id} read model.

``stage`` is the existing ``job.kind`` string. It is not a pipeline-stage enum.
``purchase_id`` may be null when the job runs before its purchase exists.
``retryable`` is true only for QUEUED or RUNNING.
"""

from pydantic import BaseModel, ConfigDict

_RETRYABLE_STATUSES = frozenset({"QUEUED", "RUNNING"})


class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    purchase_id: str | None
    attempts: int
    error_code: str | None
    stage: str
    retryable: bool


def read_job(
    *,
    id: str,
    status: str,
    purchase_id: str | None,
    attempts: int,
    error_code: str | None,
    stage: str,
) -> JobRead:
    return JobRead(
        id=id,
        status=status,
        purchase_id=purchase_id,
        attempts=attempts,
        error_code=error_code,
        stage=stage,
        retryable=status in _RETRYABLE_STATUSES,
    )
