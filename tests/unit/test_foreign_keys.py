"""Foreign keys hold locally the way they do on hosted Postgres.

Checks are deferred to commit, so insert order inside a transaction never
matters, but a dangling reference still refuses to commit.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import init_database
from app.models import Document, Purchase


def _purchase(purchase_id: str) -> Purchase:
    return Purchase(
        id=purchase_id,
        org_id="org-demo",
        vendor="Synthetic Vendor",
        invoice_number="INV-FK",
        invoice_date="2026-01-01",
        entity_name="Synthetic Co",
        currency="USD",
        item_status="INTAKEN",
        extraction_mode="replay",
        facts_version=1,
        created_at="2026-01-01T00:00:00Z",
    )


def _document(purchase_id: str) -> Document:
    return Document(
        id=str(uuid4()),
        purchase_id=purchase_id,
        sha256="0" * 64,
        mime_type="application/json",
        pages=1,
    )


def test_child_before_parent_commits(tmp_path: Path) -> None:
    factory = init_database(f"sqlite:///{tmp_path / 'fk.db'}")
    purchase_id = str(uuid4())
    with factory() as session:
        document = _document(purchase_id)
        session.add(document)
        session.flush()  # child row written first
        session.add(_purchase(purchase_id))
        session.commit()
        assert session.get(Document, document.id) is not None


def test_dangling_reference_refuses_to_commit(tmp_path: Path) -> None:
    factory = init_database(f"sqlite:///{tmp_path / 'fk.db'}")
    with factory() as session:
        session.add(_document(str(uuid4())))
        session.flush()
        with pytest.raises(IntegrityError):
            session.commit()
