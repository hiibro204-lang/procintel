"""Product matching must be explainable, and comparable sets must stay tiered."""
from __future__ import annotations

from app.intelligence.matching import (
    best_product_match,
    comparable_set,
    normalise_text,
    score_product_match,
    tokens,
)
from app.models import Product


def test_normalisation_strips_legal_suffixes():
    assert normalise_text("Acme Systems Private Limited") == "acme systems"
    assert normalise_text("Beta & Sons Pvt Ltd") == "beta"


def test_token_overlap_scores_similar_names_higher():
    a = tokens("Ergonomic Office Chair High Back Mesh")
    b = tokens("Ergonomic office chair mesh high back")
    assert len(a & b) >= 5


def test_match_is_explainable(db, demo_corpus):
    product = db.query(Product).first()
    score, explanation = score_product_match(product.canonical_name, product)
    assert 0.0 <= score <= 1.0
    assert "shared_tokens" in explanation
    assert "weights" in explanation, "the scoring weights must be visible for review"


def test_free_text_finds_the_right_product(db, demo_corpus):
    match = best_product_match(db, "office chair high back mesh ergonomic")
    assert match.product is not None
    assert "chair" in match.product.canonical_name.lower()


def test_unrelated_text_does_not_force_a_match(db, demo_corpus):
    match = best_product_match(db, "marine diesel engine crankshaft assembly", threshold=0.6)
    assert match.product is None


def test_comparable_set_returns_tiers_with_counts(db, demo_corpus):
    product = db.query(Product).first()
    result = comparable_set(db, product.id)
    assert len(result["tiers"]) == 3
    assert all("n" in tier for tier in result["tiers"])
    assert sum(t["n"] for t in result["tiers"]) > 0
