"""
narrative.py — turn the computed numbers into a short analytical paragraph.

This is the "AI layer". It hands the margin-pressure index, the component
z-scores, and a little recent history to Claude and asks for a tight, factual
read of what moved and what to watch. The whole thing is wrapped so that if the
API is unavailable for ANY reason — no key, auth failure, network down, rate
limit, even a safety refusal — we fall back to a deterministic templated
summary built from the same numbers. The daily run must never hard-fail just
because the LLM call didn't go through.

Design choices (informed by the Anthropic API reference):
  - Official `anthropic` SDK, model `claude-opus-4-8`, single `messages.create`.
  - Short factual output → modest max_tokens, non-streaming (well under the
    SDK timeout), adaptive thinking left OFF for low latency / determinism in a
    daily automated job. Flip `THINKING` to enable it if desired.
  - The prompt is deliberately tight and tells the model the crude-as-proxy
    caveat so it doesn't overclaim precision it doesn't have.

The key is read from a local `.env` (gitignored) — see `_load_dotenv`.
"""

from __future__ import annotations

import os
from pathlib import Path

from feedstock.paths import ENV_PATH

MODEL = "claude-opus-4-8"
MAX_TOKENS = 600
# Adaptive thinking is off by default on Opus 4.8 when omitted; we keep it off
# for a fast, deterministic daily paragraph. To enable: THINKING = {"type": "adaptive"}.
THINKING = None


