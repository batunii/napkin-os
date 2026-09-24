"""budget_band: a band, never the figure (Contract 3 §2.3).

The model reports the amount and currency it read (with the quote, which is
verified and then dropped from the span). The band is computed here. A
non-EUR amount converts at a static rate and is banded only when a ±15%
move in the rate leaves it in the same band — otherwise extraction abstains.
"""

from __future__ import annotations

FX_TO_EUR = {"EUR": 1.0, "GBP": 1.17, "USD": 0.92}
BANDS = [(50_000, "under_50k"), (250_000, "50k_250k"), (1_000_000, "250k_1m"),
         (5_000_000, "1m_5m"), (float("inf"), "over_5m")]


def band_for_eur(eur: float) -> str:
    return next(b for lim, b in BANDS if eur < lim)


def band(amount, currency: str) -> tuple[str | None, str | None]:
    """(band, None) or (None, why it abstains)."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        return None, "budget_band: no amount"
    cur = (currency or "").upper()
    rate = FX_TO_EUR.get(cur)
    if rate is None:
        return None, "budget_band: amount in an unsupported currency"
    if cur != "EUR" and band_for_eur(amount * rate * 0.85) != band_for_eur(amount * rate * 1.15):
        return None, f"budget_band: {cur} amount sits too near a band edge to convert"
    return band_for_eur(amount * rate), None
