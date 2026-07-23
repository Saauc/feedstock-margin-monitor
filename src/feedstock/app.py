"""
app.py — interactive dark-theme dashboard for the feedstock margin monitor.

The server embeds the full accumulated history (dates, per-component directional
z-scores, raw values, crack spreads, refinery utilization, curve, backtest) into
the page as JSON. ALL interactivity is then client-side JavaScript:

  - a date scrubber to move through history (not just "today"),
  - weight sliders to reweight the coatings basket and watch the index recompute,
  - a plain-English summary + per-date analyst note that regenerate on the fly.

Because the recompute is client-side, the SAME page works as a static shareable
link (no backend) and as a live Render app. No API key needed to view.

Run locally:  python -m feedstock.app   (then open http://127.0.0.1:5000)
"""

from __future__ import annotations

import json

from flask import Flask, render_template_string

from feedstock import analysis, store, transform

app = Flask(__name__)

METRIC_COLORS = {
    "brent_crude_usd_bbl": "#f0997b",
    "wti_crude_usd_bbl": "#ef9f27",
    "henry_hub_natgas_usd_mmbtu": "#5dcaa5",
    "rbob_gasoline_usd_gal": "#97c459",
    "ulsd_diesel_usd_gal": "#d4537e",
    "propane_mont_belvieu_usd_gal": "#7f77dd",
    "eur_usd": "#85b7eb",
    "usd_cny": "#ed93b1",
}
CRACK_COLORS = {
    "gasoline_crack_usd_bbl": "#97c459",
    "distillate_crack_usd_bbl": "#ef9f27",
    "crack_321_usd_bbl": "#7f77dd",
}
CHART_METRICS = ["brent_crude_usd_bbl", "wti_crude_usd_bbl",
                 "henry_hub_natgas_usd_mmbtu", "rbob_gasoline_usd_gal",
                 "ulsd_diesel_usd_gal", "eur_usd", "usd_cny"]
CRACK_METRICS = ["gasoline_crack_usd_bbl", "distillate_crack_usd_bbl", "crack_321_usd_bbl"]


def _history(conn, metrics):
    return {m: [(r["date"], r["value"])
                for r in store.fetch_metric_history(conn, m)] for m in metrics}


@app.route("/")
def dashboard():
    config = analysis.load_config()
    components = config["components"]
    metrics = list(components.keys())

    store.init_db()
    conn = store.get_connection()
    try:
        series_map = analysis.build_series_map(conn, metrics)
        series = transform.compute_pressure_series(series_map, config)
        market = analysis.market_snapshot(conn, config)
        bt_row = store.latest_backtest(conn)
        chart_series = _history(conn, CHART_METRICS)
        crack_series = _history(conn, CRACK_METRICS)
        ref_hist = [(r["date"], r["value"])
                    for r in store.fetch_metric_history(conn, "refinery_utilization_pct")]
    finally:
        conn.close()

    dates = [r["date"] for r in series]

    # Directional z-scores per component per date (None where absent). These are
    # weight-independent, so the client can reweight the basket by re-averaging
    # them — no server round-trip.
    dirz = {m: [round(r["components"][m], 4) if m in r["components"] else None
                for r in series] for m in metrics}

    # Raw values aligned to the index date axis (components + WTI for charts).
    all_metrics = list(dict.fromkeys(metrics + CHART_METRICS))
    lookups = {}
    for m in all_metrics:
        pairs = series_map.get(m) or chart_series.get(m) or []
        lookups[m] = dict(pairs)
    values = {m: [round(lookups[m][d], 4) if d in lookups[m] else None for d in dates]
              for m in all_metrics}

    def ffill(pairs):
        d = dict(pairs)
        out, last = [], None
        for dt in dates:
            if dt in d:
                last = d[dt]
            out.append(round(last, 3) if last is not None else None)
        return out

    cracks = {m: ffill(crack_series.get(m, [])) for m in CRACK_METRICS}
    refinery = ffill(ref_hist)
    backtest = json.loads(bt_row["report_json"]) if bt_row else None

    cfg = {
        "metrics": metrics,
        "labels": {m: components[m].get("label", m) for m in metrics},
        "proxy": {m: components[m].get("proxy_for", "") for m in metrics},
        "weights": {m: round(float(components[m].get("weight", 0)), 3) for m in metrics},
        "k": float(config.get("logistic_k", 1.0)),
        "span": int(config.get("index_smoothing_span", 1)),
        "percentileWindow": int(config.get("percentile_window", 504)),
        "alertThreshold": float(config.get("alert_threshold", 70)),
    }
    data = {
        "dates": dates, "dirz": dirz, "values": values, "cracks": cracks,
        "refinery": refinery, "backtest": backtest, "cfg": cfg,
        "curve": {kk: market.get(kk) for kk in
                  ("curve_state", "wti_roll_yield_ann_pct",
                   "wti_front_usd_bbl", "wti_deferred_6m_usd_bbl")},
        "colors": {**METRIC_COLORS, **CRACK_COLORS},
        "chartMetrics": CHART_METRICS,
    }
    return render_template_string(TEMPLATE, data=data, has_data=bool(dates))


