"""
backfill.py — one-time historical load of the official (EIA + FRED) series.

The daily ingest only pulls the LATEST print of each official series (cheap,
idempotent). But the backtest needs *history* — years of the coatings PPI to
correlate against the index. Run this once after adding EIA_API_KEY / FRED_API_KEY
to pull the full series history into the database:

    python -m feedstock.backfill        # or: feedstock-backfill

It's idempotent (same UNIQUE(date, source, metric) guard), so re-running only
inserts genuinely new points. Without keys it no-ops gracefully.
"""

from __future__ import annotations

from feedstock import analysis, ingest, official_data, spreads, transform
from feedstock.store import (
    delete_metric,
    get_connection,
    init_db,
    record_many,
    record_observation,
)

_REBUILT_METRICS = [
    "margin_pressure_index",
    "gasoline_crack_usd_bbl",
    "distillate_crack_usd_bbl",
    "crack_321_usd_bbl",
]


def _rebuild_index_and_cracks(conn, config: dict) -> int:
    """Recompute the margin-pressure index AND crack spreads over ALL history.

    Clears the prior computed/derived series first (record_observation is
    first-write-wins, so stale rows would otherwise survive a re-run), then
    walks the full date axis and persists a fresh series.
    """
    for metric in _REBUILT_METRICS:
        delete_metric(conn, metric)

    components = config["components"]
    series_map = analysis.build_series_map(conn, list(components.keys()))
    written = 0
    for rec in transform.compute_pressure_series(series_map, config):
        record_observation(conn, rec["date"], "computed",
                           "margin_pressure_index", rec["index"])
        written += 1

    crude_metric = config.get("crack_spreads", {}).get("crude_metric", "wti_crude_usd_bbl")
    cm = analysis.build_series_map(
        conn, ["rbob_gasoline_usd_gal", "ulsd_diesel_usd_gal", crude_metric])
    dates, aligned = transform.align_by_date(cm)
    for i, date in enumerate(dates):
        rb = aligned["rbob_gasoline_usd_gal"][i]
        ho = aligned["ulsd_diesel_usd_gal"][i]
        crude = aligned[crude_metric][i]
        if rb is None or ho is None or crude is None:
            continue
        for metric, value in spreads.compute_spreads(rb, ho, crude).items():
            record_observation(conn, date, "derived", metric, value)
    conn.commit()
    return written


def run_backfill(range_: str = "5y") -> int:
    """Load full official + market history, then rebuild the index over it."""
    config = analysis.load_config()
    init_db()
    conn = get_connection()
    try:
        print("Backfilling official series (EIA + FRED)...")
        official = official_data.fetch_official_history(config)
        print(f"Backfilling market history (Yahoo + FX, {range_})...")
        market = ingest.fetch_market_history(range_)
        obs = official + market
        if not obs:
            print("Nothing fetched — check keys / network.")
            return 0
        inserted, skipped = record_many(conn, obs)
        print(f"Raw history: {len(obs)} points — {inserted} new, {skipped} present.")

        print("Rebuilding index + crack-spread history...")
        n = _rebuild_index_and_cracks(conn, config)
        print(f"Rebuilt {n} days of margin-pressure index.")
        return inserted
    finally:
        conn.close()


def main() -> None:
    run_backfill()


if __name__ == "__main__":
    main()
