"""
analysis.py — wire the pure index logic (transform.py) to config + the DB.

Responsibilities (the I/O that transform.py deliberately avoids):
  - load weights / window / directions from config.json
  - pull each component metric's history out of SQLite
  - hand that to transform.compute_latest_pressure
  - print a readable breakdown
  - persist the result back into the same observations table:
        metric = 'margin_pressure_index'   (the 0..100 score)
        metric = 'zscore_<component>'       (each directional z-score)
    written under source='computed' on the snapshot's date. These reuse the
    same UNIQUE(date, source, metric) idempotency, so re-running is safe, and
    they give Phase 3 (narrative) and Phase 6 (charts) something to read.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import pstdev

from feedstock import quant, transform
from feedstock.paths import CONFIG_PATH
from feedstock.store import (
    fetch_metric_history,
    get_connection,
    init_db,
    latest_observations,
    record_observation,
)


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_series_map(conn, metrics: list[str]) -> dict:
    """Pull (date, value) history for each input metric from the DB."""
    return {
        metric: [(row["date"], row["value"]) for row in fetch_metric_history(conn, metric)]
        for metric in metrics
    }


def _basket_returns(conn, config: dict) -> list[float]:
    """Weighted daily returns of the feedstock-cost basket (portfolio-style).

    Aligns the component price series, then forms each day's basket return as
    the weight-normalized average of the component returns. This is the right
    thing to compute volatility on — a price-like series — unlike the bounded
    0-100 index (whose annualized vol is meaningless).
    """
    components = config["components"]
    series_map = build_series_map(conn, list(components.keys()))
    dates, aligned = transform.align_by_date(series_map)
    weights = {m: float(cfg.get("weight", 0.0)) for m, cfg in components.items()}
    rets: list[float] = []
    for i in range(1, len(dates)):
        num = wsum = 0.0
        for m in components:
            col = aligned.get(m)
            if not col:
                continue
            cur, prev = col[i], col[i - 1]
            if cur is None or prev is None or prev == 0:
                continue
            num += weights[m] * (cur / prev - 1.0)
            wsum += weights[m]
        if wsum > 0:
            rets.append(num / wsum)
    return rets


def compute_quant_context(conn, config: dict) -> dict:
    """Contextualize the index: percentile (of the index), plus the feedstock-
    cost-basket volatility and its regime.

    The percentile uses the index's own history. Volatility is computed on the
    cost BASKET (a price-like series), not the bounded index, so annualizing it
    is meaningful. Returns neutral/unknown fields until enough history exists.
    """
    window = int(config.get("percentile_window", 504))
    idx = [r["value"] for r in fetch_metric_history(conn, "margin_pressure_index")]
    ctx: dict[str, object] = {"n": len(idx), "percentile": None,
                              "vol_pct_ann": None, "vol_regime": "unknown"}
    if len(idx) < 2:
        return ctx

    recent = idx[-window:]
    pct = quant.historical_percentile(recent)
    ctx["percentile"] = round(pct, 1) if pct is not None else None

    # Feedstock-cost-basket volatility + regime (price-like, so annualizable).
    basket = _basket_returns(conn, config)
    vol = quant.ewma_vol_returns(basket)
    ctx["vol_pct_ann"] = round(vol * 100, 1) if vol is not None else None
    win = 20
    if len(basket) >= 2:
        vol_hist = [pstdev(basket[max(0, i - win + 1):i + 1])
                    for i in range(1, len(basket))]
        current = pstdev(basket[-win:])
        ctx["vol_regime"] = quant.vol_regime(current, vol_hist)
    return ctx


def market_snapshot(conn, config: dict) -> dict:
    """Combine quant context + crack spreads + curve into one signals dict.

    Shared by the narrative, briefing, and dashboard so they all tell the same
    story. Pulls the latest derived crack/curve metrics straight from the DB.
    """
    ctx = compute_quant_context(conn, config)
    latest = {r["metric"]: r["value"] for r in latest_observations(conn)}
    front = latest.get("wti_crude_usd_bbl")
    deferred = latest.get("wti_deferred_6m_usd_bbl")
    curve_state = None
    if front is not None and deferred is not None:
        curve_state = quant.curve_state(front, deferred)
    return {
        "historical_percentile": ctx["percentile"],
        "vol_regime": ctx["vol_regime"],
        "vol_pct_ann": ctx["vol_pct_ann"],
        "gasoline_crack_usd_bbl": latest.get("gasoline_crack_usd_bbl"),
        "distillate_crack_usd_bbl": latest.get("distillate_crack_usd_bbl"),
        "crack_321_usd_bbl": latest.get("crack_321_usd_bbl"),
        "wti_front_usd_bbl": front,
        "wti_deferred_6m_usd_bbl": deferred,
        "wti_roll_yield_ann_pct": latest.get("wti_roll_yield_ann_pct"),
        "curve_state": curve_state,
        "refinery_utilization_pct": latest.get("refinery_utilization_pct"),
    }


def _print_report(snapshot: dict, config: dict) -> None:
    components = config["components"]
    print("=" * 60)
    print("  MARGIN-PRESSURE INDEX")
    print("=" * 60)
    if snapshot["date"] is None:
        print("  No data in database yet — run ingest.py first.")
        return

    print(f"  As of:        {snapshot['date']}")
    print(f"  Index (0-100): {snapshot['index']:.1f}   "
          f"(50 = costs at baseline; higher = more pressure)")
    print(f"  Composite z:   {snapshot['composite_z']:+.3f}")
    if not snapshot["sufficient"]:
        print("  NOTE: not enough trailing history yet — index defaults toward")
        print("        neutral 50. It becomes meaningful as days accumulate.")
    print("-" * 60)
    print(f"  {'component':<26}{'weight':>8}{'dir':>5}{'dir. z':>10}")
    for metric, z in snapshot["components"].items():
        cfg = components.get(metric, {})
        print(f"  {cfg.get('label', metric):<26}"
              f"{cfg.get('weight', 0):>8.2f}"
              f"{cfg.get('direction', 1):>5}"
              f"{z:>+10.3f}")
    print("=" * 60)


def run_analysis(persist: bool = True) -> dict:
    """Compute the latest margin-pressure index and (optionally) persist it."""
    config = load_config()
    metrics = list(config["components"].keys())

    init_db()
    conn = get_connection()
    try:
        series_map = build_series_map(conn, metrics)
        snapshot = transform.compute_latest_pressure(series_map, config)
        _print_report(snapshot, config)

        if persist and snapshot["date"] is not None:
            record_observation(
                conn, snapshot["date"], "computed",
                "margin_pressure_index", snapshot["index"],
            )
            for metric, z in snapshot["components"].items():
                record_observation(
                    conn, snapshot["date"], "computed", f"zscore_{metric}", z,
                )
            conn.commit()

        # Quant context is computed AFTER the index is persisted, so the
        # percentile/vol see today's value too.
        ctx = compute_quant_context(conn, config)
        snapshot["quant"] = ctx
        if persist and snapshot["date"] is not None and ctx["percentile"] is not None:
            record_observation(conn, snapshot["date"], "computed",
                               "index_percentile", ctx["percentile"])
            if ctx["vol_pct_ann"] is not None:
                record_observation(conn, snapshot["date"], "computed",
                                   "index_vol_pct_ann", ctx["vol_pct_ann"])
            conn.commit()

        if ctx["percentile"] is not None:
            print(f"  Historical percentile: {ctx['percentile']:.0f}th "
                  f"(of last {ctx['n']} obs)   vol regime: {ctx['vol_regime']}")

        return snapshot
    finally:
        conn.close()


if __name__ == "__main__":
    run_analysis()
