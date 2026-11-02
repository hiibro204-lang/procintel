"""Price statistics, coverage discipline and the bid recommendation engine.

Reporting rules (blueprint Section 10.3), enforced here rather than in the UI:
  * fewer than min_n_for_stats observations  -> show the records, no statistic
  * min_n_for_stats .. min_n_for_distribution -> min/median/max + small-sample warning
  * >= min_n_for_distribution                 -> percentiles and trend
  * never mix bases in one statistic
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PriceAggregate, PriceObservation, Product

TIER_RECORDS_ONLY = "records_only"
TIER_SUMMARY = "summary"
TIER_DISTRIBUTION = "distribution"


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (no numpy dependency)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def report_tier(n: int) -> str:
    if n < settings.min_n_for_stats:
        return TIER_RECORDS_ONLY
    if n < settings.min_n_for_distribution:
        return TIER_SUMMARY
    return TIER_DISTRIBUTION


def describe(values: list[float]) -> dict:
    """Descriptive statistics with the sample size attached to everything."""
    clean = [float(v) for v in values if v is not None]
    n = len(clean)
    tier = report_tier(n)
    out: dict = {"n": n, "tier": tier, "values": clean}
    if n == 0:
        out["message"] = "no comparable observations"
        return out
    out["min"] = round(min(clean), 2)
    out["max"] = round(max(clean), 2)
    out["median"] = round(statistics.median(clean), 2)
    if tier == TIER_SUMMARY:
        out["warning"] = f"small sample (n={n}); treat as indicative only"
    if tier == TIER_DISTRIBUTION:
        out["p10"] = round(percentile(clean, 10), 2)
        out["p90"] = round(percentile(clean, 90), 2)
        out["mean"] = round(statistics.fmean(clean), 2)
        out["spread_pct"] = round((out["p90"] - out["p10"]) / out["median"] * 100, 1) if out["median"] else None
    return out


def coverage_pct(db: Session, product_id: int, basis: str) -> float | None:
    """Share of observations for this product whose price could be normalised."""
    total = db.query(PriceObservation).filter(
        PriceObservation.product_id == product_id, PriceObservation.basis == basis
    ).count()
    if not total:
        return None
    normalised = db.query(PriceObservation).filter(
        PriceObservation.product_id == product_id,
        PriceObservation.basis == basis,
        PriceObservation.normalised_unit_price.is_not(None),
    ).count()
    return round(normalised / total * 100, 1)


def product_price_stats(
    db: Session,
    product_id: int,
    *,
    basis: str = "awarded",
    state: str | None = None,
    since: dt.date | None = None,
) -> dict:
    """Price statistics for one product and one basis. Bases are never blended."""
    product = db.get(Product, product_id)
    if product is None:
        return {"error": "product not found", "product_id": product_id}

    stmt = select(PriceObservation).where(
        PriceObservation.product_id == product_id,
        PriceObservation.basis == basis,
        PriceObservation.normalised_unit_price.is_not(None),
    )
    if state:
        stmt = stmt.where(PriceObservation.state == state)
    if since:
        stmt = stmt.where(PriceObservation.observed_at >= since)
    rows = list(db.scalars(stmt.order_by(PriceObservation.observed_at.desc())))

    stats = describe([r.normalised_unit_price for r in rows])
    stats.update(
        {
            "product_id": product_id,
            "product": product.canonical_name,
            "basis": basis,
            "state": state,
            "coverage_pct": coverage_pct(db, product_id, basis),
            "observations": [
                {
                    "id": r.id,
                    "tender_id": r.tender_id,
                    "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                    "unit_price": r.unit_price,
                    "normalised_unit_price": r.normalised_unit_price,
                    "quantity": r.quantity,
                    "state": r.state,
                    "confidence": r.confidence,
                }
                for r in rows[:100]
            ],
        }
    )
    stats.pop("values", None)
    return stats


def quarter_of(d: dt.date | None) -> str | None:
    if not d:
        return None
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def recompute_aggregates(db: Session, product_id: int | None = None) -> int:
    """Rebuild price_aggregates for one product or all of them."""
    stmt = select(Product.id)
    if product_id:
        stmt = stmt.where(Product.id == product_id)
    products = [p for p in db.scalars(stmt)]
    written = 0
    for pid in products:
        rows = list(db.scalars(select(PriceObservation).where(PriceObservation.product_id == pid)))
        if not rows:
            continue
        buckets: dict[tuple, list[PriceObservation]] = {}
        for r in rows:
            key = (r.state, quarter_of(r.observed_at), r.basis)
            buckets.setdefault(key, []).append(r)
        for (state, quarter, basis), bucket in buckets.items():
            values = [r.normalised_unit_price for r in bucket if r.normalised_unit_price]
            if not values:
                continue
            stats = describe(values)
            existing = db.scalar(
                select(PriceAggregate).where(
                    PriceAggregate.product_id == pid,
                    PriceAggregate.state == state,
                    PriceAggregate.quarter == quarter,
                    PriceAggregate.basis == basis,
                )
            )
            agg = existing or PriceAggregate(product_id=pid, state=state, quarter=quarter, basis=basis)
            agg.n_observations = stats["n"]
            agg.minimum = stats.get("min")
            agg.maximum = stats.get("max")
            agg.median = stats.get("median")
            agg.p10 = stats.get("p10")
            agg.p90 = stats.get("p90")
            agg.mean = stats.get("mean")
            agg.coverage_pct = coverage_pct(db, pid, basis)
            agg.computed_at = dt.datetime.now(dt.timezone.utc)
            db.add(agg)
            written += 1
    db.commit()
    return written


@dataclass
class BidRecommendation:
    product_id: int
    quantity: float | None
    basis: str
    inputs: dict
    range_low: float | None
    range_mid: float | None
    range_high: float | None
    steps: list[dict]
    confidence: str
    notes: list[str]


def recommend_bid_price(
    db: Session,
    product_id: int,
    *,
    quantity: float | None = None,
    landed_cost: float | None = None,
    target_margin_pct: float = 0.15,
    state: str | None = None,
    competitive_stance: str = "balanced",
) -> BidRecommendation:
    """Transparent recommended price range.

    Every input is returned with the answer, and every step is shown, because a
    recommendation a procurement head cannot audit is a recommendation they will
    not use (blueprint Section 4.7).
    """
    stats = product_price_stats(db, product_id, basis="awarded", state=state)
    notes: list[str] = []
    steps: list[dict] = []
    n = stats.get("n", 0)
    tier = stats.get("tier", TIER_RECORDS_ONLY)

    stance_factor = {"aggressive": 0.94, "balanced": 1.0, "defensive": 1.06}.get(competitive_stance, 1.0)
    steps.append(
        {"step": "competitive_stance", "detail": competitive_stance, "factor": stance_factor}
    )

    if n == 0:
        notes.append("no awarded observations for this product; recommendation not available")
        return BidRecommendation(
            product_id, quantity, "awarded", {"landed_cost": landed_cost},
            None, None, None, steps, "none", notes
        )

    median = stats.get("median")
    low_obs = stats.get("p10", stats.get("min"))
    high_obs = stats.get("p90", stats.get("max"))
    if tier == TIER_RECORDS_ONLY:
        notes.append(f"only {n} observation(s); showing the observed range, not a modelled range")
        low = min(stats.get("min", median), stats.get("max", median))
        high = max(stats.get("min", median), stats.get("max", median))
        confidence = "low"
    else:
        low, high = low_obs, high_obs
        confidence = "medium" if tier == TIER_SUMMARY else "high"
        if tier == TIER_SUMMARY:
            notes.append(f"small sample (n={n}); range is indicative")

    steps.append(
        {
            "step": "historical_band",
            "detail": f"n={n}, median={median}, low={low}, high={high}",
            "basis": "awarded",
        }
    )

    range_low = round(low * stance_factor, 2)
    range_mid = round(median * stance_factor, 2)
    range_high = round(high * stance_factor, 2)

    if landed_cost:
        floor = landed_cost * (1 + target_margin_pct)
        steps.append(
            {
                "step": "margin_floor",
                "detail": f"landed cost {landed_cost:,.0f} × (1 + {target_margin_pct:.0%})",
                "value": round(floor, 2),
            }
        )
        if floor > range_high:
            notes.append(
                "your margin floor is above the entire historical band: either the cost base is "
                "uncompetitive or the specification differs from the historical comparables"
            )
        range_low = max(range_low, round(floor, 2))
        range_mid = max(range_mid, round(floor, 2))

    return BidRecommendation(
        product_id=product_id,
        quantity=quantity,
        basis="awarded",
        inputs={
            "landed_cost": landed_cost,
            "target_margin_pct": target_margin_pct,
            "state": state,
            "competitive_stance": competitive_stance,
            "n_observations": n,
            "coverage_pct": stats.get("coverage_pct"),
        },
        range_low=range_low,
        range_mid=range_mid,
        range_high=range_high,
        steps=steps,
        confidence=confidence,
        notes=notes,
    )
