"""
ingest.py — pull daily feedstock-relevant data from free public sources.

Sources (all free, no API key):
  - Yahoo Finance public chart API  -> Brent, WTI crude, Henry Hub natural gas
  - frankfurter.app (ECB reference) -> EUR/USD and a couple of relevant FX pairs

Design principles:
  - One function per source. Each is self-contained and wraps its own network
    I/O in try/except so a single source failing (Yahoo down, FX rate-limited)
    degrades gracefully — we still ingest whatever else succeeded.
  - Functions RETURN observations, they don't write to the DB. Keeping fetch
    and persist separate makes each unit testable and the data flow obvious.
  - We tag each observation with the *market date reported by the source*, not
    "today". That's what makes re-runs idempotent: the same trading day always
    maps to the same date key, so store.py dedupes it.

We deliberately use Yahoo's chart JSON endpoint via `requests` instead of the
`yfinance` package: it's the same data, needs no extra dependency, and behaves
predictably in CI (yfinance is prone to rate-limit/version breakage).

An "observation" here is a 4-tuple: (date, source, metric, value).
"""

from __future__ import annotations

import datetime as dt
import json

import requests

from feedstock import http, official_data, spreads
from feedstock.paths import CONFIG_PATH as _CONFIG_PATH
from feedstock.store import get_connection, init_db, latest_observations, record_many

Observation = tuple[str, str, str, float]

# A browser-like UA avoids Yahoo's occasional 429s on default python-requests.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; feedstock-monitor/1.0)"}
_TIMEOUT = 20  # seconds; fail fast rather than hang a scheduled run

# Yahoo Finance ticker -> (source, metric) we store it under.
# Continuous front-month NYMEX/ICE futures. Crude + gas anchor the chain;
# the refined PRODUCTS (RBOB gasoline, ULSD) are what make crack spreads
# possible — margin is product-minus-crude, not the crude level (see spreads.py).
#   BZ=F Brent, CL=F WTI, NG=F Henry Hub gas, RB=F RBOB gasoline, HO=F ULSD.
YAHOO_TICKERS = {
    "BZ=F": ("yahoo_finance", "brent_crude_usd_bbl"),
    "CL=F": ("yahoo_finance", "wti_crude_usd_bbl"),
    "NG=F": ("yahoo_finance", "henry_hub_natgas_usd_mmbtu"),
    "RB=F": ("yahoo_finance", "rbob_gasoline_usd_gal"),
    "HO=F": ("yahoo_finance", "ulsd_diesel_usd_gal"),
}

# frankfurter pairs we care about for coatings/paint feedstock economics.
# EUR/USD: most European producers cost-base in EUR, sell vs USD crude.
# USD/CNY: China is the swing producer of many petrochemical intermediates.
FX_PAIRS = [
    ("EUR", "USD", "eur_usd"),   # USD per 1 EUR
    ("USD", "CNY", "usd_cny"),   # CNY per 1 USD
]


def fetch_yahoo_metric(ticker: str, source: str, metric: str) -> list[Observation]:
    """Fetch the latest daily close for one Yahoo ticker.

    Returns a list with zero or one observation. We ask for a 5-day window and
    take the most recent *non-null* close, because the current day's bar can
    exist with a null close before/around the market open. The observation date
    is the trading date of that close (from Yahoo's own timestamps, in UTC).
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": "1d", "range": "5d"}
    try:
        resp = http.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        # Exchange UTC offset in seconds. Adding it before formatting the date
        # yields the exchange's *local trading date* — the date a trader would
        # actually name — instead of the UTC date. For NYMEX that happens to be
        # the same today, but this prevents an off-by-one for any source whose
        # bar timestamp crosses midnight relative to UTC.
        gmtoffset = result.get("meta", {}).get("gmtoffset", 0) or 0
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]

        # Walk backwards to the last bar that actually has a close price.
        for ts, close in zip(reversed(timestamps), reversed(closes)):
            if close is not None:
                date = dt.datetime.fromtimestamp(
                    ts + gmtoffset, tz=dt.UTC
                ).strftime("%Y-%m-%d")
                return [(date, source, metric, round(float(close), 4))]

        print(f"  [warn] {ticker}: no non-null close in window")
        return []
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as exc:
        # Catch unexpected JSON shapes too: a 200 response can carry
        # "result": null (soft errors / rate-limiting), and None[0] raises
        # TypeError. We log and return empty so the OTHER sources still run —
        # one source failing must never abort the whole ingest.
        print(f"  [error] Yahoo fetch failed for {ticker}: {exc!r}")
        return []


def fetch_crude_and_gas() -> list[Observation]:
    """Fetch all three energy benchmarks (Brent, WTI, Henry Hub)."""
    observations: list[Observation] = []
    for ticker, (source, metric) in YAHOO_TICKERS.items():
        observations.extend(fetch_yahoo_metric(ticker, source, metric))
    return observations


def fetch_yahoo_history(ticker: str, source: str, metric: str,
                        range_: str = "5y") -> list[Observation]:
    """Fetch the FULL daily-close history for one Yahoo ticker (for backfill).

    Same endpoint as the daily fetch, but asks for a multi-year window and keeps
    every non-null close, so the index can be rebuilt over years of history.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": "1d", "range": range_}
    try:
        resp = http.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        gmtoffset = result.get("meta", {}).get("gmtoffset", 0) or 0
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        out: list[Observation] = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            date = dt.datetime.fromtimestamp(ts + gmtoffset, tz=dt.UTC).strftime("%Y-%m-%d")
            out.append((date, source, metric, round(float(close), 4)))
        return out
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as exc:
        print(f"  [error] Yahoo history failed for {ticker}: {exc!r}")
        return []


