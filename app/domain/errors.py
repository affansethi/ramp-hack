"""Domain refusals. No refusal writes an amount."""


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 422,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.field_errors: list[dict[str, str]] = []
