"""Price normalisation.

A quoted total is not a unit price, and two awarded prices are rarely measured on
the same basis. Every transformation is recorded so that any normalised figure can
be reconstructed and audited (blueprint Section 10.2).

Design rule: an adjustment is either OBSERVED (stated in the source document),
INDEXED (derived from a maintained series), or ESTIMATED (an assumption). The
result reports how many factors were estimated, because each one multiplies the
uncertainty of the final number.
"""
from __future__ import annotations

from dataclasses import dataclass, field

GST_DEFAULT_RATE = 0.18

OBSERVED = "observed"
INDEXED = "indexed"
ESTIMATED = "estimated"


@dataclass
class NormalisationInput:
    total_price: float
    quantity: float | None = None
    gst_treatment: str | None = None          # inclusive | exclusive | unknown
    gst_rate: float | None = None
    freight_total: float | None = None        # stated freight for the whole order
    installation_total: float | None = None   # stated installation/commissioning
    warranty_years: float | None = None
    reference_warranty_years: float = 3.0
    annual_service_cost_ratio: float = 0.03   # service cost per year, share of price
    quantity_band_factor: float | None = None
    geography_factor: float | None = None
    time_deflator: float | None = None
    reference_quantity: float | None = None


@dataclass
class NormalisationResult:
    unit_price: float
    normalised_unit_price: float
    steps: list[dict] = field(default_factory=list)
    estimated_factors: int = 0
    observed_factors: int = 0
    notes: dict = field(default_factory=dict)

    @property
    def quality(self) -> str:
        if self.estimated_factors == 0:
            return "high"
        if self.estimated_factors <= 2:
            return "medium"
        return "low"


def _step(steps: list[dict], name: str, kind: str, before: float, after: float, detail: str = "") -> None:
    steps.append(
        {
            "step": name,
            "kind": kind,
            "before": round(before, 4),
            "after": round(after, 4),
            "factor": round(after / before, 6) if before else None,
            "detail": detail,
        }
    )


def normalise_price(inp: NormalisationInput) -> NormalisationResult:
    """Normalise a quoted total to a comparable per-unit price."""
    if inp.total_price is None or inp.total_price <= 0:
        raise ValueError("total_price must be positive")

    qty = inp.quantity if inp.quantity and inp.quantity > 0 else None
    steps: list[dict] = []
    estimated = observed = 0

    unit = inp.total_price / qty if qty else inp.total_price
    _step(steps, "unit_price", OBSERVED if qty else ESTIMATED, inp.total_price,
          unit, f"total / quantity ({qty or 'no quantity; treating total as unit'})")
    if qty:
        observed += 1
    else:
        estimated += 1

    current = unit

    # GST ------------------------------------------------------------------ #
    treatment = (inp.gst_treatment or "unknown").lower()
    if treatment == "inclusive":
        rate = inp.gst_rate
        kind = OBSERVED
        if rate is None:
            rate, kind = GST_DEFAULT_RATE, ESTIMATED
        before = current
        current = current / (1 + rate)
        _step(steps, "remove_gst", kind, before, current, f"÷ (1 + {rate:.0%})")
        observed += 1 if kind == OBSERVED else 0
        estimated += 1 if kind == ESTIMATED else 0
    elif treatment == "exclusive":
        _step(steps, "gst", OBSERVED, current, current, "already ex-GST")
        observed += 1
    else:
        _step(steps, "gst", ESTIMATED, current, current,
              "GST treatment unknown; left as quoted and flagged")

    # Freight and installation -------------------------------------------- #
    if qty and inp.freight_total:
        before = current
        current = current - (inp.freight_total / qty)
        _step(steps, "remove_freight", OBSERVED, before, current,
              f"− {inp.freight_total:.0f} / {qty:g} units")
        observed += 1
    if qty and inp.installation_total:
        before = current
        current = current - (inp.installation_total / qty)
        _step(steps, "remove_installation", OBSERVED, before, current,
              f"− {inp.installation_total:.0f} / {qty:g} units")
        observed += 1

    # Warranty standardisation -------------------------------------------- #
    if inp.warranty_years is not None and inp.warranty_years != inp.reference_warranty_years:
        delta = inp.reference_warranty_years - inp.warranty_years
        factor = 1 + (delta * inp.annual_service_cost_ratio)
        before = current
        current = current * factor
        _step(steps, "warranty_standardisation", ESTIMATED, before, current,
              f"{inp.warranty_years:g}y → {inp.reference_warranty_years:g}y @ "
              f"{inp.annual_service_cost_ratio:.0%}/yr")
        estimated += 1

    # Quantity band -------------------------------------------------------- #
    if inp.quantity_band_factor is not None:
        before = current
        current = current * inp.quantity_band_factor
        _step(steps, "quantity_band", INDEXED, before, current,
              f"× {inp.quantity_band_factor:g} to reference quantity")
        observed += 1

    # Geography ------------------------------------------------------------ #
    if inp.geography_factor is not None:
        before = current
        current = current * inp.geography_factor
        _step(steps, "geography", INDEXED, before, current, f"× {inp.geography_factor:g}")
        observed += 1

    # Time ------------------------------------------------------------------ #
    if inp.time_deflator is not None:
        before = current
        current = current * inp.time_deflator
        _step(steps, "time_deflator", INDEXED, before, current, f"× {inp.time_deflator:g}")
        observed += 1

    if current <= 0:
        raise ValueError("normalisation produced a non-positive price; check freight/installation inputs")

    return NormalisationResult(
        unit_price=round(unit, 2),
        normalised_unit_price=round(current, 2),
        steps=steps,
        estimated_factors=estimated,
        observed_factors=observed,
        notes={
            "quantity": qty,
            "gst_treatment": treatment,
            "estimated_factors": estimated,
            "observed_factors": observed,
            "quality": "high" if estimated == 0 else ("medium" if estimated <= 2 else "low"),
        },
    )
