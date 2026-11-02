"""GeM parsers, exercised against captured-structure fixtures."""
from __future__ import annotations

import pytest

from app.connectors.base import load_fixture, parse_money, parse_rank
from app.connectors.gem_parsers import canonical_seller_name, parse_bid_result, parse_listing


@pytest.fixture(scope="module")
def result():
    return parse_bid_result(load_fixture("gem_result_fixture.html"), "https://example/test")


def test_bid_number_and_dates(result):
    t = result["tender"]
    assert t["portal_tender_id"] == "GEM/2026/B/8069492"
    assert t["closing_at"] is not None
    assert t["quantity"] == 40


def test_buyer_details_are_split_from_address(result):
    t = result["tender"]
    assert t["buyer_email"] == "buyer12.cg.mp@gembuyer.in"
    assert t["state"] == "Madhya Pradesh"
    assert t["pincode"] == "462001"


def test_technical_status_uses_the_real_status_column(result):
    """Regression: 'Status' must not resolve to 'EMD Status'."""
    statuses = {r["seller_name_raw"]: r["status"] for r in result["technical_results"]}
    assert statuses["SHREEJI OFFICE SYSTEMS PRIVATE LIMITED"] == "qualified"
    assert statuses["KAVERI TRADERS"] == "disqualified"


def test_financial_ladder_is_ranked_and_ordered(result):
    ladder = result["financial_bids"]
    assert [f["rank"] for f in ladder] == [1, 2, 3]
    prices = [f["total_price"] for f in ladder]
    assert prices == sorted(prices)
    assert ladder[0]["total_price"] == 620000.0


def test_seller_suffixes_are_stripped_so_tables_join(result):
    tech_names = {b["seller_name_raw"] for b in result["bidders"]}
    fin_names = {f["seller_name_raw"] for f in result["financial_bids"]}
    assert "DECCAN SEATING SOLUTIONS" in fin_names
    assert fin_names & tech_names, "technical and financial tables must share seller identities"


def test_canonical_seller_name_handles_observed_suffixes():
    assert canonical_seller_name("ACME LTD (MII)") == "ACME LTD"
    assert canonical_seller_name("BETA (MSE Social Category:OBC)") == "BETA"
    assert canonical_seller_name("GAMMA (Under PMA)") == "GAMMA"


def test_listing_parser_finds_records():
    records = parse_listing(load_fixture("gem_listing_fixture.html"), "u")
    assert len(records) == 3
    assert all(r["portal_tender_id"].startswith("GEM/") for r in records)
    assert all(r["closing_at"] for r in records)


def test_parser_rejects_a_non_result_page():
    with pytest.raises(ValueError):
        parse_bid_result("<html><body><p>Nothing here</p></body></html>")


def test_money_and_rank_helpers():
    assert parse_money("₹ 6,20,000.00") == 620000.0
    assert parse_money("Rs. 1,94,1200.00") == 1941200.0
    assert parse_rank("L3") == 3
    assert parse_rank("") is None
