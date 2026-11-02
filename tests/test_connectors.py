"""End-to-end connector behaviour, driven by fixtures rather than the network."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.connectors import build_connector
from app.connectors.base import load_fixture
from app.models import (
    Award,
    Bidder,
    FinancialBid,
    PriceObservation,
    Source,
    Tender,
    TenderLineItem,
)

URL = "https://bidplus.gem.gov.in/bidding/bid/getBidResultView/GEM/2026/B/8069492"


@pytest.fixture()
def ingested(db, gem_source):
    connector = build_connector(db, gem_source)
    connector.ingest_bytes(URL, load_fixture("gem_result_fixture.html"))
    return connector


def test_connector_self_test_passes(db, gem_source):
    result = build_connector(db, gem_source).self_test()
    assert result["ok"], result["assertions"]


def test_ingest_creates_the_price_ladder(db, ingested):
    tender = db.scalar(select(Tender).where(Tender.portal_tender_id == "GEM/2026/B/8069492"))
    assert tender is not None
    ladder = list(db.scalars(select(FinancialBid).where(FinancialBid.tender_id == tender.id)))
    assert len(ladder) == 3
    assert {row.rank for row in ladder} == {1, 2, 3}
    assert tender.status == "awarded"


def test_ingest_creates_award_and_observations(db, ingested):
    tender = db.scalar(select(Tender).where(Tender.portal_tender_id == "GEM/2026/B/8069492"))
    award = db.scalar(select(Award).where(Award.tender_id == tender.id))
    assert award is not None
    assert award.contract_value == 620000.0
    bases = {p.basis for p in db.scalars(select(PriceObservation).where(PriceObservation.tender_id == tender.id))}
    assert {"awarded", "l1_quote", "l2_quote", "l3_quote"} <= bases


def test_awarded_price_is_never_written_as_an_estimate(db, ingested):
    tender = db.scalar(select(Tender).where(Tender.portal_tender_id == "GEM/2026/B/8069492"))
    observations = list(db.scalars(select(PriceObservation).where(PriceObservation.tender_id == tender.id)))
    awarded = [o for o in observations if o.basis == "awarded"]
    assert awarded and awarded[0].normalised_unit_price
    # 620000 / 40 = 15500; unknown GST must not have been guessed away
    assert round(awarded[0].unit_price, 2) == 15500.0


def test_ingest_is_idempotent(db, ingested, gem_source):
    connector = build_connector(db, gem_source)
    connector.ingest_bytes(URL, load_fixture("gem_result_fixture.html"))
    tender = db.scalar(select(Tender).where(Tender.portal_tender_id == "GEM/2026/B/8069492"))
    assert db.query(FinancialBid).filter(FinancialBid.tender_id == tender.id).count() == 3
    assert db.query(Award).filter(Award.tender_id == tender.id).count() == 1
    assert db.query(TenderLineItem).filter(TenderLineItem.tender_id == tender.id).count() == 1


def test_bidders_include_the_disqualified_seller(db, ingested):
    tender = db.scalar(select(Tender).where(Tender.portal_tender_id == "GEM/2026/B/8069492"))
    names = {b.seller_name_raw for b in db.scalars(select(Bidder).where(Bidder.tender_id == tender.id))}
    assert "KAVERI TRADERS" in names


def test_buyer_contact_is_stored_as_an_official_channel(db, ingested):
    from app.models import Contact

    contacts = list(db.scalars(select(Contact)))
    assert contacts
    contact = contacts[-1]
    assert contact.data_class in {"official_channel", "individual_identifying"}
    assert contact.official_email and "@" in contact.official_email


def test_blocked_source_cannot_be_run(db):
    from app.registry import ComplianceError, assert_runnable

    maharashtra = db.query(Source).filter(Source.key == "maharashtra").one()
    with pytest.raises(ComplianceError):
        assert_runnable(maharashtra)
