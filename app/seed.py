"""Seed the one synthetic org from the spec's demo setup. No tax decisions."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Organization, Person, SetupRow

ORG_ID = "00000000-0000-4000-8000-000000000001"
PERSON_ID = "00000000-0000-4000-8000-000000000002"
SETUP_ID = "00000000-0000-4000-8000-000000000003"
ENTITY_NAME = "Synthetic Northwind"


def seed(session: Session) -> None:
    if session.get(Organization, ORG_ID) is not None:
        return
    session.add(Organization(id=ORG_ID, name=ENTITY_NAME))
    session.add(Person(id=PERSON_ID, org_id=ORG_ID, name="Avery Chen", role="reviewer"))
    session.add(
        SetupRow(
            id=SETUP_ID,
            org_id=ORG_ID,
            jurisdictions="CA,WA,NY",
            period_start="2026-10-01",
            period_end="2026-12-31",
            basis="invoice_date",
        )
    )
    session.flush()
    # Touch the import so a fresh database always has the reviewer row.
    assert session.scalar(select(Person).where(Person.id == PERSON_ID)) is not None