def main():
    import os
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)


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
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; line-height:1.6; }
  .wrap { max-width:1120px; margin:0 auto; padding:28px 20px 60px; }
  header { display:flex; align-items:baseline; justify-content:space-between; flex-wrap:wrap; gap:8px;
    border-bottom:1px solid var(--border); padding-bottom:16px; }
  h1 { font-size:20px; font-weight:600; margin:0; }
  .muted { color:var(--muted); }
  .grid { display:grid; gap:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 20px; }
  .summary { margin-top:20px; border-left:3px solid #7f77dd; font-size:17px; line-height:1.55; }
  .summary .sublabel { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px; margin-bottom:6px; }
  .controls { margin-top:16px; }
  .ctl-row { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .ctl-row label { font-size:13px; color:var(--muted); min-width:38px; }
  input[type=range] { -webkit-appearance:none; appearance:none; height:4px; background:var(--border);
    border-radius:2px; outline:none; }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:16px; height:16px; border-radius:50%;
    background:#7f77dd; cursor:pointer; }
  input[type=range]::-moz-range-thumb { width:16px; height:16px; border-radius:50%; background:#7f77dd; border:none; cursor:pointer; }
  #dateSlider { flex:1; min-width:220px; }
  button { background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px;
    padding:4px 10px; cursor:pointer; font-size:12px; }
  button:hover { border-color:var(--muted); }
  .ctl-head { display:flex; align-items:center; gap:12px; font-size:12px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.6px; margin:16px 0 10px; }
  .weights-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px 20px; }
  .wt { font-size:13px; }
  .wt .wt-top { display:flex; justify-content:space-between; margin-bottom:2px; }
  .wt .wt-val { color:var(--muted); font-variant-numeric:tabular-nums; }
  .wt input[type=range] { width:100%; }
  .hero { display:grid; grid-template-columns:auto 1fr; gap:24px; align-items:center; margin-top:20px; }
  .index-num { font-size:64px; font-weight:700; line-height:1; }
  .index-sub { font-size:13px; color:var(--muted); margin-top:6px; }
  .band { font-size:15px; font-weight:600; }
  .alert-banner { border:1px solid #e24b4a; background:rgba(226,75,74,0.12); border-radius:10px;
    padding:14px 16px; margin-top:16px; }
  .alert-banner .t { color:#ff7b72; font-weight:700; letter-spacing:.5px; }
  .ok-banner { color:#97c459; font-size:14px; margin-top:16px; }
  .strip { grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); margin-top:18px; }
  .stat { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
  .stat .l { font-size:12px; color:var(--muted); }
  .stat .v { font-size:22px; font-weight:600; margin:4px 0 2px; text-transform:capitalize; }
  .stat .s { font-size:12px; color:var(--muted); }
  section { margin-top:28px; }
  section h2 { font-size:15px; font-weight:600; margin:0 0 12px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px; }
  .drivers { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
  .driver { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
  .driver .dlabel { font-size:12px; color:var(--muted); }
  .driver .dval { font-size:22px; font-weight:600; margin:4px 0 2px; }
  .driver .dpct { font-size:13px; }
  .up { color:#ff7b72; } .down { color:#3fb950; }
  .charts { grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }
  .chart-card h3 { font-size:14px; font-weight:600; margin:0 0 10px; }
  .canvas-wrap { position:relative; width:100%; height:240px; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:var(--muted); margin-bottom:8px; }
  .legend span { display:flex; align-items:center; gap:5px; }
  .swatch { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .narrative { font-size:15px; }
  .cover p { margin:0 0 10px; font-size:14px; line-height:1.7; }
  footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--border); font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Petrochemical Feedstock Margin Monitor</h1>
    <div class="muted" style="font-size:14px;">As of <span id="asOf">—</span></div>
  </header>

  {% if not has_data %}
    <div class="card" style="margin-top:24px;">No data yet — run <code>python -m feedstock.run_daily</code>.</div>
  {% else %}

  <div class="card summary">
    <div class="sublabel">In plain terms</div>
    <div id="plainSummary">—</div>
  </div>

  <div class="card controls">
    <div class="ctl-row">
      <label for="dateSlider">Date</label>
      <input type="range" id="dateSlider" min="0" value="0">
      <span id="dateLabel" class="muted" style="min-width:92px; font-variant-numeric:tabular-nums;"></span>
      <button id="latestBtn">Latest</button>
    </div>
    <div class="ctl-head">Reweight the cost basket — see how the index shifts <button id="resetBtn">Reset</button></div>
    <div class="weights-grid" id="weightSliders"></div>
  </div>

  <div class="hero">
    <div>
      <div class="index-num" id="idxNum">—</div>
      <div class="index-sub">/ 100 margin-pressure</div>
    </div>
    <div>
      <div class="band" id="band">—</div>
      <div class="muted" style="font-size:14px;" id="idxSub"></div>
      <div id="alertBox"></div>
    </div>
  </div>

  <div class="grid strip">
    <div class="stat"><div class="l">Historical percentile</div><div class="v" id="stPct">—</div><div class="s">of trailing window</div></div>
    <div class="stat"><div class="l">Index vol regime</div><div class="v" id="stVol">—</div><div class="s">recent vs history</div></div>
    <div class="stat"><div class="l">3:2:1 crack</div><div class="v" id="stCrack">—</div><div class="s">refining margin</div></div>
    <div class="stat"><div class="l">WTI curve (current)</div><div class="v" id="stCurve">—</div><div class="s" id="stCurveSub">—</div></div>
    <div class="stat"><div class="l">Refinery utilization</div><div class="v" id="stRef">—</div><div class="s">EIA (US)</div></div>
  </div>

  <section>
    <h2>Analyst note · for the selected date</h2>
    <div class="card narrative" id="note">—</div>
  </section>

  <section>
    <h2>Input-cost drivers</h2>
    <div class="grid drivers" id="drivers"></div>
  </section>

  <section>
    <h2>What this does &amp; doesn't cover</h2>
    <div class="card cover">
      <p><strong style="color:#97c459;">Tracks</strong> — the petroleum- and energy-linked slice of coatings input cost: propane (a real resin/propylene feedstock), gasoline &amp; diesel as naphtha/solvent proxies, natural gas for process energy, crude as the anchor.</p>
      <p><strong style="color:#e24b4a;">Excludes</strong> — TiO₂ pigment, the single largest coatings raw material (~20–25% of cost), because no free daily price source exists for it; also labor, packaging, and producer margin.</p>
      <p class="muted">So the weak correlation to the real FRED coatings PPI (below) is the <em>expected, validated consequence</em> of that gap — the PPI is dominated by inputs this index can't see. It's a directional read on the tradeable slice, not a PPI predictor. A documented limitation, not a bug.</p>
    </div>
  </section>

  <section>
    <h2>Index validation · vs FRED coatings PPI</h2>
    <div class="grid strip" id="backtest"></div>
  </section>

  <section>
    <h2>Trends</h2>
    <div class="grid charts">
      <div class="card chart-card"><h3>Margin-pressure index <span class="muted" style="font-weight:400;font-size:12px;">(reweights live; marker = selected date)</span></h3>
        <div class="canvas-wrap"><canvas id="cIndex"></canvas></div></div>
      <div class="card chart-card"><h3>Refining crack spreads ($/bbl)</h3>
        <div class="legend"><span><i class="swatch" style="background:#97c459"></i>Gasoline</span><span><i class="swatch" style="background:#ef9f27"></i>Distillate</span><span><i class="swatch" style="background:#7f77dd"></i>3:2:1</span></div>
        <div class="canvas-wrap"><canvas id="cCracks"></canvas></div></div>
      <div class="card chart-card"><h3>Crude oil ($/bbl)</h3>
        <div class="legend"><span><i class="swatch" style="background:#f0997b"></i>Brent</span><span><i class="swatch" style="background:#ef9f27"></i>WTI</span></div>
        <div class="canvas-wrap"><canvas id="cCrude"></canvas></div></div>
      <div class="card chart-card"><h3>Refined products ($/gal)</h3>
        <div class="legend"><span><i class="swatch" style="background:#97c459"></i>RBOB</span><span><i class="swatch" style="background:#d4537e"></i>ULSD</span></div>
        <div class="canvas-wrap"><canvas id="cProducts"></canvas></div></div>
      <div class="card chart-card"><h3>Henry Hub natural gas ($/MMBtu)</h3>
        <div class="canvas-wrap"><canvas id="cGas"></canvas></div></div>
      <div class="card chart-card"><h3>FX (EUR/USD left · USD/CNY right)</h3>
        <div class="legend"><span><i class="swatch" style="background:#85b7eb"></i>EUR/USD</span><span><i class="swatch" style="background:#ed93b1"></i>USD/CNY</span></div>
        <div class="canvas-wrap"><canvas id="cFx"></canvas></div></div>
    </div>
  </section>

  <footer>
    Data: Yahoo Finance (crude, gas, RBOB/ULSD products, WTI curve), frankfurter.app (FX), EIA (Mont Belvieu propane, refinery utilization), FRED (coatings PPI).
    Crude &amp; refined products proxy true petrochemical feedstocks; TiO₂ pigment is not captured. Directional cost-pressure gauge, not a margin figure, and not investment advice.
  </footer>
  {% endif %}
</div>

{% if has_data %}
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = {{ data|tojson }};
const CFG = D.cfg, M = CFG.metrics, N = D.dates.length;
let selIdx = N - 1;
const weights = Object.assign({}, CFG.weights);

// --- math ---------------------------------------------------------------
const logistic = x => x <= -60 ? 0 : x >= 60 ? 1 : 1/(1+Math.exp(-x));
const stdev = a => { if(a.length<2) return 0; const m=a.reduce((s,x)=>s+x,0)/a.length;
  return Math.sqrt(a.reduce((s,x)=>s+(x-m)*(x-m),0)/a.length); };
function pctlOf(arr, v){ let c=0; for(const x of arr) if(x<=v) c++; return arr.length? 100*c/arr.length : null; }
function pctChange(arr, t, lb){ const a=arr[t], b=arr[Math.max(0,t-lb)]; return (a!=null&&b)? (a/b-1)*100 : null; }

// Recompute the whole index series for a given weighting (client-side).
function indexSeries(w){
  const alpha = 2/(CFG.span+1); let sm=null; const out=[];
  for(let t=0;t<N;t++){
    let num=0, wsum=0;
    for(const m of M){ const z=D.dirz[m][t]; if(z==null) continue; num+=w[m]*z; wsum+=Math.abs(w[m]); }
    const comp = wsum>0 ? num/wsum : 0;
    sm = sm==null ? comp : alpha*comp+(1-alpha)*sm;
    out.push(100*logistic(CFG.k*sm));
  }
  return out;
}
const band = i => i>=70?"elevated":i>=58?"firming":i>42?"neutral":i>30?"easing":"subdued";
const bandColor = i => i>=70?"#e24b4a":i>=58?"#ef9f27":i>42?"#8b949e":"#97c459";
const bandLabel = i => i>=70?"Elevated — margins under pressure":i>=58?"Firming":i>42?"Neutral":i>30?"Easing":"Subdued";

function volRegime(series, t){
  const rets=[]; for(let i=1;i<=t;i++){ if(series[i-1]) rets.push(series[i]/series[i-1]-1); }
  if(rets.length<30) return "—";
  const win=20, roll=[]; for(let i=win;i<rets.length;i++) roll.push(stdev(rets.slice(i-win,i)));
  const cur=stdev(rets.slice(-win)); const p=pctlOf(roll, cur);
  return p>=80?"elevated":p<=20?"subdued":"normal";
}

function plainSummary(series){
  const t=selIdx, now=series[t];
  const wk=series[Math.max(0,t-5)], mo=series[Math.max(0,t-21)];
  const dW=now-wk;
  const wkWord = Math.abs(dW)<3?"held roughly steady":dW<0?"eased":"climbed";
  const level = now>=70?"high":now>=58?"elevated":now>42?"around its normal range":now>30?"on the soft side":"low";
  const vsMonth = now>mo+2?"up from a month ago":now<mo-2?"down from a month ago":"about level with a month ago";
  const alert = now>=CFG.alertThreshold;
  return `Feedstock cost pressure has ${wkWord} over the past week and sits ${level} — ${vsMonth}. `
       + (alert ? "⚠ It has crossed the alert threshold." : "No alert triggered.");
}

function analystNote(series){
  const t=selIdx, idx=series[t];
  const movers = M.filter(m=>D.dirz[m][t]!=null)
    .sort((a,b)=>Math.abs(D.dirz[b][t])-Math.abs(D.dirz[a][t])).slice(0,3)
    .map(m=>{ const pc=pctChange(D.values[m], t, 5);
      return CFG.labels[m] + (pc!=null? ` (${pc>=0?"+":""}${pc.toFixed(1)}% recent)` : ""); });
  const pctWin = series.slice(Math.max(0,t-CFG.percentileWindow+1), t+1);
  const pct = pctlOf(pctWin, idx);
  const crk = D.cracks.crack_321_usd_bbl[t];
  const art = "aeiou".includes(band(idx)[0]) ? "an" : "a";
  let s = `As of ${D.dates[t]}, the margin-pressure index reads ${Math.round(idx)}/100 — ${art} ${band(idx)} level `
        + `(50 = costs at their trailing baseline). Largest contributors: ${movers.join("; ")}. `;
  const bits = [];
  if(pct!=null) bits.push(`that's the ${Math.round(pct)}th percentile of the trailing window`);
  if(D.curve.curve_state && D.curve.wti_roll_yield_ann_pct!=null)
    bits.push(`the WTI curve is in ${D.curve.curve_state} (${D.curve.wti_roll_yield_ann_pct>=0?"+":""}${D.curve.wti_roll_yield_ann_pct.toFixed(1)}%/yr roll)`);
  if(crk!=null) bits.push(`the 3:2:1 refining crack is $${crk.toFixed(1)}/bbl`);
  if(bits.length) s += "Context: " + bits.join("; ") + ". ";
  s += "Crude and refined products proxy true petrochemical feedstocks (TiO₂ pigment is not captured); read this as a directional gauge, not a precise margin figure.";
  return s;
}

// --- DOM ----------------------------------------------------------------
const $ = id => document.getElementById(id);
let idxChart;

function buildWeightSliders(){
  const host=$("weightSliders");
  host.innerHTML = M.map(m=>{
    const w=Math.round(CFG.weights[m]*100);
    return `<div class="wt"><div class="wt-top"><span>${CFG.labels[m]}</span><span class="wt-val" id="wv_${m}">${w}%</span></div>`
      + `<input type="range" min="0" max="40" step="1" value="${w}" data-m="${m}" class="wslider"></div>`;
  }).join("");
  host.querySelectorAll(".wslider").forEach(el=>{
    el.addEventListener("input", e=>{ const m=e.target.dataset.m; weights[m]=(+e.target.value)/100;
      $("wv_"+m).textContent=e.target.value+"%"; render(true); });
  });
}

function renderBacktest(){
  const b=D.backtest, host=$("backtest");
  if(!b){ host.innerHTML='<div class="stat"><div class="v">—</div><div class="s">no backtest yet</div></div>'; return; }
  const card=(l,v,s)=>`<div class="stat"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  host.innerHTML = card("Paired months", b.n_paired, "FRED PPI sample")
    + card("Change correlation", b.corr_changes, "Δindex vs ΔPPI")
    + card("Direction hit-rate", b.direction_hit_rate, "same-sign months")
    + card("Index lead", (b.best_lead_months??"—")+"m", "best lead-lag");
}

function render(weightsChanged){
  const series = indexSeries(weights);
  const t=selIdx, idx=series[t], col=bandColor(idx);
  $("asOf").textContent = D.dates[t];
  $("dateLabel").textContent = D.dates[t];
  $("idxNum").textContent = Math.round(idx); $("idxNum").style.color=col;
  $("band").textContent = bandLabel(idx); $("band").style.color=col;
  const pctWin = series.slice(Math.max(0,t-CFG.percentileWindow+1), t+1);
  const pct = pctlOf(pctWin, idx);
  $("idxSub").textContent = `Composite signal · 50 = costs at trailing baseline`;
  $("stPct").textContent = pct!=null? Math.round(pct)+"th" : "—";
  $("stVol").textContent = volRegime(series, t);
  const crk=D.cracks.crack_321_usd_bbl[t];
  $("stCrack").textContent = crk!=null? crk.toFixed(1)+" $/bbl" : "—";
  $("stCurve").textContent = D.curve.curve_state? D.curve.curve_state[0].toUpperCase()+D.curve.curve_state.slice(1) : "—";
  $("stCurveSub").textContent = D.curve.wti_roll_yield_ann_pct!=null? `roll ${D.curve.wti_roll_yield_ann_pct>=0?"+":""}${D.curve.wti_roll_yield_ann_pct.toFixed(1)}%/yr` : "—";
  const ref=D.refinery[t]; $("stRef").textContent = ref!=null? ref.toFixed(1)+"%" : "—";
  const alert = idx>=CFG.alertThreshold;
  $("alertBox").innerHTML = alert
    ? `<div class="alert-banner"><span class="t">⚠ MARGIN-PRESSURE ALERT</span><br>Index ${Math.round(idx)} is at/above the ${CFG.alertThreshold} threshold.</div>`
    : `<div class="ok-banner">✓ No alert — index below the ${CFG.alertThreshold} threshold.</div>`;
  $("plainSummary").textContent = plainSummary(series);
  $("note").textContent = analystNote(series);
  // drivers
  $("drivers").innerHTML = M.map(m=>{
    const v=D.values[m][t], pc=pctChange(D.values[m], t, 5);
    const up = pc!=null && pc>=0;
    return `<div class="driver"><div class="dlabel">${CFG.labels[m]}</div>`
      + `<div class="dval" style="color:${D.colors[m]||"#8b949e"}">${v!=null? v.toFixed(3):"—"}</div>`
      + `<div class="dpct ${up?"up":"down"}">${pc!=null? (pc>=0?"+":"")+pc.toFixed(1)+"%":"—"} recent</div></div>`;
  }).join("");
  // index chart
  idxChart.data.datasets[0].data = series;
  idxChart._marker = t;
  idxChart.update(weightsChanged? undefined : "none");
}

// --- charts -------------------------------------------------------------
const GRID="rgba(139,148,158,0.15)", TICK="#8b949e";
Chart.defaults.color=TICK; Chart.defaults.font.family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif";
const marker={ id:"marker", afterDraw(c){ if(c._marker==null) return; const x=c.scales.x.getPixelForValue(c._marker);
  const {top,bottom}=c.chartArea, ctx=c.ctx; ctx.save(); ctx.strokeStyle="#e6edf3"; ctx.lineWidth=1; ctx.setLineDash([4,3]);
  ctx.beginPath(); ctx.moveTo(x,top); ctx.lineTo(x,bottom); ctx.stroke(); ctx.restore(); } };
Chart.register(marker);
const baseOpts=(ex)=>Object.assign({responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
  elements:{point:{radius:0}}, scales:{x:{grid:{color:GRID},ticks:{maxRotation:0,autoSkip:true,maxTicksLimit:7}},y:{grid:{color:GRID}}}}, ex||{});
const line=(id,labels,ds,ex)=>new Chart($(id),{type:"line",data:{labels,datasets:ds},options:baseOpts(ex)});
const ds=(lab,data,color,dash)=>({label:lab,data,borderColor:color,borderWidth:1.6,tension:0.2,spanGaps:true,borderDash:dash||[]});

idxChart = new Chart($("cIndex"), { type:"line", data:{ labels:D.dates, datasets:[
  { label:"Index", data:indexSeries(weights), borderColor:"#7f77dd", backgroundColor:"rgba(127,119,221,0.15)", fill:true, tension:0.2, borderWidth:1.6, pointRadius:0 },
  { label:"Threshold", data:D.dates.map(()=>CFG.alertThreshold), borderColor:"#e24b4a", borderDash:[6,4], pointRadius:0, fill:false, borderWidth:1.2 } ]},
  options: baseOpts({scales:{x:{grid:{color:GRID},ticks:{maxRotation:0,autoSkip:true,maxTicksLimit:7}},y:{min:0,max:100,grid:{color:GRID}}}}) });

line("cCracks", D.dates, [ds("Gasoline",D.cracks.gasoline_crack_usd_bbl,"#97c459"),
  ds("Distillate",D.cracks.distillate_crack_usd_bbl,"#ef9f27"), ds("3:2:1",D.cracks.crack_321_usd_bbl,"#7f77dd",[5,4])]);
line("cCrude", D.dates, [ds("Brent",D.values.brent_crude_usd_bbl,"#f0997b"), ds("WTI",D.values.wti_crude_usd_bbl,"#ef9f27",[5,4])]);
line("cProducts", D.dates, [ds("RBOB",D.values.rbob_gasoline_usd_gal,"#97c459"), ds("ULSD",D.values.ulsd_diesel_usd_gal,"#d4537e",[5,4])]);
line("cGas", D.dates, [{label:"Henry Hub",data:D.values.henry_hub_natgas_usd_mmbtu,borderColor:"#5dcaa5",backgroundColor:"rgba(93,202,165,0.12)",fill:true,tension:0.2,borderWidth:1.6,pointRadius:0,spanGaps:true}]);
line("cFx", D.dates, [Object.assign(ds("EUR/USD",D.values.eur_usd,"#85b7eb"),{yAxisID:"y"}), Object.assign(ds("USD/CNY",D.values.usd_cny,"#ed93b1",[5,4]),{yAxisID:"y1"})],
  {scales:{x:{grid:{color:GRID},ticks:{maxRotation:0,autoSkip:true,maxTicksLimit:7}},y:{position:"left",grid:{color:GRID}},y1:{position:"right",grid:{display:false}}}});

// --- controls -----------------------------------------------------------
const dateSlider=$("dateSlider"); dateSlider.max=N-1; dateSlider.value=N-1;
dateSlider.addEventListener("input", e=>{ selIdx=+e.target.value; render(false); });
$("latestBtn").addEventListener("click", ()=>{ selIdx=N-1; dateSlider.value=N-1; render(false); });
$("resetBtn").addEventListener("click", ()=>{ Object.assign(weights, CFG.weights);
  document.querySelectorAll(".wslider").forEach(el=>{ const m=el.dataset.m; const w=Math.round(CFG.weights[m]*100);
    el.value=w; $("wv_"+m).textContent=w+"%"; }); render(true); });

buildWeightSliders(); renderBacktest(); render(true);
</script>
{% endif %}
</body>
</html>
"""
