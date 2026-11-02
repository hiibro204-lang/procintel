"""Seed a deterministic demo corpus.

This exists so the platform can be run, demoed and tested without touching any
portal. Every record it creates is attached to a source named "Demo corpus
(synthetic)" and is marked with extraction_method='demo_seed', so synthetic data
can never be mistaken for collected data.

    python -m scripts.seed_demo --tenders 80
"""
from __future__ import annotations

import argparse
import datetime as dt
import random

from sqlalchemy import select

from app import entities
from app.db import init_db, session_scope
from app.intelligence.aggregates import recompute_aggregates
from app.intelligence.normalise import NormalisationInput, normalise_price
from app.models import (
    Award,
    Bidder,
    FinancialBid,
    Organisation,
    PriceObservation,
    Source,
    Supplier,
    TechnicalResult,
    Tender,
    TenderLineItem,
)

DEMO_SOURCE_KEY = "demo_seed"

PRODUCTS = [
    # (name, base unit price, quantity range, unit, category hint)
    ("Ergonomic Office Chair High Back Mesh with Lumbar Support", 14800, (10, 120), "nos", "office furniture chair"),
    ("Interactive Flat Panel Display 75 Inch with CPU", 178000, (1, 12), "nos", "IT panel display"),
    ("LED Luminaire for High Bay Lighting 150W", 3900, (100, 900), "nos", "electrical led luminaire"),
    ("Split Air Conditioner 1.5 Ton Inverter", 38500, (4, 60), "nos", "HVAC air conditioner"),
    ("Desktop Computer Core i5 with 8GB RAM and 512GB SSD", 46500, (10, 200), "nos", "IT computer desktop"),
    ("Online UPS 5 kVA with Battery Bank", 62000, (2, 25), "nos", "electrical ups"),
    ("Laptop Core i7 14 inch with 16GB RAM", 82000, (5, 80), "nos", "IT laptop"),
    ("Steel Almirah 6 feet with Locker", 12800, (5, 90), "nos", "office furniture almirah"),
]

BUYERS = [
    ("Directorate of Health Services", "Madhya Pradesh", "Bhopal", "462001"),
    ("Department of Higher Education", "Telangana", "Hyderabad", "500001"),
    ("Municipal Corporation", "Maharashtra", "Pune", "411001"),
    ("Public Works Department", "Karnataka", "Bengaluru", "560001"),
    ("State Transport Corporation", "Tamil Nadu", "Chennai", "600001"),
    ("Directorate of Medical Education", "Rajasthan", "Jaipur", "302001"),
    ("Department of School Education", "Gujarat", "Ahmedabad", "380001"),
    ("District Rural Development Agency", "Uttar Pradesh", "Lucknow", "226001"),
]

SUPPLIERS = [
    "SHREEJI OFFICE SYSTEMS PRIVATE LIMITED",
    "DECCAN SEATING SOLUTIONS",
    "VENKATESWARA FURNISHINGS",
    "ORBIT IT INFRASTRUCTURE",
    "GANGOTRI ELECTRICALS",
    "PRECISION COOLING SYSTEMS",
    "SUNRISE ENTERPRISES",
    "TRINETRA TECHNOLOGIES",
]

GST_RATES = [0.18, 0.28, 0.12]


def ensure_demo_source(db) -> Source:
    source = db.scalar(select(Source).where(Source.key == DEMO_SOURCE_KEY))
    if source is None:
        source = Source(
            key=DEMO_SOURCE_KEY,
            name="Demo corpus (synthetic)",
            tier="A",
            category="demo",
            base_url="local://demo",
            connector=None,
            enabled=True,
            auth_required=False,
            robots_status="allowed",
            terms_summary="Synthetic records generated locally. Not collected from any portal.",
            permitted_paths=[],
            rate_limit_per_min=0,
            redistribution_allowed=True,
            policy_reviewed_at=dt.date.today(),
            policy_reviewed_by="seed-script",
            policy_note="Local development and demonstration data only. Never merge with collected records.",
        )
        db.add(source)
        db.commit()
    return source


