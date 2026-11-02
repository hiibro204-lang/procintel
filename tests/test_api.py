"""API contract: provenance present, personal data absent, gate enforced."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _corpus(demo_corpus):
    """Most API assertions need a corpus; this makes the dependency explicit."""


def test_health(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["tenders"] >= 1


def test_tender_search_returns_provenance(client):
    body = client.get("/api/v1/tenders?limit=5").json()
    assert body["total"] >= 1
    record = body["results"][0]
    assert "provenance" in record
    for key in ("source_id", "extraction_method", "confidence", "verification_state"):
        assert key in record["provenance"]


def test_tender_detail_exposes_the_ladder(client):
    tid = client.get("/api/v1/tenders?limit=1").json()["results"][0]["id"]
    detail = client.get(f"/api/v1/tenders/{tid}").json()
    assert detail["line_items"]
    assert isinstance(detail["price_ladder"], list)
    assert isinstance(detail["bidders"], list)


def test_individual_officer_names_are_never_serialised(client):
    tid = client.get("/api/v1/tenders?limit=5").json()["results"][0]["id"]
    detail = client.get(f"/api/v1/tenders/{tid}").json()
    blob = str(detail)
    assert "individual_name" not in blob
    for contact in detail["contacts"]:
        assert set(contact) <= {"official_email", "official_phone", "data_class"}


def test_prices_endpoint_respects_basis(client):
    pid = client.get("/api/v1/products?limit=1").json()["results"][0]["id"]
    body = client.get(f"/api/v1/products/{pid}/prices?basis=awarded").json()
    assert body["basis"] == "awarded"
    assert "n" in body and "tier" in body
    assert client.get(f"/api/v1/products/{pid}/prices?basis=nonsense").status_code == 400


def test_small_samples_are_not_given_distribution_statistics(client):
    pid = client.get("/api/v1/products?limit=1").json()["results"][0]["id"]
    body = client.get(f"/api/v1/products/{pid}/prices?basis=awarded").json()
    if body["n"] < 8:
        assert "p90" not in body, "percentiles must not be published on a small sample"


def test_recommendation_is_transparent(client):
    pid = client.get("/api/v1/products?limit=1").json()["results"][0]["id"]
    body = client.post(f"/api/v1/products/{pid}/recommend?landed_cost=1000&target_margin_pct=0.2").json()
    assert body["steps"], "the calculation must be shown, not just the answer"
    assert body["inputs"]["target_margin_pct"] == 0.2


def test_sources_report_the_gate(client):
    body = client.get("/api/v1/sources").json()
    by_key = {s["key"]: s for s in body["sources"]}
    assert by_key["gem_results"]["runnable"] is True
    assert by_key["maharashtra"]["runnable"] is False
    assert by_key["maharashtra"]["blocked_reasons"]


def test_blocked_source_run_is_refused(client):
    response = client.post("/api/v1/connectors/cppp_listing/run")
    assert response.status_code == 403
    assert "reasons" in response.json()["detail"]


def test_connector_self_test_endpoint(client):
    body = client.post("/api/v1/connectors/gem_results/self-test").json()
    assert body["ok"] is True


def test_assistant_endpoint_cites_and_declines(client):
    body = client.post(
        "/api/v1/assistant", json={"question": "What is the awarded price for an ergonomic office chair?"}
    ).json()
    assert body["intent"] == "price"
    assert body["coverage"]["basis"] == "awarded"

    declined = client.post(
        "/api/v1/assistant", json={"question": "What is the price of a submarine periscope?"}
    ).json()
    assert declined["declined"] is True


def test_pages_render(client):
    for path in ["/", "/search?q=chair", "/prices", "/assistant", "/sources", "/categories"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Procurement Intelligence" in response.text


def test_openapi_schema_is_served(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/tenders" in schema["paths"]
    assert "/api/v1/assistant" in schema["paths"]
