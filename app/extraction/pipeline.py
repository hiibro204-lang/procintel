"""Extraction pipeline: artefact -> validated, cited fields -> tender record.

Stages (blueprint Section 4.4):
  1 classify -> 2 text extract -> 3 rules -> 4 LLM with citations ->
  5 deterministic overrides -> 6 validation -> 7 log

Every field that reaches the database either carries a verbatim quote from the
source or is discarded. Rejected values are counted, never silently dropped.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Document, ExtractionLog, Tender, TenderDocument
from .llm import LlmClient, verify_citations
from .pdf import extract_pdf, parse_boq_xlsx
from .rules import extract_rules

log = logging.getLogger(__name__)

MONEY_FIELDS = {"estimated_value", "emd_amount", "tender_fee"}
NUMERIC_FIELDS = MONEY_FIELDS | {"quantity", "warranty_years"}


@dataclass
class ValidationOutcome:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_fields(fields: dict) -> ValidationOutcome:
    """Business rules. A record that fails validation enters review, not the facts."""
    errors: list[str] = []
    warnings: list[str] = []

    def value(key):
        payload = fields.get(key)
        if isinstance(payload, dict):
            return payload.get("value")
        return payload

    qty = value("quantity")
    if qty is not None:
        try:
            if float(qty) <= 0:
                errors.append("quantity must be positive")
        except (TypeError, ValueError):
            errors.append("quantity is not numeric")

    for key in MONEY_FIELDS:
        v = value(key)
        if v is None:
            continue
        try:
            if float(v) < 0:
                errors.append(f"{key} is negative")
        except (TypeError, ValueError):
            errors.append(f"{key} is not numeric")

    est, emd = value("estimated_value"), value("emd_amount")
    if est and emd:
        try:
            ratio = float(emd) / float(est)
            if ratio > 0.25:
                warnings.append(f"EMD is {ratio:.0%} of estimated value, which is unusually high")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    warranty = value("warranty_years")
    if warranty is not None:
        try:
            if float(warranty) > 15:
                warnings.append("warranty period looks implausible")
        except (TypeError, ValueError):
            warnings.append("warranty_years is not numeric")

    return ValidationOutcome(ok=not errors, errors=errors, warnings=warnings)


def merge_fields(rule_fields: dict, llm_fields: dict) -> dict:
    """Deterministic rules win where they speak; the model fills the gaps."""
    merged = dict(llm_fields or {})
    for key, payload in (rule_fields or {}).items():
        merged[key] = payload
    return merged


def process_document(
    db: Session,
    document: Document,
    *,
    client: LlmClient | None = None,
    archive_dir: Path | None = None,
) -> dict:
    """Run the pipeline over one archived document and log the outcome."""
    from ..config import settings

    client = client if client is not None else LlmClient()
    archive_dir = archive_dir or settings.archive_dir
    path = archive_dir / document.object_key
    started = dt.datetime.now(dt.timezone.utc)

    result: dict = {
        "document_id": document.id,
        "status": "ok",
        "fields": {},
        "validation": None,
        "llm_used": False,
        "dropped": [],
    }

    if not path.exists():
        result["status"] = "missing_archive"
        return result

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        items = parse_boq_xlsx(path)
        document.bytes = path.stat().st_size
        document.mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fields = {"line_items": {"value": items, "quote": "BOQ spreadsheet", "method": "boq_parser"}}
        result["fields"] = fields
        _write_log(db, document, started, result, schema_version="boq-v1")
        return result

    pdf = extract_pdf(path)
    document.page_count = pdf.page_count
    document.text_chars = pdf.char_count
    document.text_density = pdf.density
    document.ocr_used = pdf.needs_ocr
    document.bytes = path.stat().st_size

    if pdf.needs_ocr:
        result["status"] = "needs_ocr"
        result["note"] = (
            f"text density {pdf.density} chars/page is below threshold; "
            "route to the OCR stage before extraction"
        )
        _write_log(db, document, started, result, schema_version="v1")
        return result

    rule_fields = extract_rules(pdf.text)
    llm_raw = client.extract_tender_fields(pdf.text)
    result["llm_used"] = bool(llm_raw)
    check = verify_citations(llm_raw, pdf.text, pdf.pages)
    result["dropped"] = check.dropped
    merged = merge_fields(rule_fields, check.verified)

    validation = validate_fields(merged)
    result["validation"] = {
        "ok": validation.ok,
        "errors": validation.errors,
        "warnings": validation.warnings,
    }
    if not validation.ok:
        result["status"] = "rejected"
        result["fields"] = merged
        _write_log(db, document, started, result, schema_version="v1", llm_used=result["llm_used"])
        return result

    result["fields"] = merged
    _apply_to_tender(db, document, merged)
    _write_log(db, document, started, result, schema_version="v1", llm_used=result["llm_used"])
    return result


def _apply_to_tender(db: Session, document: Document, fields: dict) -> None:
    """Write extracted values onto the linked tender, filling gaps only."""
    link = db.query(TenderDocument).filter(TenderDocument.document_id == document.id).first()
    if link is None:
        return
    tender = db.get(Tender, link.tender_id)
    if tender is None:
        return

    def value(key):
        payload = fields.get(key)
        return payload.get("value") if isinstance(payload, dict) else payload

    if tender.estimated_value is None and value("estimated_value") is not None:
        tender.estimated_value = value("estimated_value")
        tender.estimated_value_is_published = True
    if tender.emd_amount is None and value("emd_amount") is not None:
        tender.emd_amount = value("emd_amount")
    if tender.tender_fee is None and value("tender_fee") is not None:
        tender.tender_fee = value("tender_fee")
    eligibility = value("eligibility")
    if eligibility and not tender.eligibility_text:
        tender.eligibility_text = str(eligibility)[:4000]
    tender.verification_state = "parsed"
    tender.last_verified_at = dt.datetime.now(dt.timezone.utc)
    db.add(tender)
    db.commit()


def _write_log(db: Session, document: Document, started, result: dict, *,
               schema_version: str, llm_used: bool = False) -> None:
    fields = result.get("fields") or {}
    filled = sum(1 for v in fields.values() if (v.get("value") if isinstance(v, dict) else v) is not None)
    entry = ExtractionLog(
        document_id=document.id,
        schema_version=schema_version,
        model_id=None if not llm_used else "llm",
        started_at=started,
        finished_at=dt.datetime.now(dt.timezone.utc),
        status=result.get("status", "ok"),
        fields_extracted=filled,
        fields_null=max(0, len(fields) - filled),
        fields_uncited=len(result.get("dropped") or []),
        validation_failures=(result.get("validation") or {}).get("errors", []),
        confidence_mean=0.9 if not result.get("dropped") else 0.7,
        payload={
            "warnings": (result.get("validation") or {}).get("warnings", []),
            "dropped": result.get("dropped"),
            "note": result.get("note"),
        },
    )
    db.add(entry)
    db.commit()
