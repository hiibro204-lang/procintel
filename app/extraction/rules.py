"""Deterministic, high-precision field rules.

Where a rule is reliable we prefer it to a language model: it is free, fast,
auditable and never hallucinates. These run as "overrides" after LLM extraction
and before validation (blueprint Section 4.4, stage 5).
"""
from __future__ import annotations

import re

MONEY = r"(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"

EMD_RE = re.compile(rf"earnest\s*money(?:\s*deposit)?\s*(?:\(emd\))?\s*[:\-]?\s*{MONEY}", re.I)
VALUE_RE = re.compile(rf"(?:estimated\s*(?:tender\s*)?value|tender\s*value)\s*[:\-]?\s*{MONEY}", re.I)
FEE_RE = re.compile(rf"(?:tender\s*(?:document\s*)?fee|cost\s*of\s*tender\s*document)\s*[:\-]?\s*{MONEY}", re.I)
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}[Z]{1}[A-Z\d]{1}\b")
HSN_RE = re.compile(r"\bHSN\s*(?:code)?\s*[:\-]?\s*(\d{4,8})\b", re.I)
DATE_RE = re.compile(r"\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\b")
QTY_RE = re.compile(r"\b(?:qty|quantity)\s*[:\-]?\s*([0-9][0-9,]*(?:\.\d+)?)", re.I)
WARRANTY_RE = re.compile(
    r"(?:warranty[^.]{0,24}?(\d{1,2})\s*(?:year|years|yr|yrs))"
    r"|(?:(\d{1,2})\s*(?:year|years|yr|yrs)[^.]{0,24}?warranty)",
    re.I,
)
GST_WORDS = {
    "inclusive": re.compile(r"(?:inclusive|including)\s+(?:of\s+)?(?:all\s+)?(?:taxes|gst)", re.I),
    "exclusive": re.compile(r"(?:exclusive|excluding|extra)\s+(?:of\s+)?(?:taxes|gst)", re.I),
}


def _money(match: re.Match | None) -> float | None:
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def extract_rules(text: str) -> dict:
    """Return only fields a deterministic rule can defend, with the quote."""
    out: dict = {}

    def put(key: str, value, match: re.Match | None, note: str = "rule"):
        if value is None:
            return
        start = max(0, (match.start() if match else 0) - 40)
        end = min(len(text), (match.end() if match else 0) + 40)
        out[key] = {
            "value": value,
            "quote": text[start:end].strip(),
            "method": note,
            "offset": match.start() if match else None,
        }

    m = EMD_RE.search(text)
    put("emd_amount", _money(m), m)
    m = VALUE_RE.search(text)
    put("estimated_value", _money(m), m)
    m = FEE_RE.search(text)
    put("tender_fee", _money(m), m)
    m = QTY_RE.search(text)
    if m:
        put("quantity", float(m.group(1).replace(",", "")), m)
    m = WARRANTY_RE.search(text)
    if m:
        years = m.group(1) or m.group(2)
        put("warranty_years", int(years), m)
    m = HSN_RE.search(text)
    if m:
        put("hsn_code", m.group(1), m)

    for label, pattern in GST_WORDS.items():
        m = pattern.search(text)
        if m:
            put("gst_treatment", label, m)
            break

    gstins = sorted(set(GSTIN_RE.findall(text)))
    if gstins:
        out["gstins"] = {"value": gstins[:20], "quote": gstins[0], "method": "rule", "offset": None}
    return out


def parse_indian_money(text: str) -> float | None:
    """Standalone helper used by tests and the UI."""
    return _money(re.search(MONEY, text or ""))
