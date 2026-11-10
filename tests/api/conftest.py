"""Reset the shared login CSRF token before and after each API test."""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_csrf_token() -> Iterator[None]:
    from tests.api.test_flow import reset_csrf

    reset_csrf()
    yield
    reset_csrf()
