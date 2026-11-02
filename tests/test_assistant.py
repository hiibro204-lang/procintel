"""The assistant must cite, must label basis, and must decline rather than invent."""
from __future__ import annotations

from app.intelligence.assistant import ask, classify, verify_numeric_claims


def test_intent_classification():
    assert classify("What were the L1 L2 L3 prices for chairs?") == "price"
    assert classify("Who won the office chair contracts?") == "supplier"
    assert classify("Which organisations purchased chairs?") == "buyer"
    assert classify("What should our selling price be?") == "recommend"
    assert classify("Write me a poem about monsoon") == "out_of_scope"


def test_numeric_verifier_flags_a_fabricated_figure():
    bad = verify_numeric_claims("The median is ₹13,584 and the highest was ₹9,99,999.", {13584.0})
    assert bad == [999999.0]


def test_numeric_verifier_accepts_supported_figures():
    assert verify_numeric_claims("Median ₹13,584 from 7 records", {13584.0, 7.0}) == []


def test_price_answer_cites_evidence(db, demo_corpus):
    answer = ask(db, "What is the awarded price for an ergonomic office chair?")
    assert answer.intent == "price"
    assert not answer.declined
    assert answer.evidence, "an answer with a figure must carry evidence"
    assert answer.coverage["basis"] == "awarded"
    assert answer.confidence in {"low", "medium", "high"}
    for claim in answer.claims:
        if claim.text.strip():
            assert isinstance(claim.evidence_ids, list)


def test_unknown_product_declines(db, demo_corpus):
    answer = ask(db, "What is the awarded price for a submarine periscope assembly?")
    assert answer.declined
    assert answer.confidence == "none"


def test_out_of_scope_declines(db, demo_corpus):
    answer = ask(db, "Write a poem about the monsoon")
    assert answer.declined
    assert "scope" in answer.direct_answer.lower()


def test_supplier_answer_lists_winners(db, demo_corpus):
    answer = ask(db, "Which suppliers won office chair contracts?")
    assert answer.intent in {"supplier", "price"}
    assert answer.evidence or answer.declined


def test_answer_payload_is_serialisable(db, demo_corpus):
    payload = ask(db, "What is the awarded price for an ergonomic office chair?").to_dict()
    assert {"question", "intent", "direct_answer", "claims", "evidence", "confidence"} <= set(payload)
