"""POST /api/v1/imports stores one synthetic file without a determination.

A valid upload returns 202 with import and job ids, records the SHA-256 of
the bytes on the row, and enqueues a kind=import job the advance poller
never runs (#49 owns that kind). Rejections create no purchase and no
determination. Every request carries X-Requested-With; POSTs carry the #31
CSRF token through the shared test helper without redesigning it.
"""

import hashlib
import json
import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adapters.storage import StoredDocument
from app.api import routes as routes_mod
from app.models import Determination, Document, Job, Organization, Purchase
from app.seed import ENTITY_NAME, ORG_ID
from tests.api.test_flow import _headers, signed_client  # noqa: F401

PNG = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)) + b"synthetic-png"
JPEG = bytes((0xFF, 0xD8, 0xFF, 0xE0)) + b"synthetic-jpeg"
ONE_PAGE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Page >>\nendobj\n"
NO_PAGE_PDF = b"%PDF-1.7\n% no countable page object here\n"
CSV = b"vendor,amount\nSynthetic Vendor,10.00\n"
TEN_MB = 10 * 1024 * 1024


class FakeStore:
    """Injected storage port: content-addressed, in memory, never Azure."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put_document(self, data: bytes, mime_type: str) -> StoredDocument:
        del mime_type
        digest = hashlib.sha256(bytes(data)).hexdigest()
        key = f"documents/{digest}"
        self.blobs.setdefault(key, bytes(data))
        return StoredDocument(key=key, sha256=digest, size=len(data))


@pytest.fixture
def iclient(
    signed_client: TestClient,  # noqa: F811
) -> Iterator[tuple[TestClient, FakeStore]]:
    routes_mod._reset_import_rate_limit()
    fake = FakeStore()
    signed_client.app.state.storage = fake
    yield signed_client, fake
    routes_mod._reset_import_rate_limit()


def _post(
    client: TestClient,
    key: str,
    *,
    entity_id: str = ORG_ID,
    filename: str = "invoice.png",
    content: bytes = PNG,
    mime: str = "image/png",
    form: dict[str, str] | None = None,
    csrf: bool = True,
):
    data = {"entity_id": entity_id}
    if form is not None:
        data.update(form)
    return client.post(
        "/api/v1/imports",
        data=data,
        files={"file": (filename, content, mime)},
        headers=_headers(key, csrf=csrf),
    )


def _get(client: TestClient, path: str):
    return client.get(path, headers={"X-Requested-With": "taxleak"})


def _counts(client: TestClient) -> dict[str, int]:
    session = client.app.state.ctx.session_factory()
    try:
        return {
            name: session.scalar(select(func.count()).select_from(model)) or 0
            for name, model in (
                ("purchases", Purchase),
                ("determinations", Determination),
                ("documents", Document),
                ("jobs", Job),
            )
        }
    finally:
        session.close()


def test_valid_png_returns_202_with_import_and_job_ids(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, fake = iclient
    response = _post(client, "import-png")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["data_mode"] == "synthetic"
    import_id = body["import_id"]
    job_id = body["job_id"]
    assert isinstance(import_id, str) and isinstance(job_id, str)

    session = client.app.state.ctx.session_factory()
    try:
        row = session.get(Document, import_id)
        assert row is not None
        assert row.sha256 == hashlib.sha256(PNG).hexdigest()
        assert row.blob_key == f"documents/{row.sha256}"
        assert row.mime_type == "image/png"
        assert row.pages == 1
        assert row.purchase_id is None
        job = session.get(Job, job_id)
        assert job is not None
        assert job.kind == "import"
        assert job.status == "QUEUED"
        assert job.purchase_id is None
    finally:
        session.close()
    assert fake.blobs[row.blob_key] == PNG

    assert _counts(client)["purchases"] == 0
    assert _counts(client)["determinations"] == 0
    assert "engine_amount" not in json.dumps(body)
    seen = _get(client, f"/api/v1/jobs/{job_id}").json()
    assert seen["stage"] == "import"
    assert seen["status"] == "QUEUED"
    assert _get(client, "/api/v1/purchases").json()["purchases"] == []


def test_same_key_and_body_replay_returns_202_with_same_ids(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    first = _post(client, "import-replay")
    assert first.status_code == 202, first.text
    second = _post(client, "import-replay")
    assert second.status_code == 202, second.text
    assert second.json()["import_id"] == first.json()["import_id"]
    assert second.json()["job_id"] == first.json()["job_id"]
    assert _counts(client)["documents"] == 1
    assert _counts(client)["jobs"] == 1


def test_csv_is_accepted_without_parsing(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, fake = iclient
    response = _post(
        client, "import-csv", filename="lines.csv", content=CSV, mime="text/csv"
    )
    assert response.status_code == 202, response.text
    session = client.app.state.ctx.session_factory()
    try:
        row = session.get(Document, response.json()["import_id"])
        assert row is not None
        assert row.sha256 == hashlib.sha256(CSV).hexdigest()
        assert row.pages == 1
    finally:
        session.close()
    assert list(fake.blobs.values()) == [CSV]
    assert _counts(client)["purchases"] == 0
    assert _counts(client)["determinations"] == 0


@pytest.mark.parametrize(
    ("filename", "content", "mime", "code"),
    [
        (
            "invoice.zip",
            b"PK\x03\x04binary",
            "application/zip",
            "UNSUPPORTED_FILE_TYPE",
        ),
        ("notes.txt", b"plain text", "text/plain", "UNSUPPORTED_FILE_TYPE"),
        ("invoice.pdf", PNG, "application/pdf", "FILE_SIGNATURE_MISMATCH"),
        ("invoice.png", ONE_PAGE_PDF, "image/png", "FILE_SIGNATURE_MISMATCH"),
    ],
)
def test_bad_type_or_signature_is_422_with_no_side_effects(
    iclient: tuple[TestClient, FakeStore],
    filename: str,
    content: bytes,
    mime: str,
    code: str,
) -> None:
    client, _ = iclient
    response = _post(
        client, f"bad-{code}-{filename}", filename=filename, content=content, mime=mime
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert _counts(client) == {
        "purchases": 0,
        "determinations": 0,
        "documents": 0,
        "jobs": 0,
    }


def test_url_field_is_rejected_with_no_side_effects(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    response = _post(
        client, "import-url", form={"url": "https://example.invalid/i.pdf"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "URL_NOT_ALLOWED"
    assert _counts(client) == {
        "purchases": 0,
        "determinations": 0,
        "documents": 0,
        "jobs": 0,
    }


def test_empty_url_field_is_treated_as_absent(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    response = _post(client, "import-empty-url", form={"url": ""})
    assert response.status_code == 202, response.text
    assert isinstance(response.json()["import_id"], str)
    assert _counts(client)["documents"] == 1


def test_same_key_with_and_without_url_conflicts(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    first = _post(client, "import-url-clash")
    assert first.status_code == 202, first.text
    retry = _post(
        client, "import-url-clash", form={"url": "https://example.invalid/i.pdf"}
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _counts(client)["documents"] == 1
    assert _counts(client)["jobs"] == 1


def test_oversize_body_is_413_with_no_side_effects(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    big = PNG + b"\x00" * (TEN_MB + 1 - len(PNG))
    response = _post(client, "import-big", content=big)
    assert response.status_code == 413, response.text[:200]
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert _counts(client) == {
        "purchases": 0,
        "determinations": 0,
        "documents": 0,
        "jobs": 0,
    }


def test_unreadable_page_count_is_422_with_no_side_effects(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    response = _post(
        client,
        "import-nopages",
        filename="blank.pdf",
        content=NO_PAGE_PDF,
        mime="application/pdf",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "UNREADABLE_DOCUMENT"
    assert _counts(client) == {
        "purchases": 0,
        "determinations": 0,
        "documents": 0,
        "jobs": 0,
    }


def test_too_many_pages_is_413_with_no_side_effects(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    eleven = b"%PDF-1.4\n" + b"<< /Type /Page >>\n" * 11
    response = _post(
        client,
        "import-pages",
        filename="long.pdf",
        content=eleven,
        mime="application/pdf",
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert _counts(client) == {
        "purchases": 0,
        "determinations": 0,
        "documents": 0,
        "jobs": 0,
    }


def test_missing_csrf_token_is_403(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    response = _post(client, "import-nocrsf", csrf=False)
    assert response.status_code == 403, response.text
    assert _counts(client)["documents"] == 0


def test_entity_outside_session_org_is_404(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    response = _post(client, "import-stranger", entity_id=str(uuid4()))
    assert response.status_code == 404, response.text
    assert _counts(client) == {
        "purchases": 0,
        "determinations": 0,
        "documents": 0,
        "jobs": 0,
    }


def test_entity_is_the_seeded_session_entity(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    session = client.app.state.ctx.session_factory()
    try:
        org = session.get(Organization, ORG_ID)
        assert org is not None
        assert org.name == ENTITY_NAME == "Acme Startup LLC"
    finally:
        session.close()


def test_rate_limit_trips(
    iclient: tuple[TestClient, FakeStore],
) -> None:
    client, _ = iclient
    statuses = [_post(client, f"import-burst-{n}").status_code for n in range(11)]
    assert statuses[:10] == [202] * 10
    assert statuses[10] == 429
    limited = _post(client, "import-burst-10")
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_logs_carry_ids_not_document_text(
    iclient: tuple[TestClient, FakeStore],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = iclient
    sentinel = b"vendor-meter SENTINEL-7f3a only-in-this-file"
    content = b"vendor,note\nSynthetic Vendor," + sentinel + b"\n"
    with caplog.at_level(logging.INFO, logger="taxleak.api"):
        response = _post(
            client,
            "import-quiet",
            filename="s.csv",
            content=content,
            mime="text/csv",
        )
    assert response.status_code == 202, response.text
    assert sentinel.decode("utf-8") not in caplog.text
