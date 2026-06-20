"""
app.py — dark-theme Flask dashboard for the feedstock margin monitor.

Read-only view over the accumulated SQLite history: the headline margin-pressure
index with its historical percentile and vol regime, the alert status, the daily
analyst note, the refining crack spreads, the WTI forward-curve signal, current
driver values, and trend charts. It does NOT fetch or compute new data — that's
the daily pipeline's job. The dashboard just visualizes accumulated history, so
it stays fast and side-effect-free.

Run locally:
    python -m feedstock.app        # then open http://127.0.0.1:5000

Charts use Chart.js from a CDN (needs internet in the browser). The theme is
self-contained dark CSS since this is a standalone page, not an embedded widget.
"""

from __future__ import annotations

import json

from flask import Flask, render_template_string

from feedstock import alerting, analysis, store, transform
from feedstock.narrative import _pct_change

app = Flask(__name__)

# Per-metric line colors (hardcoded hex — a <canvas> can't read CSS variables).
METRIC_COLORS = {
    "brent_crude_usd_bbl": "#f0997b",          # coral
    "wti_crude_usd_bbl": "#ef9f27",            # amber
    "henry_hub_natgas_usd_mmbtu": "#5dcaa5",   # teal
    "rbob_gasoline_usd_gal": "#97c459",        # green
    "ulsd_diesel_usd_gal": "#d4537e",          # pink-red
    "propane_mont_belvieu_usd_gal": "#7f77dd",  # purple
    "eur_usd": "#85b7eb",                      # blue
    "usd_cny": "#ed93b1",                      # pink
}
CRACK_COLORS = {
    "gasoline_crack_usd_bbl": "#97c459",
    "distillate_crack_usd_bbl": "#ef9f27",
    "crack_321_usd_bbl": "#7f77dd",
}
CHART_METRICS = [
    "brent_crude_usd_bbl", "wti_crude_usd_bbl", "henry_hub_natgas_usd_mmbtu",
    "rbob_gasoline_usd_gal", "ulsd_diesel_usd_gal", "eur_usd", "usd_cny",
]
CRACK_METRICS = ["gasoline_crack_usd_bbl", "distillate_crack_usd_bbl", "crack_321_usd_bbl"]


def _index_color(index: float, sufficient: bool) -> str:
    """Color the headline index by band: higher pressure → hotter color."""
    if not sufficient:
        return "#8b949e"
    if index >= 70:
        return "#e24b4a"   # red — alert
    if index >= 58:
        return "#ef9f27"   # amber — firming
    if index > 42:
        return "#8b949e"   # gray — neutral
    return "#97c459"       # green — subdued


def _history(conn, metrics: list[str]) -> dict:
    return {m: [(r["date"], r["value"])
                for r in store.fetch_metric_history(conn, m)] for m in metrics}


def _align_for_chart(series_map: dict, metrics: list[str]) -> dict:
    """Shared date axis with None for missing points (no forward-fill).

    A gap is shown as a gap, not a flat carried-forward line.
    """
    all_dates = sorted({d for m in metrics for d, _ in series_map.get(m, [])})
    out: dict = {"labels": all_dates}
    for m in metrics:
        lookup = dict(series_map.get(m, []))
        out[m] = [lookup.get(d) for d in all_dates]
    return out


def _fmt(value, suffix="", places=1):
    return f"{value:.{places}f}{suffix}" if value is not None else "—"


