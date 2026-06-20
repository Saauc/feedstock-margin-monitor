"""
briefing.py — assemble the full daily briefing as plain English.

This is the human-readable document that ties the whole pipeline together:
the headline index, the input-cost drivers behind it, the alert status, and
the analyst note (AI or templated). It reads from the accumulated SQLite
history and recomputes the latest snapshot — so it reflects whatever the most
recent ingest produced.

Kept separate from the dashboard (app.py) on purpose: this is the text form,
suitable for a terminal, a log, an email body, or a cron summary. The Flask
app is the visual form. Both read the same data.
"""

from __future__ import annotations

import json
import textwrap

from feedstock import alerting, analysis, transform
from feedstock.narrative import _pct_change
from feedstock.store import get_connection, init_db, latest_backtest, latest_narrative

_WIDTH = 64


def _wrap(text: str, indent: str = "  ") -> str:
    return "\n".join(
        textwrap.fill(line, width=_WIDTH, initial_indent=indent,
                      subsequent_indent=indent)
        for line in text.splitlines() or [text]
    )


def build_briefing(conn=None) -> str:
    """Return the full briefing as a single string."""
    own_conn = conn is None
    if own_conn:
        init_db()
        conn = get_connection()
    try:
        config = analysis.load_config()
        components = config["components"]
        metrics = list(components.keys())
        series_map = analysis.build_series_map(conn, metrics)
        snapshot = transform.compute_latest_pressure(series_map, config)
        alert = alerting.evaluate_alert(snapshot, config)
        market = analysis.market_snapshot(conn, config)
        narrative_row = latest_narrative(conn)

        lines: list[str] = []
        bar = "=" * _WIDTH
        lines.append(bar)
        lines.append(" PETROCHEMICAL FEEDSTOCK MARGIN BRIEFING")
        lines.append(f" As of {snapshot['date'] or 'no data yet'}")
        lines.append(bar)
        lines.append("")

        # Headline index
        idx = snapshot["index"]
        band = _band_label(idx)
        lines.append(f" MARGIN-PRESSURE INDEX:  {idx:.0f}/100   ({band})")
        lines.append(f"   Composite z-score: {snapshot['composite_z']:+.2f}    "
                     f"(50 = costs at trailing baseline)")
        if not snapshot["sufficient"]:
            lines.append("   NOTE: not enough trailing history yet — the index")
            lines.append("   defaults to a neutral 50 until more days accumulate.")
        if market.get("historical_percentile") is not None:
            vol_txt = (f" ({market['vol_pct_ann']:.0f}% ann)"
                       if market.get("vol_pct_ann") is not None else "")
            lines.append(
                f"   Historical percentile: {market['historical_percentile']:.0f}th"
                f"   ·   cost-vol regime: {market['vol_regime']}{vol_txt}")
        lines.append("")

        # Refining margins & forward curve
        lines.append(" REFINING MARGINS & FORWARD CURVE")
        if market.get("crack_321_usd_bbl") is not None:
            lines.append(f"   Gasoline crack    {market['gasoline_crack_usd_bbl']:>7.1f} $/bbl")
            lines.append(f"   Distillate crack  {market['distillate_crack_usd_bbl']:>7.1f} $/bbl")
            lines.append(f"   3:2:1 crack       {market['crack_321_usd_bbl']:>7.1f} $/bbl")
        if market.get("curve_state"):
            roll = market.get("wti_roll_yield_ann_pct")
            roll_txt = f"  (roll {roll:+.1f}%/yr)" if roll is not None else ""
            lines.append(
                f"   WTI curve: front {market['wti_front_usd_bbl']:.2f} vs 6m "
                f"{market['wti_deferred_6m_usd_bbl']:.2f} -> "
                f"{market['curve_state'].upper()}{roll_txt}")
        if market.get("refinery_utilization_pct") is not None:
            lines.append(f"   Refinery utilization: "
                         f"{market['refinery_utilization_pct']:.1f}%  (EIA)")
        if market.get("crack_321_usd_bbl") is None and not market.get("curve_state"):
            lines.append("   (run the pipeline to populate crack spreads & curve)")
        lines.append("")

        # Validation against FRED coatings PPI (when backfilled)
        bt_row = latest_backtest(conn)
        if bt_row:
            rep = json.loads(bt_row["report_json"])
            lines.append(" INDEX VALIDATION (vs FRED coatings PPI)")
            lines.append(
                f"   {rep['n_paired']} paired months · change-corr "
                f"{rep['corr_changes']} · hit-rate {rep['direction_hit_rate']} · "
                f"index lead {rep['best_lead_months']}m")
            lines.append("")

        # Drivers
        lines.append(" INPUT-COST DRIVERS")
        if snapshot["date"] is None:
            lines.append("   (no observations in the database — run ingest.py)")
        else:
            for metric, cfg in components.items():
                series = series_map.get(metric, [])
                latest = series[-1][1] if series else None
                z = snapshot["components"].get(metric)
                pct = _pct_change(series)
                pct_txt = f"{pct:+.1f}%" if pct is not None else "  n/a"
                z_txt = f"{z:+.2f}" if z is not None else " n/a"
                val_txt = f"{latest:>10.3f}" if latest is not None else "       n/a"
                lines.append(
                    f"   {cfg.get('label', metric):<24}{val_txt}   "
                    f"z {z_txt}   recent {pct_txt}"
                )
        lines.append("")

        # Alert
        lines.append(" ALERT STATUS")
        lines.append(alerting.render_alert_flag(alert))
        lines.append("")

        # Analyst note
        if narrative_row:
            src = narrative_row["source"].upper()
            lines.append(f" ANALYST NOTE  ({src})")
            lines.append(_wrap(narrative_row["text"]))
        else:
            lines.append(" ANALYST NOTE")
            lines.append("   (none generated yet — run narrative.py)")
        lines.append("")

        # Footer / honesty
        lines.append("-" * _WIDTH)
        lines.append(_wrap(
            "Data: Yahoo Finance (crude, gas, RBOB/ULSD products, WTI curve), "
            "frankfurter.app (FX), and — when keyed — EIA (Mont Belvieu propane) "
            "and FRED (coatings PPI). Crude and refined products PROXY true "
            "petrochemical feedstocks; TiO2 pigment is not captured. Directional "
            "cost-pressure gauge, not a margin figure, and not investment "
            "advice.", indent=" "))
        lines.append(bar)
        return "\n".join(lines)
    finally:
        if own_conn:
            conn.close()


def _band_label(index: float) -> str:
    if index >= 70:
        return "ELEVATED — margins under pressure"
    if index >= 58:
        return "firming"
    if index > 42:
        return "neutral"
    if index > 30:
        return "easing"
    return "subdued"


if __name__ == "__main__":
    print(build_briefing())
