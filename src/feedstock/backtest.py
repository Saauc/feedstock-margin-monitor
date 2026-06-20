"""
backtest.py — does the index actually track real coatings input costs?

An index nobody has validated is decoration. This module checks the
margin-pressure index (or the underlying feedstock-cost basket) against an
independent ground truth: FRED's PPI for Paint & Coating Manufacturing
(series PCU325510325510). If our daily, free-data proxy genuinely captures
coatings cost pressure, its *changes* should correlate with — and ideally
lead — changes in that monthly PPI.

Outputs a validation report: level & change correlation, directional hit-rate,
a simple OLS beta, and a lead-lag scan (does the index lead the PPI, and by how
many months?). All pure-Python so it runs offline and is unit-testable; the
only external input is two (date, value) series.

PPI is monthly and our index is daily, so we sample the index at each PPI date
(last value on or before it) before comparing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = Sequence[tuple[str, float]]  # [(date, value)], ascending


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def sample_at_dates(daily: Series, target_dates: Sequence[str]) -> list[float | None]:
    """For each target date, the last daily value on or before it (as-of join)."""
    daily = sorted(daily, key=lambda r: r[0])
    out: list[float | None] = []
    i = 0
    last: float | None = None
    for td in sorted(target_dates):
        while i < len(daily) and daily[i][0] <= td:
            last = daily[i][1]
            i += 1
        out.append(last)
    return out


def _diffs(values: Sequence[float | None]) -> list[float | None]:
    out: list[float | None] = [None]
    for prev, cur in zip(values, values[1:]):
        out.append(None if prev is None or cur is None else cur - prev)
    return out


def validate(index_daily: Series, ppi_monthly: Series,
             max_lag_months: int = 3) -> dict:
    """Compare the index against the PPI ground truth. Returns a report dict."""
    ppi_dates = [d for d, _ in sorted(ppi_monthly)]
    ppi_vals = [v for _, v in sorted(ppi_monthly)]
    idx_at_ppi = sample_at_dates(index_daily, ppi_dates)

    # Pair up where both the index and PPI are present.
    lvl_x, lvl_y = [], []
    for iv, pv in zip(idx_at_ppi, ppi_vals):
        if iv is not None:
            lvl_x.append(iv)
            lvl_y.append(pv)

    didx = _diffs(idx_at_ppi)
    dppi = _diffs(ppi_vals)
    chg_x, chg_y = [], []
    for dx, dy in zip(didx, dppi):
        if dx is not None and dy is not None:
            chg_x.append(dx)
            chg_y.append(dy)

    # Directional hit-rate: do index and PPI move the same way month to month?
    hits = sum(1 for dx, dy in zip(chg_x, chg_y) if (dx >= 0) == (dy >= 0))
    hit_rate = (hits / len(chg_x)) if chg_x else None

    # OLS beta of ΔPPI on Δindex (simple slope).
    beta = None
    if len(chg_x) >= 3:
        mx = sum(chg_x) / len(chg_x)
        my = sum(chg_y) / len(chg_y)
        denom = sum((x - mx) ** 2 for x in chg_x)
        if denom:
            beta = sum((x - mx) * (y - my) for x, y in zip(chg_x, chg_y)) / denom

    # Lead-lag: shift the index forward k months and re-correlate the changes.
    # Best k>0 means the index LEADS the PPI by k months.
    lead_lag: dict[int, float] = {}
    for k in range(0, max_lag_months + 1):
        if k == 0:
            xk, yk = chg_x, chg_y
        else:
            raw_x = didx[1:len(didx) - k]
            raw_y = dppi[1 + k:]
            pairs = [(a, b) for a, b in zip(raw_x, raw_y)
                     if a is not None and b is not None]
            xk = [a for a, _ in pairs]
            yk = [b for _, b in pairs]
        c = _pearson(xk, yk) if len(xk) >= 3 else None
        if c is not None:
            lead_lag[k] = round(c, 3)

    best_lag = max(lead_lag, key=lambda kk: lead_lag[kk]) if lead_lag else None

    return {
        "n_ppi": len(ppi_vals),       # total PPI prints in the window
        "n_paired": len(lvl_y),       # effective sample (months index overlaps PPI)
        "corr_levels": round(c, 3) if (c := _pearson(lvl_x, lvl_y)) is not None else None,
        "corr_changes": round(c, 3) if (c := _pearson(chg_x, chg_y)) is not None else None,
        "direction_hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "ols_beta_dppi_on_dindex": round(beta, 4) if beta is not None else None,
        "lead_lag_corr": lead_lag,
        "best_lead_months": best_lag,
    }


# --- I/O runner (keeps validate() pure & offline-testable) ----------------

def run_backtest(conn, config: dict) -> dict | None:
    """Pull the index + target PPI history from the DB, validate, persist.

    Returns the report dict, or None if there isn't enough paired history yet
    (e.g. the FRED series hasn't been backfilled, or the index is too young).
    """
    import json

    from feedstock.store import (
        fetch_metric_history,
        latest_observations,
        save_backtest_result,
    )

    cfg = config.get("backtest", {})
    target = cfg.get("target_metric", "coatings_ppi")
    max_lag = int(cfg.get("max_lag_months", 3))
    min_paired = int(cfg.get("min_paired", 6))

    idx = [(r["date"], r["value"])
           for r in fetch_metric_history(conn, "margin_pressure_index")]
    ppi = [(r["date"], r["value"]) for r in fetch_metric_history(conn, target)]
    if len(idx) < 2 or len(ppi) < min_paired:
        return None

    report = validate(idx, ppi, max_lag_months=max_lag)
    if report["n_paired"] < min_paired:
        return None

    latest = {r["metric"]: r["date"] for r in latest_observations(conn)}
    run_date = latest.get("margin_pressure_index") or idx[-1][0]
    save_backtest_result(conn, run_date, json.dumps(report))
    return report


def run_backtest_cli() -> dict | None:
    """No-arg entry point for the daily orchestrator (run_daily.py)."""
    from feedstock import analysis
    from feedstock.store import get_connection, init_db

    config = analysis.load_config()
    init_db()
    conn = get_connection()
    try:
        report = run_backtest(conn, config)
        print("=" * 60)
        print("  BACKTEST  (index vs FRED coatings PPI)")
        print("=" * 60)
        if report is None:
            print("  Not enough paired history yet. Backfill FRED "
                  "(python -m feedstock.backfill) and accumulate index history.")
        else:
            print(f"  Paired months: {report['n_paired']}   "
                  f"change-corr: {report['corr_changes']}   "
                  f"hit-rate: {report['direction_hit_rate']}")
            print(f"  Best index lead: {report['best_lead_months']} month(s)   "
                  f"OLS beta: {report['ols_beta_dppi_on_dindex']}")
        print("=" * 60)
        return report
    finally:
        conn.close()