@app.route("/")
def dashboard():
    config = analysis.load_config()
    components = config["components"]
    metrics = list(components.keys())
    threshold = float(config.get("alert_threshold", 70))

    store.init_db()
    conn = store.get_connection()
    try:
        series_map = analysis.build_series_map(conn, metrics)
        snapshot = transform.compute_latest_pressure(series_map, config)
        alert = alerting.evaluate_alert(snapshot, config)
        market = analysis.market_snapshot(conn, config)
        narrative_row = store.latest_narrative(conn)
        backtest_row = store.latest_backtest(conn)
        index_rows = store.fetch_metric_history(conn, "margin_pressure_index")
        chart_series = _history(conn, CHART_METRICS)
        crack_series = _history(conn, CRACK_METRICS)
    finally:
        conn.close()

    backtest = json.loads(backtest_row["report_json"]) if backtest_row else None

    driver_cards = []
    for metric, cfg in components.items():
        series = series_map.get(metric, [])
        latest = series[-1][1] if series else None
        pct = _pct_change(series)
        driver_cards.append({
            "label": cfg.get("label", metric),
            "value": f"{latest:.3f}" if latest is not None else "—",
            "pct": (f"{pct:+.1f}%" if pct is not None else "—"),
            "pct_up": (pct is not None and pct >= 0),
            "color": METRIC_COLORS.get(metric, "#8b949e"),
        })

    charts = {
        "index": {
            "labels": [r["date"] for r in index_rows],
            "values": [round(r["value"], 1) for r in index_rows],
            "threshold": threshold,
        },
        "cracks": _align_for_chart(crack_series, CRACK_METRICS),
        "crude": _align_for_chart(chart_series, ["brent_crude_usd_bbl", "wti_crude_usd_bbl"]),
        "products": _align_for_chart(chart_series, ["rbob_gasoline_usd_gal", "ulsd_diesel_usd_gal"]),
        "gas": _align_for_chart(chart_series, ["henry_hub_natgas_usd_mmbtu"]),
        "fx": _align_for_chart(chart_series, ["eur_usd", "usd_cny"]),
    }

    # Top metrics strip
    pct = market.get("historical_percentile")
    roll = market.get("wti_roll_yield_ann_pct")
    stats = [
        {"label": "Historical percentile",
         "value": (f"{pct:.0f}th" if pct is not None else "—"),
         "sub": "of trailing window"},
        {"label": "Feedstock cost vol", "value": market.get("vol_regime", "—"),
         "sub": _fmt(market.get("vol_pct_ann"), "% ann")},
        {"label": "3:2:1 crack", "value": _fmt(market.get("crack_321_usd_bbl"), " $/bbl"),
         "sub": "refining margin"},
        {"label": "WTI curve",
         "value": (market.get("curve_state") or "—").title(),
         "sub": (f"roll {roll:+.1f}%/yr" if roll is not None else "—")},
    ]
    if market.get("refinery_utilization_pct") is not None:
        stats.append({"label": "Refinery utilization",
                      "value": _fmt(market["refinery_utilization_pct"], "%"),
                      "sub": "EIA (US)"})

    context = {
        "as_of": snapshot["date"] or "no data yet",
        "index": round(snapshot["index"]),
        "index_color": _index_color(snapshot["index"], snapshot["sufficient"]),
        "composite_z": f"{snapshot['composite_z']:+.2f}",
        "sufficient": snapshot["sufficient"],
        "alert": alert,
        "stats": stats,
        "term": market,
        "narrative": (narrative_row["text"] if narrative_row else None),
        "narrative_source": (narrative_row["source"].upper() if narrative_row else None),
        "backtest": backtest,
        "drivers": driver_cards,
        "charts": charts,
        "colors": {**METRIC_COLORS, **CRACK_COLORS},
        "has_data": snapshot["date"] is not None,
    }
    return render_template_string(TEMPLATE, **context)


def main():
    app.run(host="127.0.0.1", port=5000, debug=True)


TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedstock Margin Monitor</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.6; }
  .wrap { max-width:1120px; margin:0 auto; padding:28px 20px 60px; }
  header { display:flex; align-items:baseline; justify-content:space-between;
    flex-wrap:wrap; gap:8px; border-bottom:1px solid var(--border); padding-bottom:16px; }
  h1 { font-size:20px; font-weight:600; margin:0; }
  .muted { color:var(--muted); }
  .grid { display:grid; gap:16px; }
  .card { background:var(--card); border:1px solid var(--border);
    border-radius:12px; padding:18px 20px; }
  .hero { display:grid; grid-template-columns:auto 1fr; gap:24px; align-items:center;
    margin-top:20px; }
  .index-num { font-size:64px; font-weight:700; line-height:1; }
  .index-sub { font-size:13px; color:var(--muted); margin-top:6px; }
  .band { font-size:15px; font-weight:600; }
  .alert-banner { border:1px solid #e24b4a; background:rgba(226,75,74,0.12);
    border-radius:10px; padding:14px 16px; margin-top:16px; }
  .alert-banner .t { color:#ff7b72; font-weight:700; letter-spacing:.5px; }
  .ok-banner { color:#97c459; font-size:14px; margin-top:16px; }
  .strip { grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); margin-top:18px; }
  .stat { background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:14px 16px; }
  .stat .l { font-size:12px; color:var(--muted); }
  .stat .v { font-size:22px; font-weight:600; margin:4px 0 2px; text-transform:capitalize; }
  .stat .s { font-size:12px; color:var(--muted); }
  section { margin-top:28px; }
  section h2 { font-size:15px; font-weight:600; margin:0 0 12px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.6px; }
  .drivers { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
  .driver .dlabel { font-size:12px; color:var(--muted); }
  .driver .dval { font-size:22px; font-weight:600; margin:4px 0 2px; }
  .driver .dpct { font-size:13px; }
  .up { color:#ff7b72; } .down { color:#3fb950; }
  .charts { grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }
  .chart-card h3 { font-size:14px; font-weight:600; margin:0 0 10px; }
  .canvas-wrap { position:relative; width:100%; height:240px; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:var(--muted);
    margin-bottom:8px; }
  .legend span { display:flex; align-items:center; gap:5px; }
  .swatch { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .narrative { font-size:15px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--border);
    color:var(--muted); margin-left:8px; vertical-align:middle; }
  footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--border);
    font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Petrochemical Feedstock Margin Monitor</h1>
    <div class="muted" style="font-size:14px;">As of {{ as_of }}</div>
  </header>

  {% if not has_data %}
    <div class="card" style="margin-top:24px;">
      No data yet. Run <code>python -m feedstock.run_daily</code> to ingest the
      first observations, then refresh.
    </div>
  {% else %}

  <div class="hero">
    <div>
      <div class="index-num" style="color:{{ index_color }}">{{ index }}</div>
      <div class="index-sub">/ 100 margin-pressure</div>
    </div>
    <div>
      <div class="band" style="color:{{ index_color }}">
        {% if not sufficient %}Neutral (building history)
        {% elif index >= 70 %}Elevated — margins under pressure
        {% elif index >= 58 %}Firming
        {% elif index > 42 %}Neutral
        {% else %}Subdued{% endif %}
      </div>
      <div class="muted" style="font-size:14px;">
        Composite z-score {{ composite_z }} · 50 = costs at trailing baseline
      </div>
      {% if alert.triggered %}
        <div class="alert-banner">
          <span class="t">⚠ MARGIN-PRESSURE ALERT</span><br>
          Index {{ index }} crossed the alert threshold of {{ alert.threshold|int }}
          ({{ "%+.1f"|format(alert.exceedance) }} above). Feedstock costs running
          materially hotter than baseline.
        </div>
      {% else %}
        <div class="ok-banner">✓ No alert — index below the
          {{ alert.threshold|int }} threshold.</div>
      {% endif %}
    </div>
  </div>

  <div class="grid strip">
    {% for s in stats %}
    <div class="stat">
      <div class="l">{{ s.label }}</div>
      <div class="v">{{ s.value }}</div>
      <div class="s">{{ s.sub }}</div>
    </div>
    {% endfor %}
  </div>

  {% if narrative %}
  <section>
    <h2>Analyst note
      {% if narrative_source %}<span class="pill">{{ narrative_source }}</span>{% endif %}
    </h2>
    <div class="card narrative">{{ narrative }}</div>
  </section>
  {% endif %}

  {% if backtest %}
  <section>
    <h2>Index validation · vs FRED coatings PPI</h2>
    <div class="grid strip">
      <div class="stat"><div class="l">Paired months</div>
        <div class="v">{{ backtest.n_paired }}</div><div class="s">FRED PPI sample</div></div>
      <div class="stat"><div class="l">Change correlation</div>
        <div class="v">{{ backtest.corr_changes }}</div><div class="s">Δindex vs ΔPPI</div></div>
      <div class="stat"><div class="l">Direction hit-rate</div>
        <div class="v">{{ backtest.direction_hit_rate }}</div><div class="s">same-sign months</div></div>
      <div class="stat"><div class="l">Index lead</div>
        <div class="v">{{ backtest.best_lead_months }}m</div><div class="s">best lead-lag</div></div>
    </div>
  </section>
  {% endif %}

  <section>
    <h2>Input-cost drivers</h2>
    <div class="grid drivers">
      {% for d in drivers %}
      <div class="card driver">
        <div class="dlabel">{{ d.label }}</div>
        <div class="dval" style="color:{{ d.color }}">{{ d.value }}</div>
        <div class="dpct {{ 'up' if d.pct_up else 'down' }}">{{ d.pct }} recent</div>
      </div>
      {% endfor %}
    </div>
  </section>

  <section>
    <h2>Trends</h2>
    <div class="grid charts">
      <div class="card chart-card">
        <h3>Margin-pressure index</h3>
        <div class="canvas-wrap"><canvas id="cIndex" role="img"
          aria-label="Margin-pressure index over time with alert threshold"></canvas></div>
      </div>
      <div class="card chart-card">
        <h3>Refining crack spreads ($/bbl)</h3>
        <div class="legend">
          <span><i class="swatch" style="background:{{ colors.gasoline_crack_usd_bbl }}"></i>Gasoline</span>
          <span><i class="swatch" style="background:{{ colors.distillate_crack_usd_bbl }}"></i>Distillate</span>
          <span><i class="swatch" style="background:{{ colors.crack_321_usd_bbl }}"></i>3:2:1</span>
        </div>
        <div class="canvas-wrap"><canvas id="cCracks" role="img"
          aria-label="Gasoline, distillate and 3:2:1 crack spreads over time"></canvas></div>
      </div>
      <div class="card chart-card">
        <h3>Crude oil ($/bbl)</h3>
        <div class="legend">
          <span><i class="swatch" style="background:{{ colors.brent_crude_usd_bbl }}"></i>Brent</span>
          <span><i class="swatch" style="background:{{ colors.wti_crude_usd_bbl }}"></i>WTI</span>
        </div>
        <div class="canvas-wrap"><canvas id="cCrude" role="img"
          aria-label="Brent and WTI crude over time"></canvas></div>
      </div>
      <div class="card chart-card">
        <h3>Refined products ($/gal)</h3>
        <div class="legend">
          <span><i class="swatch" style="background:{{ colors.rbob_gasoline_usd_gal }}"></i>RBOB</span>
          <span><i class="swatch" style="background:{{ colors.ulsd_diesel_usd_gal }}"></i>ULSD</span>
        </div>
        <div class="canvas-wrap"><canvas id="cProducts" role="img"
          aria-label="RBOB gasoline and ULSD diesel over time"></canvas></div>
      </div>
      <div class="card chart-card">
        <h3>Henry Hub natural gas ($/MMBtu)</h3>
        <div class="canvas-wrap"><canvas id="cGas" role="img"
          aria-label="Henry Hub natural gas over time"></canvas></div>
      </div>
      <div class="card chart-card">
        <h3>FX (EUR/USD left · USD/CNY right)</h3>
        <div class="legend">
          <span><i class="swatch" style="background:{{ colors.eur_usd }}"></i>EUR/USD</span>
          <span><i class="swatch" style="background:{{ colors.usd_cny }}"></i>USD/CNY</span>
        </div>
        <div class="canvas-wrap"><canvas id="cFx" role="img"
          aria-label="EUR/USD and USD/CNY over time"></canvas></div>
      </div>
    </div>
  </section>

  <footer>
    Data: Yahoo Finance (crude, gas, RBOB/ULSD products, WTI curve),
    frankfurter.app (FX), and — when keyed — EIA (Mont Belvieu propane) and FRED
    (coatings PPI). Crude &amp; refined products proxy true petrochemical
    feedstocks; TiO₂ pigment is not captured. Directional cost-pressure gauge,
    not a margin figure, and not investment advice.
  </footer>
  {% endif %}
</div>

{% if has_data %}
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
  const DATA = {{ charts|tojson }};
  const COLORS = {{ colors|tojson }};
  const GRID = "rgba(139,148,158,0.15)", TICK = "#8b949e";
  Chart.defaults.color = TICK;
  Chart.defaults.font.family = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif";

  const base = (extra) => Object.assign({
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    elements: { point: { radius: DATA.index.labels.length > 40 ? 0 : 3 } },
    scales: { x: { grid: { color: GRID }, ticks: { maxRotation: 0, autoSkip: true } },
              y: { grid: { color: GRID } } },
  }, extra || {});

  const line = (id, labels, datasets, extra) =>
    new Chart(document.getElementById(id),
      { type: "line", data: { labels, datasets }, options: base(extra) });

  const ds = (label, data, color, dash) => ({
    label, data, borderColor: color, tension: 0.2, spanGaps: true,
    borderDash: dash || [],
  });

  line("cIndex", DATA.index.labels, [
    { label: "Index", data: DATA.index.values, borderColor: "#7f77dd",
      backgroundColor: "rgba(127,119,221,0.15)", fill: true, tension: 0.2 },
    { label: "Threshold", data: DATA.index.labels.map(() => DATA.index.threshold),
      borderColor: "#e24b4a", borderDash: [6, 4], pointRadius: 0, fill: false },
  ], { scales: { x: { grid: { color: GRID } },
       y: { grid: { color: GRID }, min: 0, max: 100 } } });

  line("cCracks", DATA.cracks.labels, [
    ds("Gasoline", DATA.cracks.gasoline_crack_usd_bbl, COLORS.gasoline_crack_usd_bbl),
    ds("Distillate", DATA.cracks.distillate_crack_usd_bbl, COLORS.distillate_crack_usd_bbl),
    ds("3:2:1", DATA.cracks.crack_321_usd_bbl, COLORS.crack_321_usd_bbl, [5, 4]),
  ]);

  line("cCrude", DATA.crude.labels, [
    ds("Brent", DATA.crude.brent_crude_usd_bbl, COLORS.brent_crude_usd_bbl),
    ds("WTI", DATA.crude.wti_crude_usd_bbl, COLORS.wti_crude_usd_bbl, [5, 4]),
  ]);

  line("cProducts", DATA.products.labels, [
    ds("RBOB", DATA.products.rbob_gasoline_usd_gal, COLORS.rbob_gasoline_usd_gal),
    ds("ULSD", DATA.products.ulsd_diesel_usd_gal, COLORS.ulsd_diesel_usd_gal, [5, 4]),
  ]);

  line("cGas", DATA.gas.labels, [
    { label: "Henry Hub", data: DATA.gas.henry_hub_natgas_usd_mmbtu,
      borderColor: COLORS.henry_hub_natgas_usd_mmbtu,
      backgroundColor: "rgba(93,202,165,0.12)", fill: true, tension: 0.2, spanGaps: true },
  ]);

  line("cFx", DATA.fx.labels, [
    Object.assign(ds("EUR/USD", DATA.fx.eur_usd, COLORS.eur_usd), { yAxisID: "y" }),
    Object.assign(ds("USD/CNY", DATA.fx.usd_cny, COLORS.usd_cny, [5, 4]), { yAxisID: "y1" }),
  ], { scales: { x: { grid: { color: GRID } },
       y: { position: "left", grid: { color: GRID } },
       y1: { position: "right", grid: { display: false } } } });
</script>
{% endif %}
</body>
</html>
"""


if __name__ == "__main__":
    main()
