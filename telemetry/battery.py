"""Estimated state-of-charge for a single-cell 18650 Li-ion repeater battery.

MeshCore telemetry only reports raw cell voltage; it does not report a
charge percentage. This estimates one from voltage using the manufacturer's
0.2C discharge curve for a Samsung INR18650-35E cell (the assumed repeater
battery), so the dashboard has something usable to trend over time.

The curve is intentionally a rough approximation: real state of charge also
depends on discharge rate, temperature, and cell age/wear, none of which are
available here. Treat the resulting percentage as indicative, not precise.
"""

from __future__ import annotations

# Approximate 0.2C discharge curve for a Samsung INR18650-35E cell,
# normalized to a 3.30 Ah total capacity. The points are sorted ascending by
# voltage for interpolation and match the shape in the reference graph.
_DISCHARGE_CURVE: tuple[tuple[float, float], ...] = (
    (2.80, 0.0),
    (3.20, 10.0),
    (3.42, 20.0),
    (3.55, 30.0),
    (3.66, 40.0),
    (3.78, 50.0),
    (3.88, 60.0),
    (3.96, 70.0),
    (4.05, 80.0),
    (4.13, 90.0),
    (4.20, 100.0),
)


def estimate_percent(voltage: float | None) -> float | None:
    """Map a cell voltage to an estimated charge percentage (0-100)."""
    if voltage is None:
        return None

    curve = _DISCHARGE_CURVE
    if voltage <= curve[0][0]:
        return 0.0
    if voltage >= curve[-1][0]:
        return 100.0

    for (v_lo, p_lo), (v_hi, p_hi) in zip(curve, curve[1:]):
        if v_lo <= voltage <= v_hi:
            frac = (voltage - v_lo) / (v_hi - v_lo)
            return round(p_lo + frac * (p_hi - p_lo), 1)

    return None  # unreachable given the bounds checks above
