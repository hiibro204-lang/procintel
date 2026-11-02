"""Versioned REST API.

Every endpoint that returns money also returns provenance, because a price without
a source is a liability. Individual-identifying fields are never serialised here —
see the `data_class` rule in Section 3.3 of the blueprint.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import entities
from ..connectors import ConnectorError, build_connector
from ..db import get_db
from ..intelligence.aggregates import product_price_stats, recompute_aggregates, recommend_bid_price
from ..intelligence.assistant import ask
from ..intelligence.llm_client import assistant_client
from ..intelligence.matching import best_product_match, comparable_set
from ..models import (
    Award,
    Bidder,
    Contact,
    Document,
    ExtractionLog,
    FinancialBid,
    Organisation,
    OrgUnit,
    PriceAggregate,
    PriceObservation,
    Product,
    ProductCategory,
    Source,
    Supplier,
    Tender,
    TenderLineItem,
)
from ..registry import check, sync_sources

router = APIRouter(prefix="/api/v1", tags=["api"])


# --------------------------------------------------------------------------- #
# Serialisers
# --------------------------------------------------------------------------- #
def tender_brief(t: Tender) -> dict:
    return {
        "id": t.id,
        "portal_tender_id": t.portal_tender_id,
        "tender_ref": t.tender_ref,
        "title": t.title,
        "organisation_id": t.organisation_id,
        "category_id": t.category_id,
        "state": t.state,
        "city": t.city,
        "tender_type": t.tender_type,
        "status": t.status,
        "published_at": t.published_at.isoformat() if t.published_at else None,
        "closing_at": t.closing_at.isoformat() if t.closing_at else None,
        "estimated_value": t.estimated_value,
        "estimated_value_is_published": t.estimated_value_is_published,
        "emd_amount": t.emd_amount,
        "tender_fee": t.tender_fee,
        "portal_url": t.portal_url,
        "provenance": {
            "source_id": t.source_id,
            "retrieved_at": t.retrieved_at.isoformat() if t.retrieved_at else None,
            "extraction_method": t.extraction_method,
            "confidence": t.confidence,
            "verification_state": t.verification_state,
            "last_verified_at": t.last_verified_at.isoformat() if t.last_verified_at else None,
        },
    }


def tender_detail(db: Session, t: Tender) -> dict:
    out = tender_brief(t)
    org = db.get(Organisation, t.organisation_id) if t.organisation_id else None
    unit = db.get(OrgUnit, t.org_unit_id) if t.org_unit_id else None
    category = db.get(ProductCategory, t.category_id) if t.category_id else None
    out["organisation"] = {"id": org.id, "name": org.canonical_name, "type": org.org_type,
                           "state": org.state} if org else None
    out["org_unit"] = {"id": unit.id, "name": unit.name} if unit else None
    out["category"] = {"id": category.id, "path": category.path} if category else None
    out["contacts"] = [
        {
            # official channels only; individual names are deliberately excluded
            "official_email": c.official_email,
            "official_phone": c.official_phone,
            "data_class": c.data_class,
        }
        for c in db.scalars(select(Contact).where(Contact.org_unit_id == t.org_unit_id))
    ] if t.org_unit_id else []
    out["line_items"] = [
        {
            "id": li.id, "line_no": li.line_no, "description": li.description,
            "quantity": li.quantity, "unit": li.unit, "hsn_code": li.hsn_code,
            "product_id": li.product_id, "source_offset": li.source_offset,
        }
        for li in db.scalars(select(TenderLineItem).where(TenderLineItem.tender_id == t.id))
    ]
    out["bidders"] = [
        {
            "id": b.id, "seller_name": b.seller_name_raw, "supplier_id": b.supplier_id,
            "offered_make": b.offered_make, "offered_model": b.offered_model,
            "mse_status": b.mse_status, "mii_status": b.mii_status,
            "participated_at": b.participated_at.isoformat() if b.participated_at else None,
        }
        for b in db.scalars(select(Bidder).where(Bidder.tender_id == t.id))
    ]
    ladder = []
    for fb in db.scalars(select(FinancialBid).where(FinancialBid.tender_id == t.id).order_by(FinancialBid.rank)):
        supplier = db.get(Supplier, fb.supplier_id) if fb.supplier_id else None
        ladder.append(
            {
                "rank": fb.rank, "seller_name": supplier.canonical_name if supplier else None,
                "total_price": fb.total_price, "currency": fb.currency,
                "result_url": fb.result_url,
            }
        )
    out["price_ladder"] = ladder
    award = db.scalar(select(Award).where(Award.tender_id == t.id))
    if award:
        supplier = db.get(Supplier, award.supplier_id) if award.supplier_id else None
        out["award"] = {
            "contract_value": award.contract_value,
            "awarded_quantity": award.awarded_quantity,
            "num_bids_received": award.num_bids_received,
            "award_date": award.award_date.isoformat() if award.award_date else None,
            "supplier": supplier.canonical_name if supplier else None,
            "verification_state": award.verification_state,
            "confidence": award.confidence,
        }
    else:
        out["award"] = None
    out["price_observations"] = [
        {
            "id": p.id, "basis": p.basis, "unit_price": p.unit_price,
            "normalised_unit_price": p.normalised_unit_price, "quantity": p.quantity,
            "observed_at": p.observed_at.isoformat() if p.observed_at else None,
            "confidence": p.confidence, "normalisation_notes": p.normalisation_notes,
        }
        for p in db.scalars(select(PriceObservation).where(PriceObservation.tender_id == t.id))
    ]
    out["documents"] = [
        {"id": d.id, "url": d.source_url, "mime": d.mime, "pages": d.page_count,
         "text_density": d.text_density, "ocr_used": d.ocr_used}
        for d in db.scalars(select(Document).where(Document.source_id == t.source_id).limit(5))
    ]
    return out


# --------------------------------------------------------------------------- #
# Health, sources, stats
# --------------------------------------------------------------------------- #
@router.get("/health")
def health(db: Session = Depends(get_db)):
    return {
        "status": "ok",
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tenders": db.query(Tender).count(),
        "price_observations": db.query(PriceObservation).count(),
        "products": db.query(Product).count(),
        "suppliers": db.query(Supplier).count(),
    }


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    out = []
    for s in db.scalars(select(Source).order_by(Source.key)):
        gate = check(s)
        out.append(
            {
                "key": s.key, "name": s.name, "tier": s.tier, "category": s.category,
                "enabled": s.enabled, "robots_status": s.robots_status,
                "auth_required": s.auth_required, "rate_limit_per_min": s.rate_limit_per_min,
                "redistribution_allowed": s.redistribution_allowed,
                "policy_reviewed_at": s.policy_reviewed_at.isoformat() if s.policy_reviewed_at else None,
                "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
                "consecutive_failures": s.consecutive_failures,
                "runnable": gate.ok,
                "blocked_reasons": gate.reasons,
            }
        )
    return {"sources": out, "runnable": sum(1 for s in out if s["runnable"])}


@router.post("/sources/sync")
def sync(db: Session = Depends(get_db)):
    return sync_sources(db)


@router.get("/stats/overview")
def overview(db: Session = Depends(get_db)):
    total_tenders = db.query(Tender).count()
    with_ladder = db.query(FinancialBid.tender_id).distinct().count()
    awarded = db.query(Award).count()
    ladder_pct = round(with_ladder / total_tenders * 100, 1) if total_tenders else 0.0
    by_state = [
        {"state": state, "count": count}
        for state, count in db.query(Tender.state, func.count(Tender.id))
        .group_by(Tender.state)
        .order_by(func.count(Tender.id).desc())
        .limit(8)
        .all()
    ]
    by_basis = [
        {"basis": basis, "count": count}
        for basis, count in db.query(PriceObservation.basis, func.count(PriceObservation.id))
        .group_by(PriceObservation.basis)
        .all()
    ]
    recent = list(db.scalars(select(Tender).order_by(Tender.retrieved_at.desc()).limit(6)))
    return {
        "tenders": total_tenders,
        "tenders_with_price_ladder": with_ladder,
        "ladder_coverage_pct": ladder_pct,
        "awards": awarded,
        "products": db.query(Product).count(),
        "suppliers": db.query(Supplier).count(),
        "organisations": db.query(Organisation).count(),
        "price_observations": db.query(PriceObservation).count(),
        "by_state": by_state,
        "by_basis": by_basis,
        "recent_tenders": [tender_brief(t) for t in recent],
    }


# --------------------------------------------------------------------------- #
# Tenders
# --------------------------------------------------------------------------- #
@router.get("/tenders")
def search_tenders(
    q: str | None = None,
    state: str | None = None,
    status: str | None = None,
    category_id: int | None = None,
    min_value: float | None = None,
    limit: int = Query(25, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    stmt = select(Tender)
    if q:
        terms = [t for t in q.split() if t]
        stmt = stmt.where(or_(*[Tender.title.ilike(f"%{t}%") for t in terms]) if terms else True)
    if state:
        stmt = stmt.where(Tender.state == state)
    if status:
        stmt = stmt.where(Tender.status == status)
    if category_id:
        stmt = stmt.where(Tender.category_id == category_id)
    if min_value:
        stmt = stmt.where(Tender.estimated_value >= min_value)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(db.scalars(stmt.order_by(Tender.published_at.desc().nulls_last(), Tender.id.desc())
                           .limit(limit).offset(offset)))
    return {"total": total, "limit": limit, "offset": offset,
            "results": [tender_brief(t) for t in rows]}


@router.get("/tenders/{tender_id}")
def get_tender(tender_id: int, db: Session = Depends(get_db)):
    t = db.get(Tender, tender_id)
    if t is None:
        raise HTTPException(404, "tender not found")
    return tender_detail(db, t)


# --------------------------------------------------------------------------- #
# Products and prices
# --------------------------------------------------------------------------- #
@router.get("/products")
def search_products(q: str | None = None, limit: int = 25, db: Session = Depends(get_db)):
    stmt = select(Product)
    if q:
        stmt = stmt.where(Product.canonical_name.ilike(f"%{q}%"))
    rows = list(db.scalars(stmt.order_by(Product.canonical_name).limit(limit)))
    if q and not rows:
        match = best_product_match(db, q, threshold=0.2)
        rows = [match.product] if match.product else []
    return {
        "results": [
            {
                "id": p.id, "canonical_name": p.canonical_name, "category_id": p.category_id,
                "unit": p.unit,
                "observations": db.query(PriceObservation).filter(PriceObservation.product_id == p.id).count(),
            }
            for p in rows
        ]
    }


@router.get("/products/{product_id}/prices")
def product_prices(
    product_id: int,
    basis: str = "awarded",
    state: str | None = None,
    db: Session = Depends(get_db),
):
    if basis not in {"awarded", "l1_quote", "l2_quote", "l3_quote", "estimated", "market"}:
        raise HTTPException(400, "invalid basis")
    return product_price_stats(db, product_id, basis=basis, state=state)


@router.get("/products/{product_id}/comparables")
def product_comparables(product_id: int, state: str | None = None, db: Session = Depends(get_db)):
    return comparable_set(db, product_id, state=state)


@router.post("/products/{product_id}/recommend")
def product_recommend(
    product_id: int,
    quantity: float | None = None,
    landed_cost: float | None = None,
    target_margin_pct: float = 0.15,
    competitive_stance: str = "balanced",
    state: str | None = None,
    db: Session = Depends(get_db),
):
    rec = recommend_bid_price(
        db, product_id, quantity=quantity, landed_cost=landed_cost,
        target_margin_pct=target_margin_pct, state=state, competitive_stance=competitive_stance,
    )
    return {
        "product_id": rec.product_id,
        "basis": rec.basis,
        "range": {"low": rec.range_low, "mid": rec.range_mid, "high": rec.range_high},
        "inputs": rec.inputs,
        "steps": rec.steps,
        "confidence": rec.confidence,
        "notes": rec.notes,
    }


@router.post("/aggregates/recompute")
def recompute(db: Session = Depends(get_db)):
    written = recompute_aggregates(db)
    return {"aggregates_written": written}


# --------------------------------------------------------------------------- #
# Suppliers and buyers
# --------------------------------------------------------------------------- #
@router.get("/suppliers")
def search_suppliers(q: str | None = None, limit: int = 25, db: Session = Depends(get_db)):
    stmt = select(Supplier)
    if q:
        stmt = stmt.where(Supplier.canonical_name.ilike(f"%{q}%"))
    rows = list(db.scalars(stmt.order_by(Supplier.canonical_name).limit(limit)))
    return {
        "results": [
            {
                "id": s.id, "canonical_name": s.canonical_name, "status": s.profile_status,
                "bids": db.query(FinancialBid).filter(FinancialBid.supplier_id == s.id).count(),
                "wins": db.query(FinancialBid).filter(
                    FinancialBid.supplier_id == s.id, FinancialBid.rank == 1).count(),
            }
            for s in rows
        ]
    }


@router.get("/suppliers/{supplier_id}")
def supplier_profile(supplier_id: int, db: Session = Depends(get_db)):
    s = db.get(Supplier, supplier_id)
    if s is None:
        raise HTTPException(404, "supplier not found")
    stats = entities.supplier_category_stats(db, supplier_id)
    history = []
    for fb in db.scalars(select(FinancialBid).where(FinancialBid.supplier_id == supplier_id).limit(50)):
        t = db.get(Tender, fb.tender_id)
        history.append(
            {
                "tender_id": fb.tender_id,
                "portal_tender_id": t.portal_tender_id if t else None,
                "title": t.title if t else None,
                "rank": fb.rank,
                "total_price": fb.total_price,
                "state": t.state if t else None,
                "result_url": fb.result_url,
            }
        )
    return {
        "id": s.id, "canonical_name": s.canonical_name, "status": s.profile_status,
        "merge_evidence": s.merge_evidence, "stats": stats, "history": history,
    }


@router.get("/buyers")
def list_buyers(limit: int = 25, db: Session = Depends(get_db)):
    rows = list(
        db.scalars(
            select(Organisation).order_by(Organisation.canonical_name).limit(limit)
        )
    )
    out = []
    for org in rows:
        spend = db.query(func.coalesce(func.sum(Award.contract_value), 0.0)).join(
            Tender, Tender.id == Award.tender_id
        ).filter(Tender.organisation_id == org.id).scalar()
        out.append(
            {
                "id": org.id, "name": org.canonical_name, "type": org.org_type,
                "state": org.state,
                "tenders": db.query(Tender).filter(Tender.organisation_id == org.id).count(),
                "awarded_value": float(spend or 0),
            }
        )
    return {"results": out}


@router.get("/buyers/{org_id}")
def buyer_profile(org_id: int, db: Session = Depends(get_db)):
    org = db.get(Organisation, org_id)
    if org is None:
        raise HTTPException(404, "organisation not found")
    tenders = list(db.scalars(select(Tender).where(Tender.organisation_id == org_id).limit(50)))
    categories = {}
    for t in tenders:
        if t.category_id:
            categories[t.category_id] = categories.get(t.category_id, 0) + 1
    return {
        "id": org.id, "name": org.canonical_name, "type": org.org_type, "state": org.state,
        "units": [{"id": u.id, "name": u.name} for u in db.scalars(
            select(OrgUnit).where(OrgUnit.organisation_id == org_id))],
        "tenders": [tender_brief(t) for t in tenders],
        "category_counts": categories,
    }


# --------------------------------------------------------------------------- #
# Assistant
# --------------------------------------------------------------------------- #
class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    use_llm: bool = True


@router.post("/assistant")
def assistant(req: AskRequest, db: Session = Depends(get_db)):
    client = assistant_client() if req.use_llm else None
    answer = ask(db, req.question, use_llm=req.use_llm, llm=client)
    return answer.to_dict()


# --------------------------------------------------------------------------- #
# Connectors and extraction (operator endpoints)
# --------------------------------------------------------------------------- #
@router.post("/connectors/{source_key}/self-test")
def connector_self_test(source_key: str, db: Session = Depends(get_db)):
    source = db.scalar(select(Source).where(Source.key == source_key))
    if source is None:
        raise HTTPException(404, "source not found")
    try:
        connector = build_connector(db, source)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    return connector.self_test()


@router.post("/connectors/{source_key}/run")
def connector_run(source_key: str, db: Session = Depends(get_db)):
    source = db.scalar(select(Source).where(Source.key == source_key))
    if source is None:
        raise HTTPException(404, "source not found")
    gate = check(source)
    if not gate.ok:
        raise HTTPException(403, {"refused": "compliance gate", "reasons": gate.reasons})
    try:
        connector = build_connector(db, source)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    run = connector.run()
    return {
        "run_id": run.id, "status": run.status, "pages_fetched": run.pages_fetched,
        "records_parsed": run.records_parsed, "records_rejected": run.records_rejected,
        "error_class": run.error_class, "error_detail": run.error_detail,
    }


@router.get("/extraction/{document_id}")
def extraction_log(document_id: int, db: Session = Depends(get_db)):
    logs = list(db.scalars(select(ExtractionLog).where(ExtractionLog.document_id == document_id)))
    if not logs:
        raise HTTPException(404, "no extraction log for that document")
    latest = logs[-1]
    return {
        "document_id": document_id,
        "status": latest.status,
        "schema_version": latest.schema_version,
        "fields_extracted": latest.fields_extracted,
        "fields_null": latest.fields_null,
        "fields_uncited": latest.fields_uncited,
        "validation_failures": latest.validation_failures,
        "payload": latest.payload,
    }
