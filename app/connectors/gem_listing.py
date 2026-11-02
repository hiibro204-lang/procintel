"""GeM bid-listing connector.

Listing pages give us discovery: which bids exist, who is buying, what category,
what quantity, and when they close. Prices live on the result page, so this
connector's job is to keep the tender corpus current and to hand bid IDs to the
result connector.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import entities
from ..models import Source, Tender
from .base import Connector, SourceRestructured, load_fixture, parse_date
from .gem_parsers import parse_listing

LISTING_URLS = [
    "https://bidplus.gem.gov.in/all-bids-data",
]


class GemListingConnector(Connector):
    key = "gem_listing"

    def __init__(self, db: Session, source: Source, client=None, urls: list[str] | None = None):
        super().__init__(db, source, client)
        self.urls = urls or LISTING_URLS

    def fetch(self) -> list[tuple[str, bytes]]:
        out = []
        for url in self.urls:
            resp = self.get(url)
            out.append((url, resp.content))
        return out

    def parse(self, url: str, content: bytes) -> list[dict]:
        records = parse_listing(content, url)
        if not records:
            raise SourceRestructured("listing page produced no recognisable bid rows")
        return records

    def persist(self, record: dict) -> None:
        portal_id = record.get("portal_tender_id")
        if not portal_id:
            raise ValueError("record without portal_tender_id")
        tender = self.db.scalar(
            select(Tender).where(
                Tender.source_id == self.source.id, Tender.portal_tender_id == portal_id
            )
        )
        if tender is None:
            tender = Tender(portal_tender_id=portal_id, title=record.get("title") or portal_id,
                            source_id=self.source.id)
            self.db.add(tender)
            self.db.flush()

        org, unit = entities.upsert_organisation(
            self.db,
            record.get("department") or record.get("ministry"),
            state=record.get("state"),
            department=record.get("department"),
            office=record.get("office"),
            portal_code=portal_id,
            source=self.source,
        )
        category = entities.category_for_text(self.db, record.get("category_name") or record.get("title"))

        tender.title = record.get("title") or tender.title
        tender.description = record.get("category_name")
        tender.organisation_id = org.id
        tender.org_unit_id = unit.id if unit else None
        tender.category_id = category.id
        tender.state = record.get("state")
        tender.published_at = record.get("published_at") or tender.published_at
        tender.closing_at = record.get("closing_at") or tender.closing_at
        tender.portal_url = record.get("portal_url")
        tender.status = "live"
        tender.extraction_method = "html_listing"
        tender.confidence = 0.9
        tender.verification_state = "parsed"
        tender.retrieved_at = dt.datetime.now(dt.timezone.utc)
        tender.last_verified_at = tender.retrieved_at
        self.db.commit()

    def self_test(self) -> dict:
        content = load_fixture("gem_listing_fixture.html")
        records = parse_listing(content, LISTING_URLS[0])
        assertions = {
            "records_found": len(records) >= 2,
            "all_have_bid_number": all(r.get("portal_tender_id") for r in records),
            "closing_dates_parsed": all(r.get("closing_at") for r in records),
        }
        return {
            "source": self.key,
            "ok": all(assertions.values()),
            "assertions": assertions,
            "sample": records[:2],
        }