def fetch_fx_history(base: str, quote: str, metric: str,
                     start: str = "2019-01-01") -> list[Observation]:
    """Fetch the FULL daily FX history for one pair via frankfurter's time-series."""
    today = dt.date.today().isoformat()
    try:
        resp = http.get(
            f"https://api.frankfurter.app/{start}..{today}",
            params={"from": base, "to": quote},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rates = resp.json().get("rates", {})
        out: list[Observation] = []
        for date, day in sorted(rates.items()):
            if quote in day:
                out.append((date, "frankfurter", metric, round(float(day[quote]), 6)))
        return out
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"  [error] FX history failed for {base}/{quote}: {exc!r}")
        return []


def fetch_market_history(range_: str = "5y") -> list[Observation]:
    """All Yahoo + FX history (for the one-time backfill)."""
    observations: list[Observation] = []
    for ticker, (source, metric) in YAHOO_TICKERS.items():
        observations.extend(fetch_yahoo_history(ticker, source, metric, range_))
    for base, quote, metric in FX_PAIRS:
        observations.extend(fetch_fx_history(base, quote, metric))
    return observations


def fetch_fx_rates() -> list[Observation]:
    """Fetch FX rates from frankfurter.app (ECB data, no key required).

    frankfurter returns the reference rate plus the date it applies to; we use
    that date as the observation date. Each pair is fetched independently and
    failures are isolated per pair.
    """
    observations: list[Observation] = []
    for base, quote, metric in FX_PAIRS:
        try:
            resp = http.get(
                "https://api.frankfurter.app/latest",
                params={"from": base, "to": quote},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            date = payload["date"]
            value = payload["rates"][quote]
            observations.append((date, "frankfurter", metric, round(float(value), 6)))
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            print(f"  [error] FX fetch failed for {base}/{quote}: {exc!r}")
    return observations


def run_ingest() -> None:
    """Top-level entry point: ensure schema, fetch every source, persist.

    Each source contributes whatever it could fetch; store.py dedupes against
    what's already there, so this is safe to run repeatedly within a day.
    """
    init_db()
    with open(_CONFIG_PATH) as fh:
        config = json.load(fh)

    print("Fetching crude, gas & refined products (Yahoo Finance)...")
    energy = fetch_crude_and_gas()
    print("Fetching FX rates (frankfurter.app)...")
    fx = fetch_fx_rates()
    print("Fetching official data (EIA + FRED)...")
    official = official_data.fetch_official(config)

    all_obs = energy + fx + official
    if not all_obs:
        print("No observations fetched — every source failed. Nothing written.")
        return

    conn = get_connection()
    try:
        inserted, skipped = record_many(conn, all_obs)
        print(
            f"\nFetched {len(all_obs)} observation(s): "
            f"{inserted} new, {skipped} already present (idempotent skip)."
        )

        # Derive crack spreads from the freshly stored product & crude prices.
        crude_metric = config.get("crack_spreads", {}).get(
            "crude_metric", "wti_crude_usd_bbl")
        cracks = spreads.compute_and_store_spreads(conn, crude_metric)
        if cracks:
            print("\nDerived crack spreads ($/bbl):")
            for date, metric, value in cracks:
                print(f"  {date}  {metric:<26} {value:+.2f}")

        # Forward-curve shape (contango/backwardation). Imported lazily to avoid
        # a circular import (term_structure uses ingest.fetch_yahoo_metric).
        from feedstock import term_structure
        curve = term_structure.fetch_and_store_curve(conn)
        if curve:
            print(f"\nWTI curve: front {curve['front']:.2f} vs "
                  f"{curve['months_ahead']}m {curve['deferred']:.2f} "
                  f"({curve['deferred_symbol']}) -> {curve['state'].upper()}, "
                  f"roll {curve['roll_yield_ann_pct']:+.1f}%/yr")

        print("\nLatest value per metric now in the database:")
        for row in latest_observations(conn):
            print(
                f"  {row['date']}  {row['source']:<14} "
                f"{row['metric']:<30} {row['value']}"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    run_ingest()
