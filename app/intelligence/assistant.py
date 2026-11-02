"""The AI research assistant, built around an evidence contract.

The model never supplies facts. It supplies phrasing, and only over a fact set
this module computed from database rows. Every number in the final text is
checked against that fact set; a number the verifier cannot find is treated as a
hallucination and the answer falls back to the deterministic template.

Without an API key the assistant still works: the template *is* the answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Award, FinancialBid, Organisation, PriceObservation, Product, Supplier, Tender
from .aggregates import product_price_stats, recommend_bid_price
from .matching import best_product_match

OUT_OF_SCOPE = (
    "This assistant answers questions about tenders, awarded prices, suppliers, buyers and "
    "procurement history held in this platform. I can't answer questions outside that scope."
)

INTENT_PATTERNS = {
    "recommend": ("what should our selling price", "what should we quote", "recommend a price", "bid price"),
    "price": ("price", "prices", "l1", "l2", "l3", "quoted", "awarded", "cost", "rate", "cheapest", "lowest"),
    "supplier": ("supplier", "suppliers", "vendor", "who won", "competitor", "won the contract", "winner"),
    "buyer": ("which organisations", "which organizations", "buyers", "who purchased", "who buys", "departments"),
    "aggregate": ("how many", "count", "total", "average", "trend", "history"),
}


def classify(question: str) -> str:
    q = question.lower()
    for intent, needles in INTENT_PATTERNS.items():
        if any(n in q for n in needles):
            return intent
    if any(w in q for w in ("tender", "bid", "procure", "rfp", "contract")):
        return "search"
    return "out_of_scope"


@dataclass
class Claim:
    text: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"text": self.text, "evidence_ids": self.evidence_ids}


@dataclass
class Answer:
    question: str
    intent: str
    direct_answer: str
    claims: list[Claim] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    analysis: dict | None = None
    coverage: dict = field(default_factory=dict)
    confidence: str = "none"
    declined: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "intent": self.intent,
            "direct_answer": self.direct_answer,
            "claims": [c.to_dict() for c in self.claims],
            "evidence": self.evidence,
            "analysis": self.analysis,
            "coverage": self.coverage,
            "confidence": self.confidence,
            "declined": self.declined,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# Numeric verification
# --------------------------------------------------------------------------- #
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for raw in NUM_RE.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def verify_numeric_claims(text: str, allowed: set[float], *, rel_tolerance: float = 0.01) -> list[float]:
    """Return the numbers in `text` that are NOT supported by `allowed`.

    An empty list means every figure the model wrote is traceable to a computed
    fact. This is the guard that stops a fluent paragraph from inventing a price.
    """
    bad = []
    for value in numbers_in(text):
        if any(
            abs(value - a) <= max(rel_tolerance * max(abs(a), 1.0), 0.01) for a in allowed
        ):
            continue
        bad.append(value)
    return bad


# --------------------------------------------------------------------------- #
# Question answering
# --------------------------------------------------------------------------- #
def _evidence_from_observations(db: Session, rows: list[PriceObservation], limit: int = 25) -> list[dict]:
    out = []
    for r in rows[:limit]:
        tender = db.get(Tender, r.tender_id)
        supplier = db.get(Supplier, r.supplier_id) if r.supplier_id else None
        out.append(
            {
                "type": "price_observation",
                "id": r.id,
                "tender_id": r.tender_id,
                "portal_tender_id": tender.portal_tender_id if tender else None,
                "title": tender.title if tender else None,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                "basis": r.basis,
                "unit_price": r.unit_price,
                "normalised_unit_price": r.normalised_unit_price,
                "quantity": r.quantity,
                "state": r.state,
                "supplier": supplier.canonical_name if supplier else None,
                "source_url": tender.portal_url if tender else None,
                "confidence": r.confidence,
            }
        )
    return out


def _supplier_evidence(db: Session, supplier: Supplier, limit: int = 20) -> list[dict]:
    rows = list(
        db.scalars(
            select(FinancialBid).where(FinancialBid.supplier_id == supplier.id).order_by(FinancialBid.rank)
        )
    )
    out = []
    for fb in rows[:limit]:
        tender = db.get(Tender, fb.tender_id)
        out.append(
            {
                "type": "financial_bid",
                "id": fb.id,
                "tender_id": fb.tender_id,
                "portal_tender_id": tender.portal_tender_id if tender else None,
                "title": tender.title if tender else None,
                "rank": fb.rank,
                "total_price": fb.total_price,
                "state": tender.state if tender else None,
                "source_url": fb.result_url,
            }
        )
    return out


def ask(db: Session, question: str, *, use_llm: bool = True, llm=None) -> Answer:
    intent = classify(question)
    if intent == "out_of_scope":
        return Answer(question=question, intent=intent, direct_answer=OUT_OF_SCOPE,
                      confidence="none", declined=True)

    match = best_product_match(db, question, threshold=0.30)
    product = match.product

    if intent == "recommend":
        return _recommend(db, question, product, match)

    if product is None:
        return _fallback_search(db, question, intent)

    if intent in {"price", "aggregate"}:
        return _price_answer(db, question, product, match, use_llm=use_llm, llm=llm)
    if intent == "supplier":
        return _supplier_answer(db, question, product, match)
    if intent == "buyer":
        return _buyer_answer(db, question, product, match)
    return _fallback_search(db, question, intent)


def _price_answer(db: Session, question: str, product: Product, match, *, use_llm: bool, llm) -> Answer:
    stats = product_price_stats(db, product.id, basis="awarded")
    l1 = product_price_stats(db, product.id, basis="l1_quote")
    rows = list(
        db.scalars(
            select(PriceObservation)
            .where(PriceObservation.product_id == product.id, PriceObservation.basis == "awarded")
            .order_by(PriceObservation.observed_at.desc())
        )
    )
    evidence = _evidence_from_observations(db, rows)
    n = stats.get("n", 0)
    notes: list[str] = []
    if n == 0:
        return Answer(
            question=question,
            intent="price",
            direct_answer=(
                f"I hold no awarded-price records for \"{product.canonical_name}\", so I can't give you "
                "a price for it. I won't estimate one."
            ),
            evidence=[],
            coverage={"product": product.canonical_name, "n": 0, "matched_by": match.method},
            confidence="none",
            declined=True,
            notes=["no observations on the 'awarded' basis for this product"],
        )

    if stats["tier"] == "records_only":
        notes.append(
            f"only {n} observation(s): showing the individual records rather than a statistic"
        )
    median = stats.get("median")
    claims = [
        Claim(
            text=(
                f"{n} awarded record(s) for {product.canonical_name}; "
                f"median normalised unit price ₹{median:,.0f}"
                if median is not None
                else f"{n} awarded record(s) for {product.canonical_name}"
            ),
            evidence_ids=[f"price_observation:{e['id']}" for e in evidence[:5]],
        )
    ]
    if stats.get("tier") == "distribution":
        claims.append(
            Claim(
                text=(
                    f"the middle 80% of observations fall between ₹{stats['p10']:,.0f} and "
                    f"₹{stats['p90']:,.0f}"
                ),
                evidence_ids=[f"price_observation:{e['id']}" for e in evidence[:5]],
            )
        )
    if l1.get("n"):
        claims.append(
            Claim(
                text=(
                    f"{l1['n']} L1 quote(s) on the same product have a median of ₹{l1['median']:,.0f}"
                ),
                evidence_ids=[],
            )
        )

    direct = claims[0].text + "."
    allowed = {
        float(v)
        for v in [n, median, stats.get("min"), stats.get("max"), stats.get("p10"), stats.get("p90"),
                  l1.get("n"), l1.get("median")]
        if v is not None
    }
    if use_llm and llm is not None and getattr(llm, "available", False):
        phrased = _phrase(llm, question, [c.text for c in claims], allowed)
        if phrased:
            direct = phrased

    return Answer(
        question=question,
        intent="price",
        direct_answer=direct,
        claims=claims,
        evidence=evidence,
        analysis={
            "statistics": {k: v for k, v in stats.items() if k not in {"observations", "values"}},
            "l1_statistics": {k: v for k, v in l1.items() if k not in {"observations", "values"}},
            "normalisation_note": (
                "prices are normalised per unit; each observation records the factors applied "
                "and how many of them were estimated rather than observed"
            ),
        },
        coverage={
            "product": product.canonical_name,
            "match_score": match.score,
            "n": n,
            "basis": "awarded",
            "coverage_pct": stats.get("coverage_pct"),
            "tier": stats.get("tier"),
        },
        confidence="high" if stats.get("tier") == "distribution" else ("medium" if n >= 3 else "low"),
        notes=notes,
    )


def _supplier_answer(db: Session, question: str, product: Product, match) -> Answer:
    fb_rows = list(
        db.scalars(
            select(FinancialBid)
            .join(Tender, Tender.id == FinancialBid.tender_id)
            .where(Tender.title.ilike(f"%{product.canonical_name.split()[0]}%"))
        )
    )
    supplier_ids = {r.supplier_id for r in fb_rows if r.supplier_id}
    counts: dict[int, dict] = {}
    for r in fb_rows:
        if not r.supplier_id:
            continue
        entry = counts.setdefault(r.supplier_id, {"bids": 0, "wins": 0})
        entry["bids"] += 1
        if r.rank == 1:
            entry["wins"] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["bids"]))
    evidence = []
    claims = []
    for supplier_id, stat in ranked[:5]:
        supplier = db.get(Supplier, supplier_id)
        if not supplier:
            continue
        evidence.extend(_supplier_evidence(db, supplier, limit=5))
        claims.append(
            Claim(
                text=f"{supplier.canonical_name}: {stat['wins']} win(s) from {stat['bids']} bid(s)",
                evidence_ids=[f"financial_bid:{e['id']}" for e in evidence[-5:]],
            )
        )
    if not claims:
        return Answer(
            question=question,
            intent="supplier",
            direct_answer=f"I hold no bid records for {product.canonical_name}.",
            declined=True,
            confidence="none",
            coverage={"product": product.canonical_name, "suppliers": 0},
        )
    return Answer(
        question=question,
        intent="supplier",
        direct_answer=claims[0].text + ".",
        claims=claims,
        evidence=evidence,
        coverage={"product": product.canonical_name, "suppliers": len(supplier_ids)},
        confidence="medium",
    )


def _buyer_answer(db: Session, question: str, product: Product, match) -> Answer:
    rows = list(
        db.scalars(select(PriceObservation).where(PriceObservation.product_id == product.id))
    )
    by_org: dict[int, int] = {}
    for r in rows:
        if r.organisation_id:
            by_org[r.organisation_id] = by_org.get(r.organisation_id, 0) + 1
    evidence, claims = [], []
    for org_id, count in sorted(by_org.items(), key=lambda kv: -kv[1])[:10]:
        org = db.get(Organisation, org_id)
        if not org:
            continue
        evidence.append({"type": "organisation", "id": org.id, "name": org.canonical_name,
                         "state": org.state, "records": count})
        claims.append(Claim(text=f"{org.canonical_name} ({count} record(s))",
                            evidence_ids=[f"organisation:{org.id}"]))
    if not claims:
        return Answer(question=question, intent="buyer", declined=True, confidence="none",
                      direct_answer=f"I hold no purchase records for {product.canonical_name}.",
                      coverage={"product": product.canonical_name, "buyers": 0})
    return Answer(
        question=question,
        intent="buyer",
        direct_answer=f"{len(by_org)} buying organisation(s) appear in the records for "
                      f"{product.canonical_name}; the most frequent is {claims[0].text.split(' (')[0]}.",
        claims=claims,
        evidence=evidence,
        coverage={"product": product.canonical_name, "buyers": len(by_org)},
        confidence="medium",
    )


def _recommend(db: Session, question: str, product: Product | None, match) -> Answer:
    if product is None:
        return Answer(question=question, intent="recommend", declined=True, confidence="none",
                      direct_answer="I can't recommend a price without identifying the product. "
                                   "Name the item, or search for it first.")
    rec = recommend_bid_price(db, product.id)
    if rec.range_mid is None:
        return Answer(question=question, intent="recommend", declined=True, confidence="none",
                      direct_answer=f"I hold no awarded prices for {product.canonical_name}, so I "
                                    "can't recommend a price for it.",
                      notes=rec.notes)
    return Answer(
        question=question,
        intent="recommend",
        direct_answer=(
            f"Recommended range for {product.canonical_name}: ₹{rec.range_low:,.0f} to "
            f"₹{rec.range_high:,.0f} per unit (midpoint ₹{rec.range_mid:,.0f}). "
            f"Based on {rec.inputs['n_observations']} awarded record(s); confidence {rec.confidence}."
        ),
        claims=[
            Claim(text=f"range_low={rec.range_low}", evidence_ids=[]),
            Claim(text=f"range_high={rec.range_high}", evidence_ids=[]),
        ],
        analysis={"steps": rec.steps, "inputs": rec.inputs, "notes": rec.notes},
        coverage={"product": product.canonical_name, "n": rec.inputs["n_observations"]},
        confidence=rec.confidence,
        notes=rec.notes,
    )


def _fallback_search(db: Session, question: str, intent: str) -> Answer:
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", question)][:6]
    if not words:
        return Answer(question=question, intent=intent, declined=True, confidence="none",
                      direct_answer=OUT_OF_SCOPE)
    stmt = select(Tender).where(
        or_(*[Tender.title.ilike(f"%{w}%") for w in words])
    ).limit(25)
    rows = list(db.scalars(stmt))
    if not rows:
        return Answer(
            question=question,
            intent=intent,
            declined=True,
            confidence="none",
            direct_answer="I found no tenders matching that description in the corpus. "
                          "I won't guess at one.",
            coverage={"searched_terms": words, "matches": 0},
        )
    evidence = [
        {
            "type": "tender",
            "id": t.id,
            "portal_tender_id": t.portal_tender_id,
            "title": t.title,
            "state": t.state,
            "closing_at": t.closing_at.isoformat() if t.closing_at else None,
            "source_url": t.portal_url,
        }
        for t in rows
    ]
    return Answer(
        question=question,
        intent=intent,
        direct_answer=f"I found {len(rows)} tender(s) matching those terms.",
        claims=[Claim(text=f"{len(rows)} matching tender(s)",
                      evidence_ids=[f"tender:{t.id}" for t in rows[:5]])],
        evidence=evidence,
        coverage={"searched_terms": words, "matches": len(rows)},
        confidence="medium",
    )


def _phrase(llm, question: str, facts: list[str], allowed: set[float]) -> str | None:
    """Ask the model to phrase the answer, then reject it if it invented a number."""
    prompt = (
        "Rewrite the following verified facts as two short sentences of plain English.\n"
        "You may not introduce any number that is not already present in the facts.\n"
        "Do not add causes, comparisons, opinions or recommendations.\n\n"
        "Facts:\n- " + "\n- ".join(facts) + f"\n\nQuestion: {question}\nAnswer:"
    )
    try:
        body = llm.chat_json(
            [{"role": "system", "content": "You restate verified facts. You never add facts."},
             {"role": "user", "content": prompt}],
            max_tokens=220,
        )
    except Exception:
        return None
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    if verify_numeric_claims(text, allowed):
        return None  # a fabricated figure; fall back to the template
    return text.strip()
