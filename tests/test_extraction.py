"""The anti-hallucination contract: uncited values are discarded, not down-weighted."""
from __future__ import annotations

from app.extraction.llm import verify_citations
from app.extraction.pipeline import merge_fields, validate_fields
from app.extraction.rules import extract_rules

SOURCE = (
    "Tender for supply of 40 ergonomic office chairs. "
    "Earnest Money Deposit (EMD) : 12,400.00 "
    "Estimated Tender Value : 6,20,000.00 "
    "The rates quoted shall be inclusive of all taxes and GST. "
    "Warranty of 3 years on the frame is required. HSN Code : 9401"
)


def test_rule_extraction_reads_money_and_dates():
    fields = extract_rules(SOURCE)
    assert fields["emd_amount"]["value"] == 12400.0
    assert fields["estimated_value"]["value"] == 620000.0
    assert fields["warranty_years"]["value"] == 3
    assert fields["gst_treatment"]["value"] == "inclusive"
    assert fields["hsn_code"]["value"] == "9401"
    for payload in fields.values():
        assert payload["quote"], "a rule-derived field must still carry its quote"


def test_citation_verifier_keeps_a_real_quote():
    check = verify_citations(
        {"quantity": {"value": 40, "quote": "supply of 40 ergonomic office chairs"}}, SOURCE
    )
    assert "quantity" in check.verified
    assert check.dropped_count == 0


def test_citation_verifier_drops_a_fabricated_quote():
    check = verify_citations(
        {"quantity": {"value": 400, "quote": "supply of 400 ergonomic chairs in three lots"}}, SOURCE
    )
    assert "quantity" not in check.verified
    assert check.dropped[0]["reason"] == "quote not found verbatim in source"


def test_citation_verifier_drops_uncited_value():
    check = verify_citations({"estimated_value": {"value": 999999, "quote": None}}, SOURCE)
    assert "estimated_value" not in check.verified
    assert check.dropped[0]["reason"] == "no quote supplied"


def test_honest_absence_is_not_a_failure():
    check = verify_citations({"eligibility": {"value": None, "quote": None}}, SOURCE)
    assert check.verified == {}
    assert check.dropped_count == 0


def test_rules_override_the_model_where_both_speak():
    merged = merge_fields(
        {"emd_amount": {"value": 12400.0, "quote": "EMD : 12,400.00", "method": "rule"}},
        {"emd_amount": {"value": 50000.0, "quote": "EMD", "method": "llm"},
         "eligibility": {"value": "MSE only", "quote": "Warranty of 3 years", "method": "llm"}},
    )
    assert merged["emd_amount"]["value"] == 12400.0
    assert merged["eligibility"]["value"] == "MSE only"


def test_validation_rejects_impossible_quantities():
    outcome = validate_fields({"quantity": {"value": 0}})
    assert not outcome.ok
    assert any("positive" in e for e in outcome.errors)


def test_validation_warns_on_implausible_emd_ratio():
    outcome = validate_fields({"estimated_value": {"value": 100000}, "emd_amount": {"value": 50000}})
    assert outcome.ok
    assert any("EMD" in w for w in outcome.warnings)
