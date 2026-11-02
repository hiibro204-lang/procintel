"""Price normalisation: every transformation recorded, estimated factors counted."""
from __future__ import annotations

import pytest

from app.intelligence.normalise import NormalisationInput, normalise_price


def test_unit_price_is_total_over_quantity():
    result = normalise_price(NormalisationInput(total_price=620000, quantity=40))
    assert result.unit_price == 15500.0
    assert result.normalised_unit_price == 15500.0
    assert result.steps[0]["step"] == "unit_price"


def test_inclusive_gst_is_removed():
    result = normalise_price(
        NormalisationInput(total_price=118000, quantity=10, gst_treatment="inclusive", gst_rate=0.18)
    )
    assert result.unit_price == 11800.0
    assert result.normalised_unit_price == pytest.approx(10000.0, rel=1e-6)
    assert result.estimated_factors == 0
    assert result.quality == "high"


def test_unknown_gst_is_flagged_not_guessed():
    result = normalise_price(NormalisationInput(total_price=1000, quantity=1, gst_treatment="unknown"))
    gst_step = [s for s in result.steps if s["step"] == "gst"][0]
    assert gst_step["kind"] == "estimated"
    assert gst_step["after"] == gst_step["before"], "unknown GST must not silently change the price"


def test_freight_and_installation_are_removed_per_unit():
    result = normalise_price(
        NormalisationInput(
            total_price=100000, quantity=50, gst_treatment="exclusive",
            freight_total=5000, installation_total=2500,
        )
    )
    assert result.normalised_unit_price == pytest.approx(2000 - 100 - 50, rel=1e-6)


def test_estimated_factor_count_drives_quality_band():
    result = normalise_price(
        NormalisationInput(
            total_price=1000, quantity=10, gst_treatment="inclusive", gst_rate=0.18,
            warranty_years=1, reference_warranty_years=3,
            quantity_band_factor=0.94, geography_factor=1.01, time_deflator=1.008,
        )
    )
    assert result.estimated_factors >= 1
    assert result.quality in {"medium", "low"}
    assert result.notes["estimated_factors"] == result.estimated_factors


def test_every_step_is_reconstructable():
    result = normalise_price(
        NormalisationInput(total_price=118000, quantity=10, gst_treatment="inclusive", gst_rate=0.18)
    )
    running = result.steps[0]["after"]
    for step in result.steps[1:]:
        assert step["before"] == pytest.approx(running, rel=1e-6)
        running = step["after"]
    assert running == pytest.approx(result.normalised_unit_price, rel=1e-6)


def test_rejects_non_positive_total():
    with pytest.raises(ValueError):
        normalise_price(NormalisationInput(total_price=0))


def test_rejects_normalisation_that_goes_negative():
    with pytest.raises(ValueError):
        normalise_price(
            NormalisationInput(total_price=1000, quantity=10, freight_total=50000)
        )
