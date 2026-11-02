"""SQLAlchemy models for the Procurement Intelligence Platform.

Design conventions (see blueprint Section 5):
  * Every row has an immutable surrogate key plus a natural key derived from the
    source, so re-ingestion is idempotent.
  * Every asserted fact carries provenance: source_id, retrieved_at,
    extraction_method, confidence, verification_state.
  * Nothing is destructively updated. Corrections supersede; history is kept.
  * Tenant-owned rows carry tenant_id and are isolated by the query layer.

Types are deliberately portable: String/Text/Integer/Float/JSON/DateTime only,
so the same schema runs on SQLite (dev, tests) and PostgreSQL (production).
Money uses Numeric with asdecimal=False so callers get plain floats.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

PRICE_BASIS = ("awarded", "l1_quote", "l2_quote", "l3_quote", "estimated", "market")
VERIFICATION_STATES = ("published", "parsed", "inferred", "unverified", "superseded")
DATA_CLASSES = ("organisational", "official_channel", "individual_identifying", "restricted")

MONEY = Numeric(18, 2, asdecimal=False)
QTY = Numeric(18, 3, asdecimal=False)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def basis_check(col: str = "basis") -> CheckConstraint:
    values = ", ".join(f"'{v}'" for v in PRICE_BASIS)
    return CheckConstraint(f"{col} IN ({values})", name=f"ck_{col}_basis")


class ProvenanceMixin:
    """Provenance columns required on every asserted fact."""

    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)
    retrieved_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    extraction_method = Column(String(64), default="manual", nullable=False)
    confidence = Column(Float, default=1.0, nullable=False)
    verification_state = Column(
        String(24),
        default="parsed",
        nullable=False,
        index=True,
    )


# --------------------------------------------------------------------------- #
# Ingestion / source registry
# --------------------------------------------------------------------------- #
class Source(Base):
    """The source registry. Drives scheduling, rate limits and legal policy.

    A connector cannot be scheduled unless its Source row has a current policy
    record (see registry.assert_runnable). This is the platform's compliance
    gate, expressed as data rather than as prose in a wiki.
    """

    __tablename__ = "sources"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, nullable=False)          # stable machine key
    name = Column(String(200), nullable=False)
    tier = Column(String(2), nullable=False)                        # A / B / C / D
    category = Column(String(64), nullable=False)
    base_url = Column(String(400), nullable=False)
    connector = Column(String(64), nullable=True)                   # python module key
    enabled = Column(Boolean, default=False, nullable=False)

    # Access policy
    auth_required = Column(Boolean, default=False, nullable=False)
    robots_status = Column(String(24), default="unknown", nullable=False)
    terms_summary = Column(Text, nullable=True)
    permitted_paths = Column(JSON, default=list)
    rate_limit_per_min = Column(Integer, default=10, nullable=False)
    egress_region = Column(String(8), default="IN")
    licence_id = Column(String(64), nullable=True)
    redistribution_allowed = Column(Boolean, default=True, nullable=False)

    # Governance
    policy_reviewed_at = Column(Date, nullable=True)
    policy_reviewed_by = Column(String(120), nullable=True)
    policy_note = Column(Text, nullable=True)

    # Schedule + health
    schedule_cron = Column(String(64), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    consecutive_failures = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    runs = relationship("IngestionRun", back_populates="source")


class IngestionRun(Base):
    """One connector execution. The health record."""

    __tablename__ = "ingestion_runs"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(24), default="running", nullable=False)  # running/ok/partial/failed
    pages_fetched = Column(Integer, default=0, nullable=False)
    records_parsed = Column(Integer, default=0, nullable=False)
    records_rejected = Column(Integer, default=0, nullable=False)
    docs_archived = Column(Integer, default=0, nullable=False)
    duplicates_skipped = Column(Integer, default=0, nullable=False)
    error_class = Column(String(64), nullable=True)                 # blocked/restructured/empty/network
    error_detail = Column(Text, nullable=True)
    metrics = Column(JSON, default=dict)

    source = relationship("Source", back_populates="runs")


class Document(Base):
    """An archived artefact, content-addressed. Archive-first: extraction runs
    against this table, never against the live web."""

    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)
    source_url = Column(String(1000), nullable=False)
    object_key = Column(String(400), nullable=False)      # path/key in the archive
    sha256 = Column(String(64), nullable=False, index=True)
    mime = Column(String(120), nullable=True)
    bytes = Column(Integer, default=0, nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    http_status = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    text_chars = Column(Integer, nullable=True)
    text_density = Column(Float, nullable=True)           # chars per page
    ocr_used = Column(Boolean, default=False, nullable=False)
    text_path = Column(String(400), nullable=True)        # extracted text sidecar

    __table_args__ = (UniqueConstraint("sha256", "source_url", name="uq_document_hash_url"),)


class DocumentVersion(Base):
    """Superseded artefacts for the same logical document (corrigenda, reissues)."""

    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    version_no = Column(Integer, default=1, nullable=False)
    sha256 = Column(String(64), nullable=False)
    captured_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    change_summary = Column(Text, nullable=True)


class ExtractionLog(Base):
    """Per-document extraction attempt and outcome."""

    __tablename__ = "extraction_logs"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    schema_version = Column(String(24), default="v1", nullable=False)
    model_id = Column(String(120), nullable=True)
    started_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(24), default="ok", nullable=False)
    fields_extracted = Column(Integer, default=0, nullable=False)
    fields_null = Column(Integer, default=0, nullable=False)
    fields_uncited = Column(Integer, default=0, nullable=False)
    validation_failures = Column(JSON, default=list)
    confidence_mean = Column(Float, nullable=True)
    cost_tokens = Column(Integer, default=0, nullable=False)
    payload = Column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
class Organisation(Base):
    """Canonical buying entity (ministry, department, PSU, local body)."""

    __tablename__ = "organisations"

    id = Column(Integer, primary_key=True)
    canonical_name = Column(String(400), nullable=False)
    normalised_name = Column(String(400), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("organisations.id"), nullable=True)
    org_type = Column(String(32), default="unknown", nullable=False)  # central/state/psu/local/defence
    state = Column(String(80), nullable=True)
    city = Column(String(120), nullable=True)
    pincode = Column(String(10), nullable=True)
    portal_codes = Column(JSON, default=dict)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    confidence = Column(Float, default=0.8, nullable=False)

    units = relationship("OrgUnit", back_populates="organisation")


class OrgUnit(Base):
    """Department / office / consignee under an organisation."""

    __tablename__ = "org_units"

    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False, index=True)
    name = Column(String(400), nullable=False)
    kind = Column(String(48), default="office", nullable=False)
    address = Column(Text, nullable=True)
    state = Column(String(80), nullable=True)
    pincode = Column(String(10), nullable=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    organisation = relationship("Organisation", back_populates="units")


class Contact(Base):
    """Official channels only. Individual-identifying fields are policy-gated."""

    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)
    org_unit_id = Column(Integer, ForeignKey("org_units.id"), nullable=False, index=True)
    designation = Column(String(200), nullable=True)
    official_email = Column(String(200), nullable=True)
    official_phone = Column(String(64), nullable=True)
    # An individual's name is stored only where inseparable from the published
    # record, and the API refuses to search or export on it.
    individual_name = Column(String(200), nullable=True)
    channel_class = Column(String(32), default="official_channel", nullable=False)
    data_class = Column(String(32), default="official_channel", nullable=False)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "data_class IN ('organisational','official_channel','individual_identifying','restricted')",
            name="ck_contact_data_class",
        ),
    )


class Supplier(Base):
    """Canonical selling entity. Never merged on name similarity alone."""

    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True)
    canonical_name = Column(String(400), nullable=False)
    normalised_name = Column(String(400), nullable=False, index=True)
    entity_type = Column(String(48), nullable=True)
    registered_state = Column(String(80), nullable=True)
    website = Column(String(400), nullable=True)
    profile_status = Column(String(24), default="candidate", nullable=False)  # candidate/verified
    merge_evidence = Column(JSON, default=dict)
    first_seen_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class SupplierIdentifier(Base):
    """Lawful identifiers only, from licensed or published sources."""

    __tablename__ = "supplier_identifiers"

    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False, index=True)
    id_type = Column(String(16), nullable=False)      # GSTIN / CIN / PAN / PORTAL_SELLER_ID
    id_value = Column(String(64), nullable=False, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    licence_id = Column(String(64), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("id_type", "id_value", name="uq_supplier_identifier"),)


class SupplierCategory(Base):
    """What a supplier actually sells, derived from bids and wins."""

    __tablename__ = "supplier_categories"

    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("product_categories.id"), nullable=False, index=True)
    bids = Column(Integer, default=0, nullable=False)
    wins = Column(Integer, default=0, nullable=False)
    total_value = Column(MONEY, default=0, nullable=False)
    last_active_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("supplier_id", "category_id", name="uq_supplier_category"),)


class ProductCategory(Base):
    """Internal taxonomy tree. Source vocabularies map onto it; HSN cross-refs."""

    __tablename__ = "product_categories"

    id = Column(Integer, primary_key=True)
    parent_id = Column(Integer, ForeignKey("product_categories.id"), nullable=True)
    name = Column(String(200), nullable=False)
    path = Column(String(600), nullable=False, index=True)
    hsn_codes = Column(JSON, default=list)
    source_category_map = Column(JSON, default=dict)


class Product(Base):
    """Canonical product / service family."""

    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("product_categories.id"), nullable=False, index=True)
    canonical_name = Column(String(400), nullable=False)
    brand = Column(String(120), nullable=True)
    model = Column(String(160), nullable=True)
    unit = Column(String(32), default="unit", nullable=False)
    spec_summary = Column(Text, nullable=True)
    search_tokens = Column(Text, nullable=True)     # normalised token bag for matching
    status = Column(String(24), default="canonical", nullable=False)


class ProductAttribute(Base):
    """Normalised specification attributes used by the comparable-set matcher."""

    __tablename__ = "product_attributes"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    attr_key = Column(String(80), nullable=False, index=True)
    attr_value = Column(String(400), nullable=True)
    attr_unit = Column(String(32), nullable=True)
    attr_numeric = Column(Float, nullable=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    confidence = Column(Float, default=0.8, nullable=False)


class ProductAlias(Base):
    """Observed description text mapped to a canonical product."""

    __tablename__ = "product_aliases"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    alias_text = Column(Text, nullable=False)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    match_score = Column(Float, default=0.0, nullable=False)
    match_explanation = Column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# Tenders and the price ladder
# --------------------------------------------------------------------------- #
class Tender(Base, ProvenanceMixin):
    """One row per tender notice."""

    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True)
    portal_tender_id = Column(String(120), nullable=False, index=True)
    tender_ref = Column(String(200), nullable=True, index=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    org_unit_id = Column(Integer, ForeignKey("org_units.id"), nullable=True)
    category_id = Column(Integer, ForeignKey("product_categories.id"), nullable=True, index=True)
    tender_type = Column(String(16), default="goods", nullable=False)  # goods/works/services
    state = Column(String(80), nullable=True, index=True)
    city = Column(String(120), nullable=True)
    pincode = Column(String(10), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True, index=True)
    closing_at = Column(DateTime(timezone=True), nullable=True, index=True)
    opening_at = Column(DateTime(timezone=True), nullable=True)
    estimated_value = Column(MONEY, nullable=True)
    estimated_value_is_published = Column(Boolean, default=False, nullable=False)
    emd_amount = Column(MONEY, nullable=True)
    tender_fee = Column(MONEY, nullable=True)
    eligibility_text = Column(Text, nullable=True)
    status = Column(String(32), default="unknown", nullable=False)  # live/closed/awarded/cancelled
    parent_tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=True)
    portal_url = Column(String(1000), nullable=True)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_id", "portal_tender_id", name="uq_tender_source_portal_id"),
        CheckConstraint("tender_type IN ('goods','works','services')", name="ck_tender_type"),
        Index("ix_tender_state_closing", "state", "closing_at"),
    )

    organisation = relationship("Organisation")
    line_items = relationship("TenderLineItem", back_populates="tender")


class TenderLineItem(Base):
    """A BOQ line — the unit of price intelligence."""

    __tablename__ = "tender_line_items"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False, index=True)
    line_no = Column(Integer, default=1, nullable=False)
    description = Column(Text, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    quantity = Column(QTY, nullable=True)
    unit = Column(String(32), nullable=True)
    hsn_code = Column(String(16), nullable=True)
    spec_text = Column(Text, nullable=True)
    estimated_unit_rate = Column(MONEY, nullable=True)
    source_offset = Column(String(120), nullable=True)   # page/line citation in the source doc

    tender = relationship("Tender", back_populates="line_items")


class ProductMatch(Base):
    """Explainable link between a line item and a canonical product."""

    __tablename__ = "product_matches"

    id = Column(Integer, primary_key=True)
    line_item_id = Column(Integer, ForeignKey("tender_line_items.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    score = Column(Float, nullable=False)
    method = Column(String(32), default="attribute", nullable=False)  # exact/attribute/embedding
    explanation = Column(JSON, default=dict)
    reviewed_by = Column(String(120), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("line_item_id", "product_id", name="uq_line_product"),)


class TenderDocument(Base):
    """Documents belonging to a tender, with type classification."""

    __tablename__ = "tender_documents"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    doc_type = Column(String(32), default="other", nullable=False)  # NIT/BOQ/spec/corrigendum/AOC/contract
    label = Column(String(300), nullable=True)


class Bidder(Base):
    """Participation record — who turned up."""

    __tablename__ = "bidders"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    seller_name_raw = Column(String(400), nullable=False)
    offered_make = Column(String(200), nullable=True)
    offered_model = Column(String(200), nullable=True)
    participated_at = Column(DateTime(timezone=True), nullable=True)
    emd_status = Column(String(120), nullable=True)
    mse_status = Column(String(120), nullable=True)
    mii_status = Column(String(120), nullable=True)
    source_offset = Column(String(120), nullable=True)

    __table_args__ = (UniqueConstraint("tender_id", "seller_name_raw", name="uq_bidder_tender_seller"),)


class TechnicalResult(Base):
    """Technical evaluation outcome per bidder."""

    __tablename__ = "technical_results"

    id = Column(Integer, primary_key=True)
    bidder_id = Column(Integer, ForeignKey("bidders.id"), nullable=False, index=True)
    status = Column(String(32), nullable=False)   # qualified/disqualified/not_evaluated
    reason_text = Column(Text, nullable=True)
    source_offset = Column(String(120), nullable=True)


class FinancialBid(Base):
    """The price ladder. The most valuable table in the system."""

    __tablename__ = "financial_bids"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False, index=True)
    bidder_id = Column(Integer, ForeignKey("bidders.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    rank = Column(Integer, nullable=False, index=True)      # 1 = L1
    total_price = Column(MONEY, nullable=False)
    currency = Column(String(3), default="INR", nullable=False)
    price_is_gst_inclusive = Column(Boolean, nullable=True)
    item_wise_prices = Column(JSON, default=list)
    result_url = Column(String(1000), nullable=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    confidence = Column(Float, default=0.95, nullable=False)

    __table_args__ = (UniqueConstraint("tender_id", "rank", name="uq_financial_tender_rank"),)


class Award(Base):
    """Award of contract — distinct from the bid result."""

    __tablename__ = "awards"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    award_date = Column(Date, nullable=True)
    contract_value = Column(MONEY, nullable=True)
    currency = Column(String(3), default="INR", nullable=False)
    awarded_quantity = Column(QTY, nullable=True)
    num_bids_received = Column(Integer, nullable=True)
    completion_period_days = Column(Integer, nullable=True)
    aoc_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    retrieved_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    verification_state = Column(String(24), default="parsed", nullable=False)
    confidence = Column(Float, default=0.9, nullable=False)

    __table_args__ = (UniqueConstraint("tender_id", name="uq_award_tender"),)


class PurchaseOrder(Base):
    """Where published. Low coverage by design."""

    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True)
    award_id = Column(Integer, ForeignKey("awards.id"), nullable=False, index=True)
    po_number = Column(String(120), nullable=True)
    po_date = Column(Date, nullable=True)
    po_value = Column(MONEY, nullable=True)
    consignee = Column(Text, nullable=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)


class PriceObservation(Base):
    """Denormalised price fact table. `basis` is what stops an estimated value
    from ever being aggregated with an awarded price."""

    __tablename__ = "price_observations"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=False, index=True)
    line_item_id = Column(Integer, ForeignKey("tender_line_items.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=True, index=True)
    state = Column(String(80), nullable=True, index=True)
    observed_at = Column(Date, nullable=False, index=True)
    basis = Column(String(16), nullable=False, index=True)
    unit_price = Column(MONEY, nullable=False)
    quantity = Column(QTY, nullable=True)
    currency = Column(String(3), default="INR", nullable=False)
    gst_treatment = Column(String(16), nullable=True)     # inclusive/exclusive/unknown
    normalised_unit_price = Column(MONEY, nullable=True)
    normalisation_notes = Column(JSON, default=dict)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    confidence = Column(Float, default=0.9, nullable=False)

    __table_args__ = (
        basis_check(),
        UniqueConstraint("tender_id", "line_item_id", "basis", "supplier_id", name="uq_price_obs"),
        Index("ix_price_product_basis_date", "product_id", "basis", "observed_at"),
    )


class PriceAggregate(Base):
    """Precomputed statistics with explicit coverage. Never shown without n."""

    __tablename__ = "price_aggregates"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    state = Column(String(80), nullable=True)
    quarter = Column(String(8), nullable=True)       # 2025-Q3
    basis = Column(String(16), nullable=False)
    n_observations = Column(Integer, default=0, nullable=False)
    p10 = Column(MONEY, nullable=True)
    median = Column(MONEY, nullable=True)
    p90 = Column(MONEY, nullable=True)
    minimum = Column(MONEY, nullable=True)
    maximum = Column(MONEY, nullable=True)
    mean = Column(MONEY, nullable=True)
    trend_pct = Column(Float, nullable=True)
    coverage_pct = Column(Float, nullable=True)
    computed_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("product_id", "state", "quarter", "basis", name="uq_price_agg"),
    )


class MarketPrice(Base):
    """Non-procurement market reference prices. Kept strictly separate."""

    __tablename__ = "market_prices"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    vendor = Column(String(200), nullable=False)
    price = Column(MONEY, nullable=False)
    currency = Column(String(3), default="INR", nullable=False)
    captured_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    source_kind = Column(String(48), nullable=True)
    url = Column(String(1000), nullable=True)


# --------------------------------------------------------------------------- #
# Tenancy, users, alerts
# --------------------------------------------------------------------------- #
class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)
    plan = Column(String(32), default="internal", nullable=False)
    seats = Column(Integer, default=5, nullable=False)
    api_quota = Column(Integer, default=10000, nullable=False)
    status = Column(String(24), default="active", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    email = Column(String(200), nullable=False)
    name = Column(String(200), nullable=True)
    status = Column(String(24), default="active", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    name = Column(String(64), nullable=False)
    permissions = Column(JSON, default=list)


class UserRole(Base):
    __tablename__ = "user_roles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(200), nullable=False)
    query = Column(Text, nullable=True)
    filters = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    saved_search_id = Column(Integer, ForeignKey("saved_searches.id"), nullable=False)
    channel = Column(String(32), default="email", nullable=False)
    frequency = Column(String(24), default="daily", nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_match_count = Column(Integer, default=0, nullable=False)


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id = Column(Integer, primary_key=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), nullable=False, index=True)
    sent_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    channel = Column(String(32), nullable=False)
    item_count = Column(Integer, default=0, nullable=False)
    status = Column(String(24), default="sent", nullable=False)


class UserQuotation(Base):
    """A tenant's own supplier quotations — private, never merged into public data."""

    __tablename__ = "user_quotations"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    quoted_price = Column(MONEY, nullable=False)
    currency = Column(String(3), default="INR", nullable=False)
    terms = Column(JSON, default=dict)
    valid_until = Column(Date, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class AuditLog(Base):
    """Append-only record of access and change."""

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False)
    object_type = Column(String(64), nullable=True)
    object_id = Column(String(64), nullable=True)
    ip = Column(String(64), nullable=True)
    at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    detail = Column(JSON, default=dict)
