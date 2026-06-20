"""
term_structure.py — the crude futures curve: contango vs backwardation.

The shape of the forward curve is the market's own verdict on physical
tightness, independent of the spot price:

  - Backwardation (front > deferred): buyers pay up for prompt barrels →
    tight physical market. For petrochemical feedstock, a backwardated crude
    curve often coincides with squeezed downstream availability.
  - Contango (front < deferred): prompt glut, storage incentivized.

We read the front (CL=F continuous) and a ~6-month deferred WTI contract from
Yahoo, then compute the annualized roll yield (quant.roll_yield). The deferred
contract symbol is built DYNAMICALLY from today's date so it never goes stale
as contracts expire and roll.
"""

from __future__ import annotations

import datetime as dt

from feedstock import quant
from feedstock.ingest import fetch_yahoo_metric
from feedstock.store import record_observation

# NYMEX futures month codes: Jan..Dec.
_MONTH_CODES = "FGHJKMNQUVXZ"
DEFERRED_MONTHS = 6


def deferred_wti_symbol(months_ahead: int = DEFERRED_MONTHS,
                        today: dt.date | None = None) -> str:
    """Build the Yahoo symbol for the WTI contract ~`months_ahead` out.

    e.g. from June 2026, 6 months ahead -> December 2026 -> 'CLZ26.NYM'.
    Computed from the date so it auto-rolls; no hardcoded contract.
    """
    today = today or dt.date.today()
    month_index = today.month - 1 + months_ahead
    year = today.year + month_index // 12
    month = month_index % 12  # 0-based -> Jan=0
    return f"CL{_MONTH_CODES[month]}{year % 100:02d}.NYM"


def fetch_and_store_curve(conn, months_ahead: int = DEFERRED_MONTHS) -> dict:
    """Fetch front + deferred WTI, compute the roll, and persist curve metrics.

    Stores `wti_deferred_<n>m_usd_bbl` and `wti_roll_yield_ann_pct` under
    source='derived'. Returns a small summary dict (or empty on failure).
    """
    front_obs = fetch_yahoo_metric("CL=F", "yahoo_finance", "wti_crude_usd_bbl")
    deferred_symbol = deferred_wti_symbol(months_ahead)
    deferred_metric = f"wti_deferred_{months_ahead}m_usd_bbl"
    deferred_obs = fetch_yahoo_metric(deferred_symbol, "yahoo_finance", deferred_metric)

    if not front_obs or not deferred_obs:
        return {}

    date, _, _, front = front_obs[0]
    d_date, _, _, deferred = deferred_obs[0]

    roll = quant.roll_yield(front, deferred, months_ahead)
    state = quant.curve_state(front, deferred)

    record_observation(conn, d_date, "yahoo_finance", deferred_metric, deferred)
    if roll is not None:
        record_observation(conn, date, "derived", "wti_roll_yield_ann_pct",
                            round(roll * 100, 3))
    conn.commit()

    return {
        "date": date,
        "front": front,
        "deferred": deferred,
        "deferred_symbol": deferred_symbol,
        "months_ahead": months_ahead,
        "roll_yield_ann_pct": round(roll * 100, 3) if roll is not None else None,
        "state": state,
    }
