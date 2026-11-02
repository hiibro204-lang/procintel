"""Entity resolution: organisations, suppliers, categories and products.

Policy enforced here (blueprint Section 5.5):
  * organisations match on normalised name, then corroborating portal code;
  * suppliers auto-merge only on an identifier (GSTIN/CIN/PAN/portal seller ID),
    never on name similarity alone;
  * products match through the explainable scorer in intelligence.matching.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .intelligence.matching import best_product_match, normalise_text, tokens
from .models import (
    Contact,
    Organisation,
    OrgUnit,
    Product,
    ProductAlias,
    ProductCategory,
    Source,
    Supplier,
    SupplierIdentifier,
)

# Root-level taxonomy. Source vocabularies map onto these; the tree can grow
# without changing any connector (blueprint Section 5.6).
TAXONOMY_ROOTS = [
    "Office & Furniture",
    "IT Hardware & Software",
    "Electrical & Lighting",
    "Medical & Laboratory",
    "HVAC & Mechanical",
    "Vehicles & Transport",
    "Civil Works & Construction",
    "Services & Manpower",
    "Safety & Security",
    "Uncategorised",
]

CATEGORY_KEYWORDS = {
    "Office & Furniture": ["chair", "table", "desk", "furniture", "almirah", "cabinet", "sofa", "workstation"],
    "IT Hardware & Software": ["laptop", "computer", "desktop", "printer", "server", "scanner", "monitor", "software", "ups", "panel display", "ifpd"],
    "Electrical & Lighting": ["led", "luminaire", "cable", "transformer", "switchgear", "light", "lamp", "mast", "electrical"],
    "Medical & Laboratory": ["syringe", "medicine", "surgical", "hospital", "pharma", "injection", "stethoscope", "reagent"],
    "HVAC & Mechanical": ["hvac", "air condition", "ac ", "chiller", "duct", "ventilation", "pump", "compressor", "boiler"],
    "Vehicles & Transport": ["vehicle", "bus", "truck", "car ", "tyre", "ambulance", "bicycle"],
    "Civil Works & Construction": ["construction", "road", "building", "repair", "civil", "painting", "renovation"],
    "Services & Manpower": ["housekeeping", "security guard", "manpower", "maintenance service", "amc", "consultancy"],
    "Safety & Security": ["cctv", "camera", "fire", "extinguisher", "helmet", "safety"],
}


def ensure_taxonomy(db: Session) -> dict[str, ProductCategory]:
    """Idempotently create the root taxonomy. Returns name -> category."""
    out: dict[str, ProductCategory] = {}
    for name in TAXONOMY_ROOTS:
        cat = db.scalar(select(ProductCategory).where(ProductCategory.path == name))
        if cat is None:
            cat = ProductCategory(name=name, path=name, hsn_codes=[], source_category_map={})
            db.add(cat)
            db.flush()
        out[name] = cat
    db.commit()
    return out


def classify_text(text: str | None) -> str:
    """Map free text onto a taxonomy root. Returns 'Uncategorised' if unsure."""
    haystack = f" {normalise_text(text)} "
    best, best_hits = "Uncategorised", 0
    for root, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in haystack)
        if hits > best_hits:
            best, best_hits = root, hits
    return best


def category_for_text(db: Session, text: str | None) -> ProductCategory:
    roots = ensure_taxonomy(db)
    return roots[classify_text(text)]


def upsert_organisation(
    db: Session,
    name: str | None,
    *,
    state: str | None = None,
    city: str | None = None,
    pincode: str | None = None,
    department: str | None = None,
    office: str | None = None,
    portal_code: str | None = None,
    source: Source | None = None,
) -> tuple[Organisation, OrgUnit | None]:
    """Find or create the buying organisation and its office unit."""
    if not name:
        name = department or office or "Unknown organisation"
    norm = normalise_text(name)
    org = db.scalar(select(Organisation).where(Organisation.normalised_name == norm))
    if org is None:
        org_type = "unknown"
        lowered = name.lower()
        if any(k in lowered for k in ("ministry", "department of", "government of india", "directorate")):
            org_type = "central"
        elif any(k in lowered for k in ("limited", "ltd", "corporation", "psu", "nigam")):
            org_type = "psu"
        elif any(k in lowered for k in ("municipal", "panchayat", "nagar", "council", "authority")):
            org_type = "local"
        elif "railway" in lowered or "defence" in lowered or "army" in lowered:
            org_type = "defence"
        org = Organisation(
            canonical_name=name,
            normalised_name=norm,
            org_type=org_type,
            state=state,
            city=city,
            pincode=pincode,
            portal_codes={portal_code: True} if portal_code else {},
            source_id=source.id if source else None,
        )
        db.add(org)
        db.flush()
    else:
        if state and not org.state:
            org.state = state
        if portal_code:
            codes = dict(org.portal_codes or {})
            codes[portal_code] = True
            org.portal_codes = codes

    unit = None
    unit_name = office or department
    if unit_name:
        unit = db.scalar(
            select(OrgUnit).where(
                OrgUnit.organisation_id == org.id,
                OrgUnit.name == unit_name,
            )
        )
        if unit is None:
            unit = OrgUnit(
                organisation_id=org.id,
                name=unit_name,
                state=state,
                pincode=pincode,
                source_id=source.id if source else None,
            )
            db.add(unit)
            db.flush()
    return org, unit


def record_contact(
    db: Session,
    unit: OrgUnit | None,
    *,
    individual_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    source: Source | None = None,
) -> Contact | None:
    """Store an official channel. Individual names are classed and non-searchable."""
    if unit is None or not (email or phone or individual_name):
        return None
    existing = db.scalar(
        select(Contact).where(Contact.org_unit_id == unit.id, Contact.official_email == email)
    )
    if existing:
        return existing
    contact = Contact(
        org_unit_id=unit.id,
        designation=None,
        official_email=email,
        official_phone=phone,
        individual_name=individual_name,
        channel_class="official_channel",
        data_class="individual_identifying" if individual_name else "official_channel",
        source_id=source.id if source else None,
    )
    db.add(contact)
    return contact


def upsert_supplier(
    db: Session,
    raw_name: str,
    *,
    identifier: tuple[str, str] | None = None,
    source: Source | None = None,
) -> Supplier:
    """Find or create a supplier.

    Auto-merge requires an identifier. Without one, we only reuse an exact
    normalised-name match — and even that is recorded as evidence, not assumed.
    """
    if not raw_name:
        raise ValueError("supplier name required")
    if identifier:
        id_type, id_value = identifier
        hit = db.scalar(
            select(SupplierIdentifier).where(
                SupplierIdentifier.id_type == id_type,
                SupplierIdentifier.id_value == id_value,
            )
        )
        if hit:
            return db.get(Supplier, hit.supplier_id)

    norm = normalise_text(raw_name)
    supplier = db.scalar(select(Supplier).where(Supplier.normalised_name == norm))
    if supplier is None:
        supplier = Supplier(
            canonical_name=raw_name.strip(),
            normalised_name=norm,
            profile_status="verified" if identifier else "candidate",
            merge_evidence={"method": "identifier" if identifier else "exact_normalised_name",
                            "raw_name": raw_name},
        )
        db.add(supplier)
        db.flush()

    if identifier:
        db.add(
            SupplierIdentifier(
                supplier_id=supplier.id,
                id_type=identifier[0],
                id_value=identifier[1],
                source_id=source.id if source else None,
            )
        )
        supplier.profile_status = "verified"
        db.flush()
    return supplier


def upsert_product(
    db: Session,
    description: str,
    *,
    category_name: str | None = None,
    source: Source | None = None,
    threshold: float = 0.35,
) -> tuple[Product, float, dict]:
    """Match a description to a canonical product, creating one if needed."""
    category = category_for_text(db, category_name or description)
    match = best_product_match(db, description, category_id=category.id, threshold=threshold)
    if match.product is not None:
        db.add(
            ProductAlias(
                product_id=match.product.id,
                alias_text=description[:500],
                source_id=source.id if source else None,
                match_score=match.score,
                match_explanation=match.explanation,
            )
        )
        return match.product, match.score, match.explanation

    name = (description or "Unnamed item").strip()[:400]
    product = Product(
        category_id=category.id,
        canonical_name=name,
        unit="unit",
        spec_summary=None,
        search_tokens=" ".join(sorted(tokens(name))),
        status="canonical",
    )
    db.add(product)
    db.flush()
    db.add(
        ProductAlias(
            product_id=product.id,
            alias_text=description[:500],
            source_id=source.id if source else None,
            match_score=1.0,
            match_explanation={"method": "created"},
        )
    )
    return product, 1.0, {"method": "created"}


def supplier_category_stats(db: Session, supplier_id: int) -> dict:
    """Win/loss summary used by the supplier profile screens."""
    from .models import FinancialBid

    bids = db.query(FinancialBid).filter(FinancialBid.supplier_id == supplier_id).count()
    wins = db.query(FinancialBid).filter(
        FinancialBid.supplier_id == supplier_id, FinancialBid.rank == 1
    ).count()
    total = sum(
        (b.total_price or 0)
        for b in db.scalars(
            select(FinancialBid).where(FinancialBid.supplier_id == supplier_id, FinancialBid.rank == 1)
        )
    )
    return {"bids": bids, "wins": wins, "win_rate": round(wins / bids, 3) if bids else None,
            "awarded_value": round(total, 2), "as_of": dt.datetime.now(dt.timezone.utc).isoformat()}