def seed(db, n_tenders: int = 60, seed_value: int = 20261102) -> dict:
    rng = random.Random(seed_value)
    entities.ensure_taxonomy(db)
    source = ensure_demo_source(db)
    today = dt.date.today()
    counts = {"tenders": 0, "bids": 0, "awards": 0, "observations": 0}

    for i in range(n_tenders):
        name, base_price, qty_range, unit, category_hint = rng.choice(PRODUCTS)
        buyer_name, state, city, pincode = rng.choice(BUYERS)
        quantity = rng.randint(*qty_range)
        months_back = rng.randint(1, 24)
        closing = today - dt.timedelta(days=months_back * 30 + rng.randint(0, 25))
        published = closing - dt.timedelta(days=rng.randint(14, 30))

        tender = Tender(
            portal_tender_id=f"DEMO/2026/B/{100000 + i}",
            tender_ref=f"DEMO/2026/B/{100000 + i}",
            title=f"{name} — Qty {quantity}",
            description=category_hint,
            state=state,
            city=city,
            pincode=pincode,
            tender_type="goods",
            published_at=dt.datetime.combine(published, dt.time(10, 0), tzinfo=dt.timezone.utc),
            closing_at=dt.datetime.combine(closing, dt.time(17, 0), tzinfo=dt.timezone.utc),
            status="awarded",
            estimated_value=None,
            estimated_value_is_published=False,
            portal_url=f"local://demo/tender/{100000 + i}",
            source_id=source.id,
            extraction_method="demo_seed",
            confidence=0.6,
            verification_state="inferred",
        )
        db.add(tender)
        db.flush()

        org, unit_row = entities.upsert_organisation(
            db, buyer_name, state=state, city=city, pincode=pincode, source=source
        )
        entities.record_contact(db, unit_row, email=f"purchase.{city.lower()}@example.gov.in",
                                phone="011-0000-0000", source=source)
        category = entities.category_for_text(db, category_hint)
        tender.organisation_id = org.id
        tender.org_unit_id = unit_row.id if unit_row else None
        tender.category_id = category.id

        line = TenderLineItem(
            tender_id=tender.id, line_no=1, description=name,
            quantity=quantity, unit=unit, source_offset="demo:line1",
        )
        db.add(line)
        db.flush()
        product, _, _ = entities.upsert_product(db, name, category_name=category_hint, source=source)
        line.product_id = product.id

        # a plausible ladder: L1 lowest, each subsequent a little higher
        n_bidders = rng.randint(2, 4)
        chosen = rng.sample(SUPPLIERS, n_bidders)
        # small drift so trends are visible over time
        drift = 1.0 + (24 - months_back) * 0.004
        l1_total = base_price * quantity * drift * rng.uniform(0.93, 1.07)
        totals = [l1_total]
        for _ in range(n_bidders - 1):
            totals.append(totals[-1] * rng.uniform(1.02, 1.09))

        for rank, (seller, total) in enumerate(zip(chosen, totals), start=1):
            supplier = entities.upsert_supplier(db, seller, source=source)
            bidder = Bidder(
                tender_id=tender.id, supplier_id=supplier.id, seller_name_raw=seller,
                emd_status="Registered with designated Agency / Authority",
                mse_status=rng.choice(["MII", "MII MSE", ""]),
            )
            db.add(bidder)
            db.flush()
            db.add(TechnicalResult(bidder_id=bidder.id, status="qualified",
                                   source_offset="demo:technical"))
            db.add(FinancialBid(
                tender_id=tender.id, bidder_id=bidder.id, supplier_id=supplier.id,
                rank=rank, total_price=round(total, 2), currency="INR",
                result_url=f"local://demo/result/{100000 + i}", source_id=source.id, confidence=0.9,
            ))
            counts["bids"] += 1

            basis = {1: "l1_quote", 2: "l2_quote", 3: "l3_quote"}.get(rank)
            if basis:
                gst = rng.choice(GST_RATES)
                result = normalise_price(NormalisationInput(
                    total_price=round(total, 2), quantity=quantity,
                    gst_treatment="inclusive", gst_rate=gst,
                ))
                db.add(PriceObservation(
                    product_id=product.id, tender_id=tender.id, line_item_id=line.id,
                    supplier_id=supplier.id, organisation_id=org.id, state=state,
                    observed_at=closing, basis=basis,
                    unit_price=result.unit_price, quantity=quantity,
                    gst_treatment="inclusive",
                    normalised_unit_price=result.normalised_unit_price,
                    normalisation_notes={"steps": result.steps, "quality": result.quality,
                                         "estimated_factors": result.estimated_factors},
                    source_id=source.id, confidence=0.85,
                ))
                counts["observations"] += 1

        # estimated value and EMD, as a real notice would publish them
        estimate = round(l1_total * 1.12, 2)
        tender.estimated_value = estimate
        tender.estimated_value_is_published = True
        tender.emd_amount = round(estimate * 0.02, 2)
        tender.tender_fee = 500.0
        db.add(tender)

        # award to L1
        winner = entities.upsert_supplier(db, chosen[0], source=source)
        db.add(Award(
            tender_id=tender.id, supplier_id=winner.id, award_date=closing + dt.timedelta(days=9),
            contract_value=round(l1_total, 2), awarded_quantity=quantity,
            num_bids_received=n_bidders, source_id=source.id,
            verification_state="inferred", confidence=0.85,
        ))
        counts["awards"] += 1

        result = normalise_price(NormalisationInput(
            total_price=round(l1_total, 2), quantity=quantity,
            gst_treatment="inclusive", gst_rate=0.18,
        ))
        db.add(PriceObservation(
            product_id=product.id, tender_id=tender.id, line_item_id=line.id,
            supplier_id=winner.id, organisation_id=org.id, state=state, observed_at=closing,
            basis="awarded", unit_price=result.unit_price, quantity=quantity,
            gst_treatment="inclusive", normalised_unit_price=result.normalised_unit_price,
            normalisation_notes={"steps": result.steps, "quality": result.quality,
                                 "estimated_factors": result.estimated_factors},
            source_id=source.id, confidence=0.85,
        ))
        counts["observations"] += 1
        counts["tenders"] += 1

    db.commit()
    recompute_aggregates(db)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a synthetic demo corpus")
    parser.add_argument("--tenders", type=int, default=60)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    args = parser.parse_args()

    init_db(drop=args.reset)
    with session_scope() as db:
        counts = seed(db, n_tenders=args.tenders)
    print("seeded:", counts)


if __name__ == "__main__":
    main()