def _load_dotenv(path: Path = ENV_PATH) -> None:
    """Minimal .env loader: KEY=VALUE lines into os.environ.

    Prefers python-dotenv if it's installed (handles quoting/export edge
    cases), but falls back to a tiny parser so the project has no hard
    dependency on it just to read one key. Existing environment variables are
    not overwritten — a key already exported in the shell or CI wins.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=False)
        return
    except Exception:
        pass

    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# --- context building -----------------------------------------------------

def _pct_change(series: list, lookback: int = 5) -> float | None:
    """Percent change of a metric's value over the last `lookback` steps.

    `series` is [(date, value), ...] ascending. Returns None if there isn't
    enough history or the earlier value is zero.
    """
    if len(series) < 2:
        return None
    latest = series[-1][1]
    prior = series[-min(lookback + 1, len(series))][1]
    if prior == 0:
        return None
    return (latest - prior) / prior * 100.0


def build_context(snapshot: dict, series_map: dict, config: dict,
                  market: dict | None = None) -> dict:
    """Assemble the compact, factual context block we pass to the model.

    Everything here is derived straight from the data — no interpretation yet.
    That's the model's job; we just give it clean inputs. `market` carries the
    quant-layer signals (percentile, vol regime, crack spreads, curve) so the
    note can cite them.
    """
    components = config["components"]
    rows = []
    for metric, cfg in components.items():
        series = series_map.get(metric, [])
        latest = series[-1][1] if series else None
        rows.append({
            "name": cfg.get("label", metric),
            "latest_value": latest,
            "pct_change_recent": (
                round(c, 2) if (c := _pct_change(series)) is not None else None
            ),
            "directional_z": snapshot["components"].get(metric),
            "direction": "cost-up=pressure-up" if cfg.get("direction", 1) > 0
                         else "value-up=pressure-down",
        })
    return {
        "as_of": snapshot["date"],
        "margin_pressure_index": snapshot["index"],
        "composite_z": snapshot["composite_z"],
        "sufficient_history": snapshot["sufficient"],
        "components": rows,
        "market_signals": market or {},
    }


# --- templated fallback ---------------------------------------------------

def _band(index: float) -> str:
    if index >= 70:
        return "elevated"
    if index >= 58:
        return "firming"
    if index > 42:
        return "neutral"
    if index > 30:
        return "easing"
    return "subdued"


def templated_summary(context: dict) -> str:
    """Deterministic fallback paragraph built from the numbers alone.

    Used whenever the API call can't be made or fails. It's intentionally
    plain — no invented causation — but still informative.
    """
    idx = context["margin_pressure_index"]
    band = _band(idx)
    as_of = context["as_of"] or "the latest session"

    if not context["sufficient_history"]:
        return (
            f"As of {as_of}, the margin-pressure index reads {idx:.0f}/100 "
            f"(neutral baseline). There isn't yet enough trailing history to "
            f"judge whether feedstock costs are running hot or cool — the index "
            f"will become meaningful as daily observations accumulate. "
            f"Note: crude and natural gas are used as proxies for true "
            f"petrochemical feedstock costs."
        )

    # Pick the components moving the index most (largest |directional z|).
    movers = sorted(
        (c for c in context["components"] if c["directional_z"] is not None),
        key=lambda c: abs(c["directional_z"]),
        reverse=True,
    )[:3]
    parts = []
    for c in movers:
        chg = c["pct_change_recent"]
        chg_txt = f" ({chg:+.1f}% recent)" if chg is not None else ""
        parts.append(f"{c['name']}{chg_txt}")
    movers_txt = "; ".join(parts) if parts else "no single dominant driver"

    article = "an" if band[0] in "aeiou" else "a"
    text = (
        f"As of {as_of}, the margin-pressure index sits at {idx:.0f}/100 — {article} "
        f"{band} reading (50 = costs at their trailing baseline). The largest "
        f"contributors right now: {movers_txt}. A higher index means input "
        f"costs are climbing faster than their recent norm, squeezing "
        f"coatings/paint margins."
    )

    # Append quant context when available (percentile, curve, refining margin).
    m = context.get("market_signals", {})
    extras = []
    if m.get("historical_percentile") is not None:
        extras.append(f"that's the {m['historical_percentile']:.0f}th percentile "
                      f"of its trailing window")
    if m.get("vol_regime") and m["vol_regime"] != "unknown":
        extras.append(f"volatility is {m['vol_regime']}")
    if m.get("curve_state") and m.get("wti_roll_yield_ann_pct") is not None:
        extras.append(f"the WTI curve is in {m['curve_state']} "
                      f"({m['wti_roll_yield_ann_pct']:+.1f}%/yr roll)")
    if m.get("crack_321_usd_bbl") is not None:
        extras.append(f"the 3:2:1 refining crack is ${m['crack_321_usd_bbl']:.1f}/bbl")
    if extras:
        text += " Context: " + "; ".join(extras) + "."

    text += (" Crude and refined products proxy true petrochemical feedstocks "
             "(TiO2 pigment is not captured); read this as a directional gauge, "
             "not a precise margin figure.")
    return text


# --- the AI call ----------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a commodities analyst writing a daily desk note on petrochemical "
    "feedstock cost pressure for coatings/paint manufacturers. Be factual and "
    "specific; cite the actual numbers you are given, including — where present "
    "in market_signals — the historical percentile (e.g. '88th percentile of "
    "the trailing window'), the volatility regime, the crack spreads in $/bbl, "
    "and the crude curve shape (backwardation/contango) with its roll yield. No "
    "hype, no hedging filler, no investment advice. The 'margin-pressure index' "
    "runs 0-100 where 50 means input costs sit at their trailing baseline and "
    "higher means costs are climbing faster than their recent norm. Crude and "
    "refined products are PROXIES for real petrochemical feedstocks (naphtha, "
    "propylene; propane is the one true feedstock when present) — do not imply "
    "more precision than that, and note TiO2 pigment is not captured. Write "
    "100-150 words, plain prose, three beats: what moved, why margin pressure "
    "shifted, what to watch next."
)


def _format_user_message(context: dict) -> str:
    import json
    return (
        "Here is today's computed data. Interpret it for the daily note.\n\n"
        + json.dumps(context, indent=2)
    )


def generate_narrative(snapshot: dict, series_map: dict, config: dict,
                       market: dict | None = None) -> tuple[str, str]:
    """Return (narrative_text, source) where source is 'ai' or 'template'.

    Tries the Anthropic API; on any failure returns the templated summary.
    """
    context = build_context(snapshot, series_map, config, market)
    _load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # No key configured — expected in local/dev runs. Quietly fall back.
        return templated_summary(context), "template"

    try:
        import anthropic

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        messages: list[anthropic.types.MessageParam] = [
            {"role": "user", "content": _format_user_message(context)}
        ]
        if THINKING is None:
            response = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS,
                system=_SYSTEM_PROMPT, messages=messages,
            )
        else:
            response = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS,
                system=_SYSTEM_PROMPT, messages=messages, thinking=THINKING,
            )

        # A safety refusal returns HTTP 200 with stop_reason 'refusal' and no
        # usable text — treat it like any other failure and fall back.
        if response.stop_reason == "refusal":
            print("  [narrative] model declined; using templated summary.")
            return templated_summary(context), "template"

        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            return templated_summary(context), "template"
        return text, "ai"

    except Exception as exc:  # noqa: BLE001 — any failure must degrade gracefully
        # Auth, connection, rate limit, import error, etc. The whole point of
        # this layer is that a bad API call never breaks the daily run.
        print(f"  [narrative] API call failed ({type(exc).__name__}); "
              f"using templated summary.")
        return templated_summary(context), "template"


def run_narrative(persist: bool = True) -> tuple[str, str]:
    """Compute today's snapshot, generate the narrative, optionally persist it."""
    from feedstock import analysis
    from feedstock.store import get_connection, init_db, save_narrative

    config = analysis.load_config()
    metrics = list(config["components"].keys())

    init_db()
    conn = get_connection()
    try:
        series_map = analysis.build_series_map(conn, metrics)
        from feedstock import transform
        snapshot = transform.compute_latest_pressure(series_map, config)
        market = analysis.market_snapshot(conn, config)

        text, source = generate_narrative(snapshot, series_map, config, market)

        print("=" * 60)
        print(f"  DAILY NARRATIVE  ({source.upper()})")
        print("=" * 60)
        print(text)
        print("=" * 60)

        if persist and snapshot["date"] is not None:
            save_narrative(conn, snapshot["date"], text, source)
        return text, source
    finally:
        conn.close()


if __name__ == "__main__":
    run_narrative()
