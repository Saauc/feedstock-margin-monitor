"""
quant.py — the statistical layer. Pure functions, no I/O, fully unit-tested.

These are the tools that move the project from "a number on a screen" to
something a desk would actually reason with:

  - historical_percentile: a 72 means nothing without context. "72 = 88th
    percentile of the last two years" is a statement a trader can act on. This
    is distribution-free and robust to the index's arbitrary 0-100 scaling.

  - ewma_volatility: RiskMetrics-style exponentially-weighted vol of returns.
    Volatility clusters, so a decaying weight on recent squared returns tracks
    the current regime far better than a flat trailing stdev.

  - vol_regime: is today's volatility high or low *relative to its own history*?
    Margin-pressure that's drifting in a calm regime is a different risk than
    the same level in a turbulent one.

  - roll_yield / curve_state: backwardation (front > deferred) vs contango from
    the futures curve. The market's own read on physical tightness — and a real
    carry signal — independent of the spot level.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import pstdev

TRADING_DAYS = 252


def pct_returns(values: Sequence[float]) -> list[float]:
    """Simple period-over-period returns. Skips non-positive denominators."""
    out: list[float] = []
    for prev, cur in zip(values, values[1:]):
        if prev:
            out.append(cur / prev - 1.0)
    return out


def ewma_vol_returns(returns: Sequence[float], lam: float = 0.94,
                     annualize: bool = True) -> float | None:
    """EWMA volatility from a RETURN series (RiskMetrics λ=0.94).

    σ²_t = λ·σ²_{t-1} + (1-λ)·r²_t, seeded with the sample variance. Annualized
    by ×√252 by default. None if there isn't enough data. Used directly for the
    feedstock-cost-basket volatility, where we already have weighted returns.
    """
    rets = list(returns)
    if len(rets) < 2:
        return None
    var = pstdev(rets) ** 2  # seed with sample variance
    for r in rets:
        var = lam * var + (1.0 - lam) * r * r
    vol = math.sqrt(var)
    return vol * math.sqrt(TRADING_DAYS) if annualize else vol


def ewma_volatility(values: Sequence[float], lam: float = 0.94,
                    annualize: bool = True) -> float | None:
    """EWMA volatility of a PRICE series (computes returns first)."""
    return ewma_vol_returns(pct_returns(values), lam, annualize)


def historical_percentile(series: Sequence[float],
                          value: float | None = None) -> float | None:
    """Percentile rank (0-100) of `value` (default: latest) within `series`.

    Uses the "<=" rank convention: the fraction of observations at or below the
    value. Robust and unit-free — ideal for contextualizing an index level.
    """
    data = list(series)
    if not data:
        return None
    if value is None:
        value = data[-1]
    at_or_below = sum(1 for x in data if x <= value)
    return 100.0 * at_or_below / len(data)


def vol_regime(current_vol: float | None,
               vol_history: Sequence[float]) -> str:
    """Classify the current vol against its own history.

    'elevated' / 'subdued' / 'normal' by the 80th/20th percentile of the vol
    series. 'unknown' when there isn't enough history to judge.
    """
    if current_vol is None or len(list(vol_history)) < 10:
        return "unknown"
    pct = historical_percentile(vol_history, current_vol)
    if pct is None:
        return "unknown"
    if pct >= 80:
        return "elevated"
    if pct <= 20:
        return "subdued"
    return "normal"


def roll_yield(front: float, deferred: float, months_apart: float) -> float | None:
    """Annualized roll yield between a front and a deferred futures price.

    slope = (deferred - front) / front, annualized by 12/months_apart.
    Negative = backwardation (front richer; a long-roll earns carry).
    Positive = contango (deferred richer; long-roll bleeds carry).
    """
    if not front or months_apart <= 0:
        return None
    slope = (deferred - front) / front
    return slope * (12.0 / months_apart)


def curve_state(front: float, deferred: float) -> str:
    """'backwardation' (front > deferred), 'contango' (front < deferred), else 'flat'."""
    if front > deferred:
        return "backwardation"
    if front < deferred:
        return "contango"
    return "flat"


def rolling_percentile(series: Sequence[float], window: int) -> list[float | None]:
    """Percentile of each point within its trailing `window` (for charts)."""
    data = list(series)
    out: list[float | None] = []
    for i in range(len(data)):
        lo = max(0, i - window + 1)
        window_slice = data[lo:i + 1]
        out.append(historical_percentile(window_slice, data[i])
                   if len(window_slice) >= 2 else None)
    return out
