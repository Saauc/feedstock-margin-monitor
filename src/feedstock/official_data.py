"""
official_data.py — institutional data sources: EIA and FRED.

This is the credibility upgrade over "Yahoo + an FX API". Two official feeds:

  EIA  (U.S. Energy Information Administration) — Mont Belvieu propane (a REAL
        petrochemical feedstock, PDH -> propylene -> acrylic/epoxy resins) and
        refinery utilization. Free API key from https://www.eia.gov/opendata/.

  FRED (Federal Reserve Economic Data, St. Louis Fed) — the actual PPI for
        Paint & Coating Manufacturing (the backtest validation target in
        Phase B), plus chemical PPI. Free key from https://fred.stlouisfed.org/.

Both are key-gated and degrade gracefully: no key → the source is skipped and
the pipeline runs on the free Yahoo/FX feeds (the index just drops the missing
components and renormalizes). Series IDs live in config.json so they can be
corrected without touching code.

EIA is hit via the v2 `/seriesid/{id}` route, which accepts a classic series ID
and returns v2-shaped JSON — robust and avoids hand-assembling facet queries.
"""

from __future__ import annotations

import os

import requests

from feedstock import http
from feedstock.narrative import _load_dotenv

Observation = tuple[str, str, str, float]
_TIMEOUT = 25


def _fetch_eia(config: dict, history: bool = False) -> list[Observation]:
    _load_dotenv()
    key = os.environ.get("EIA_API_KEY")
    series = config.get("sources", {}).get("eia", {}).get("series", {})
    if not key:
        print("  [eia] no EIA_API_KEY set — skipping official EIA data.")
        return []
    if not series:
        return []

    out: list[Observation] = []
    for metric, series_id in series.items():
        try:
            resp = http.get(
                f"https://api.eia.gov/v2/seriesid/{series_id}",
                params={"api_key": key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()["response"]["data"]
            # Ascending by period; keep all non-null (history) or just the last.
            picked = [
                (str(row["period"]), "eia", metric, float(row["value"]))
                for row in sorted(data, key=lambda r: r.get("period", ""))
                if row.get("value") is not None
            ]
            out.extend(picked if history else picked[-1:])
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            print(f"  [eia] {metric} failed: {type(exc).__name__}")
    return out


def _fetch_fred(config: dict, history: bool = False) -> list[Observation]:
    _load_dotenv()
    key = os.environ.get("FRED_API_KEY")
    series = config.get("sources", {}).get("fred", {}).get("series", {})
    if not key:
        print("  [fred] no FRED_API_KEY set — skipping official FRED data.")
        return []
    if not series:
        return []

    out: list[Observation] = []
    for metric, series_id in series.items():
        params = {"series_id": series_id, "api_key": key, "file_type": "json",
                  "sort_order": "asc"}
        if not history:
            params.update(sort_order="desc", limit="1")  # latest print only
        try:
            resp = http.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params=params, timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            for row in resp.json().get("observations", []):
                if row.get("value") not in (".", None, ""):
                    out.append((row["date"], "fred", metric, float(row["value"])))
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            print(f"  [fred] {metric} failed: {type(exc).__name__}")
    return out


def fetch_official(config: dict) -> list[Observation]:
    """Fetch the LATEST EIA + FRED prints (lightweight, for the daily run)."""
    return _fetch_eia(config) + _fetch_fred(config)


def fetch_official_history(config: dict) -> list[Observation]:
    """Fetch the FULL EIA + FRED series history (for the one-time backfill)."""
    return _fetch_eia(config, history=True) + _fetch_fred(config, history=True)
