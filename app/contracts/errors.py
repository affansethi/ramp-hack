"""Domain error envelope wrapped as {"error": {...}}."""

from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    """Fields inside the error object. Callers still read ``error.code``."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field_errors: list[dict[str, str]]
    retryable: bool
    correlation_id: str


class ErrorResponse(BaseModel):
    """The only error wire shape. The body stays wrapped under ``error``."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
