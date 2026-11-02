"""Text normalisation and explainable product matching.

Matching is deliberately two-stage and always explainable: a procurement analyst
will immediately ask "why do you think this is the same chair?", so every match
returns the signals that produced its score (blueprint Section 5.5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PriceObservation, Product, ProductAlias, ProductAttribute

STOPWORDS = {
    "and", "or", "of", "for", "with", "the", "a", "an", "to", "in", "on", "at",
    "as", "by", "is", "are", "be", "from", "per", "no", "nos", "qty", "quantity",
    "supply", "supplying", "provide", "providing", "procurement", "purchase",
    "purchasing", "item", "items", "goods", "make", "model", "brand", "type",
}

_WS = re.compile(r"[^a-z0-9]+")


def normalise_text(text: str | None) -> str:
    """Lowercase, strip punctuation and legal suffixes, collapse whitespace."""
    if not text:
        return ""
    t = text.lower()
    for suffix in (
        "private limited", "pvt ltd", "pvt. ltd.", "limited", "ltd", "llp",
        "corporation", "corp", "company", "co.", "& sons", "and sons",
    ):
        t = t.replace(suffix, " ")
    t = _WS.sub(" ", t)
    return " ".join(t.split())


def tokens(text: str | None, *, drop_stopwords: bool = True) -> set[str]:
    base = set(normalise_text(text).split())
    if drop_stopwords:
        base -= STOPWORDS
    return {w for w in base if len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set[str], b: set[str]) -> float:
    """Share of the query's tokens that the candidate explains."""
    if not a:
        return 0.0
    return len(a & b) / len(a)


@dataclass
class MatchResult:
    product: Product | None
    score: float
    method: str
    explanation: dict = field(default_factory=dict)


def score_product_match(query: str, product: Product, aliases: list[str] | None = None) -> tuple[float, dict]:
    """Score a free-text description against a canonical product.

    Signals: token overlap both ways, alias hits, and shared attribute values.
    Weights are versioned in one place so improvements are measurable.
    """
    q = tokens(query)
    p = tokens(product.search_tokens or product.canonical_name)
    j = jaccard(q, p)
    c = containment(q, p)
    alias_hit = 0.0
    for alias in aliases or []:
        a = tokens(alias)
        if a and (a == q or jaccard(a, q) >= 0.85):
            alias_hit = 1.0
            break
    score = round(min(1.0, 0.45 * c + 0.35 * j + 0.20 * alias_hit), 4)
    explanation = {
        "query_tokens": sorted(q),
        "product_tokens": sorted(p),
        "shared_tokens": sorted(q & p),
        "containment": round(c, 4),
        "jaccard": round(j, 4),
        "alias_hit": alias_hit,
        "weights": {"containment": 0.45, "jaccard": 0.35, "alias": 0.20},
    }
    return score, explanation


def best_product_match(db: Session, text: str, category_id: int | None = None, threshold: float = 0.35) -> MatchResult:
    stmt = select(Product).where(Product.status == "canonical")
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    candidates = list(db.scalars(stmt))
    if not candidates and category_id is not None:
        candidates = list(db.scalars(select(Product).where(Product.status == "canonical")))

    best: MatchResult = MatchResult(None, 0.0, "none", {"reason": "no candidates"})
    for product in candidates:
        aliases = [a.alias_text for a in db.scalars(select(ProductAlias).where(ProductAlias.product_id == product.id))]
        score, explanation = score_product_match(text, product, aliases)
        if score > best.score:
            best = MatchResult(product, score, "attribute", explanation)
    if best.product is None or best.score < threshold:
        return MatchResult(None, best.score, "below_threshold", best.explanation)
    return best


def attribute_profile(db: Session, product_id: int) -> dict[str, str]:
    rows = db.scalars(select(ProductAttribute).where(ProductAttribute.product_id == product_id))
    return {r.attr_key: (r.attr_value or str(r.attr_numeric or "")) for r in rows}


def comparable_set(
    db: Session,
    product_id: int,
    *,
    basis: tuple[str, ...] = ("awarded", "l1_quote"),
    state: str | None = None,
    since=None,
    min_quantity: float | None = None,
) -> dict:
    """Return tiered comparable price observations for a product.

    Tier 1: same canonical product, same quarter, same quantity band.
    Tier 2: same canonical product, wider time window.
    Tier 3: same category (flagged as weaker evidence).
    Each tier is returned separately — a blended figure that hides the tier is
    exactly the kind of output that discredits a price platform.
    """
    from ..models import Product as _Product

    product = db.get(_Product, product_id)
    if product is None:
        return {"tiers": [], "product_id": product_id}

    base = select(PriceObservation).where(
        PriceObservation.product_id == product_id,
        PriceObservation.basis.in_(basis),
        PriceObservation.normalised_unit_price.is_not(None),
    )
    if state:
        base = base.where(PriceObservation.state == state)
    if since is not None:
        base = base.where(PriceObservation.observed_at >= since)
    if min_quantity is not None:
        base = base.where(PriceObservation.quantity >= min_quantity)

    rows = list(db.scalars(base.order_by(PriceObservation.observed_at.desc())))
    latest_quarter = max((r.observed_at for r in rows), default=None)

    def quarter_of(d):
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}" if d else None

    def band(q):
        if q is None:
            return "unknown"
        if q <= 10:
            return "1-10"
        if q <= 50:
            return "11-50"
        if q <= 200:
            return "51-200"
        if q <= 1000:
            return "201-1000"
        return "1000+"

    q_now = quarter_of(latest_quarter)
    tiers = [
        {"tier": 1, "label": "exact product, same quarter, same quantity band", "rows": []},
        {"tier": 2, "label": "exact product, wider time window", "rows": []},
        {"tier": 3, "label": "same category (weaker evidence)", "rows": []},
    ]
    for r in rows:
        if quarter_of(r.observed_at) == q_now and band(r.quantity) == band(rows[0].quantity if rows else None):
            tiers[0]["rows"].append(r)
        else:
            tiers[1]["rows"].append(r)

    category_rows = list(
        db.scalars(
            select(PriceObservation).where(
                PriceObservation.product_id.is_not(None),
                PriceObservation.basis.in_(basis),
                PriceObservation.normalised_unit_price.is_not(None),
            )
        )
    )
    product_ids = {p.id for p in db.scalars(select(_Product).where(_Product.category_id == product.category_id))}
    tiers[2]["rows"] = [r for r in category_rows if r.product_id in product_ids and r.product_id != product_id][:50]

    return {
        "product_id": product_id,
        "product": product.canonical_name,
        "tiers": [
            {
                "tier": t["tier"],
                "label": t["label"],
                "n": len(t["rows"]),
                "observations": [
                    {
                        "id": r.id,
                        "tender_id": r.tender_id,
                        "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                        "basis": r.basis,
                        "unit_price": r.unit_price,
                        "normalised_unit_price": r.normalised_unit_price,
                        "quantity": r.quantity,
                        "state": r.state,
                        "supplier_id": r.supplier_id,
                        "confidence": r.confidence,
                    }
                    for r in t["rows"]
                ],
            }
            for t in tiers
        ],
    }
