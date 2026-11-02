"""GeM bid-result connector — the price-ladder source.

Persists, per bid: the tender, the buyer organisation and its official channel,
every participating seller with its technical outcome, the financial ranking
(L1..Ln with prices), an award row when the bid is awarded, and price
observations that feed the price-intelligence module.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import entities
from ..intelligence.normalise import NormalisationInput, normalise_price
from ..models import (
    Award,
    Bidder,
    FinancialBid,
    PriceObservation,
    Source,
    TechnicalResult,
    Tender,
    TenderLineItem,
)
from .base import Connector, SourceRestructured, load_fixture
from .gem_parsers import parse_bid_result

RESULT_URL = "https://bidplus.gem.gov.in/bidding/bid/getBidResultView/{bid_id}"
MAX_BIDS_PER_RUN = 50


class GemResultsConnector(Connector):
    key = "gem_results"

    def __init__(self, db: Session, source: Source, client=None, bid_ids: list[str] | None = None):
        super().__init__(db, source, client)
        self.bid_ids = bid_ids or []

    # ------------------------------------------------------------------ #
    def _pending_bid_ids(self) -> list[str]:
        """Bids we know about that do not yet have a financial ladder."""
        with_results = {
            row[0]
            for row in self.db.execute(
                select(FinancialBid.tender_id).distinct()
            ).all()
        }
        stmt = select(Tender).where(Tender.source_id == self.source.id).order_by(Tender.closing_at.desc())
        pending = [t for t in self.db.scalars(stmt) if t.id not in with_results]
        return [t.portal_tender_id for t in pending][:MAX_BIDS_PER_RUN]

    def fetch(self) -> list[tuple[str, bytes]]:
        ids = self.bid_ids or self._pending_bid_ids()
        out: list[tuple[str, bytes]] = []
        for bid in ids:
            url = RESULT_URL.format(bid_id=bid)
            resp = self.get(url)
            out.append((url, resp.content))
        return out

    def parse(self, url: str, content: bytes) -> list[dict]:
        try:
            return [parse_bid_result(content, url)]
        except ValueError as exc:
            raise SourceRestructured(str(exc)) from exc

    # ------------------------------------------------------------------ #
    def persist(self, record: dict) -> None:
        t = record["tender"]
        source = self.source
        tender = self.db.scalar(
            select(Tender).where(
                Tender.source_id == source.id,
                Tender.portal_tender_id == t["portal_tender_id"],
            )
        )
        if tender is None:
            tender = Tender(portal_tender_id=t["portal_tender_id"], title=t["title"], source_id=source.id)
            self.db.add(tender)

        org, unit = entities.upsert_organisation(
            self.db,
            t.get("organisation") or t.get("department") or t.get("office"),
            state=t.get("state"),
            city=t.get("city"),
            pincode=t.get("pincode"),
            department=t.get("department"),
            office=t.get("office"),
            portal_code=t.get("portal_tender_id"),
            source=source,
        )
        entities.record_contact(
            self.db,
            unit,
            individual_name=t.get("buyer_name"),
            email=t.get("buyer_email"),
            phone=t.get("buyer_phone"),
            source=source,
        )

        category = entities.category_for_text(self.db, t.get("category_name") or t.get("title"))

        tender.title = t["title"] or tender.title
        tender.description = t.get("description")
        tender.tender_ref = t.get("tender_ref")
        tender.organisation_id = org.id
        tender.org_unit_id = unit.id if unit else None
        tender.category_id = category.id
        tender.state = t.get("state")
        tender.city = t.get("city")
        tender.pincode = t.get("pincode")
        tender.published_at = t.get("published_at")
        tender.closing_at = t.get("closing_at")
        tender.opening_at = t.get("opening_at")
        tender.portal_url = t.get("portal_url")
        status = (t.get("bid_status") or "").lower()
        tender.status = "awarded" if "award" in status else ("live" if status in {"active", ""} else status)
        tender.source_id = source.id
        tender.extraction_method = "html_table"
        tender.confidence = 0.95
        tender.verification_state = "parsed"
        tender.retrieved_at = dt.datetime.now(dt.timezone.utc)
        tender.last_verified_at = tender.retrieved_at
        self.db.flush()

        # one line item for the bid (GeM bids are single-item or BOQ-based)
        line = self.db.scalar(
            select(TenderLineItem).where(
                TenderLineItem.tender_id == tender.id, TenderLineItem.line_no == 1
            )
        )
        if line is None:
            line = TenderLineItem(tender_id=tender.id, line_no=1, description=tender.title)
            self.db.add(line)
            self.db.flush()
        line.description = tender.title
        line.quantity = t.get("quantity")
        line.unit = "unit"

        product, match_score, explanation = entities.upsert_product(
            self.db, tender.title, category_name=t.get("category_name"), source=source
        )
        line.product_id = product.id

        # ---- bidders and technical results ------------------------------- #
        tech_by_name = {tr["seller_name_raw"]: tr for tr in record["technical_results"]}
        bidder_by_name: dict[str, Bidder] = {}
        for b in record["bidders"]:
            supplier = entities.upsert_supplier(self.db, b["seller_name_raw"], source=source)
            bidder = self.db.scalar(
                select(Bidder).where(
                    Bidder.tender_id == tender.id,
                    Bidder.seller_name_raw == b["seller_name_raw"],
                )
            )
            if bidder is None:
                bidder = Bidder(tender_id=tender.id, seller_name_raw=b["seller_name_raw"])
                self.db.add(bidder)
            bidder.supplier_id = supplier.id
            bidder.offered_make = b.get("offered_make")
            bidder.offered_model = b.get("offered_model")
            bidder.participated_at = b.get("participated_at")
            bidder.emd_status = b.get("emd_status")
            bidder.mse_status = b.get("mse_status")
            bidder.mii_status = b.get("mii_status")
            self.db.flush()
            bidder_by_name[b["seller_name_raw"]] = bidder

            tech = tech_by_name.get(b["seller_name_raw"])
            if tech:
                existing = self.db.scalar(select(TechnicalResult).where(TechnicalResult.bidder_id == bidder.id))
                tr = existing or TechnicalResult(bidder_id=bidder.id)
                tr.status = tech["status"]
                tr.source_offset = f"technical_evaluation:{b['seller_name_raw']}"
                self.db.add(tr)

        # ---- financial ladder -------------------------------------------- #
        for fb in record["financial_bids"]:
            supplier = entities.upsert_supplier(self.db, fb["seller_name_raw"], source=source)
            bidder = bidder_by_name.get(fb["seller_name_raw"])
            existing = self.db.scalar(
                select(FinancialBid).where(
                    FinancialBid.tender_id == tender.id, FinancialBid.rank == fb["rank"]
                )
            )
            row = existing or FinancialBid(tender_id=tender.id, rank=fb["rank"], total_price=fb["total_price"])
            row.bidder_id = bidder.id if bidder else None
            row.supplier_id = supplier.id
            row.total_price = fb["total_price"]
            row.currency = fb.get("currency", "INR")
            row.result_url = record.get("result_url")
            row.source_id = source.id
            row.retrieved_at = dt.datetime.now(dt.timezone.utc)
            row.confidence = 0.95
            self.db.add(row)
            self.db.flush()
            self._observe_price(tender, line, product, supplier, fb, row)

        # ---- award -------------------------------------------------------- #
        if tender.status == "awarded" and record["financial_bids"]:
            l1 = record["financial_bids"][0]
            supplier = entities.upsert_supplier(self.db, l1["seller_name_raw"], source=source)
            award = self.db.scalar(select(Award).where(Award.tender_id == tender.id))
            award = award or Award(tender_id=tender.id)
            award.supplier_id = supplier.id
            award.contract_value = l1["total_price"]
            award.awarded_quantity = t.get("quantity")
            award.num_bids_received = len(record["bidders"]) or None
            award.award_date = (tender.closing_at.date() if tender.closing_at else dt.date.today())
            award.source_id = source.id
            award.verification_state = "parsed"
            award.confidence = 0.9
            self.db.add(award)
            self.db.flush()
            self._observe_price(
                tender, line, product, supplier,
                {"rank": 1, "total_price": l1["total_price"]},
                basis_override="awarded",
            )

        self.db.commit()

    # ------------------------------------------------------------------ #
    def _observe_price(self, tender, line, product, supplier, fb, row=None, basis_override=None) -> None:
        """Create one price observation per rank. Basis is never inferred."""
        rank = fb["rank"]
        basis = basis_override or {1: "l1_quote", 2: "l2_quote", 3: "l3_quote"}.get(rank, f"l{rank}_quote")
        if basis not in {"awarded", "l1_quote", "l2_quote", "l3_quote"}:
            return  # deeper ranks are stored in financial_bids but not aggregated
        existing = self.db.scalar(
            select(PriceObservation).where(
                PriceObservation.tender_id == tender.id,
                PriceObservation.line_item_id == line.id,
                PriceObservation.basis == basis,
                PriceObservation.supplier_id == supplier.id,
            )
        )
        if existing:
            return
        qty = tender.line_items[0].quantity if tender.line_items else None
        try:
            result = normalise_price(
                NormalisationInput(
                    total_price=fb["total_price"],
                    quantity=qty,
                    gst_treatment="unknown",
                )
            )
        except ValueError:
            return
        observed = (tender.closing_at.date() if tender.closing_at else dt.date.today())
        self.db.add(
            PriceObservation(
                product_id=product.id,
                tender_id=tender.id,
                line_item_id=line.id,
                supplier_id=supplier.id,
                organisation_id=tender.organisation_id,
                state=tender.state,
                observed_at=observed,
                basis=basis,
                unit_price=result.unit_price,
                quantity=qty,
                gst_treatment="unknown",
                normalised_unit_price=result.normalised_unit_price,
                normalisation_notes={
                    "steps": result.steps,
                    "estimated_factors": result.estimated_factors,
                    "quality": result.quality,
                },
                source_id=self.source.id,
                confidence=0.85 if result.estimated_factors else 0.95,
            )
        )

    # ------------------------------------------------------------------ #
    def self_test(self) -> dict:
        content = load_fixture("gem_result_fixture.html")
        record = parse_bid_result(content, RESULT_URL.format(bid_id="fixture"))
        assertions = {
            "has_bid_number": bool(record["tender"]["portal_tender_id"]),
            "has_quantity": record["tender"]["quantity"] is not None,
            "has_bidders": len(record["bidders"]) >= 1,
            "has_ladder": len(record["financial_bids"]) >= 2,
            "l1_rank_present": record["financial_bids"][0]["rank"] == 1,
            "l1_price_positive": record["financial_bids"][0]["total_price"] > 0,
            "ranks_ascending_price": all(
                a["total_price"] <= b["total_price"]
                for a, b in zip(record["financial_bids"], record["financial_bids"][1:])
            ),
        }
        return {
            "source": self.key,
            "ok": all(assertions.values()),
            "assertions": assertions,
            "sample": {
                "bid": record["tender"]["portal_tender_id"],
                "bidders": len(record["bidders"]),
                "ladder": [(f["rank"], f["seller_name_raw"], f["total_price"]) for f in record["financial_bids"]],
            },
        }
