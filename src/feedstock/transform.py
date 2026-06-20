"""
transform.py — pure numerical logic for the margin-pressure index.

Everything in this module is a pure function: no network, no database, no
clock, no config-file reads. Inputs come in as plain Python data, results go
out as plain Python data. That purity is deliberate — it's what lets the unit
tests in test_transform.py exercise the index logic exhaustively with zero
network and full determinism.

The pipeline these functions implement:

    raw per-metric series
        -> align by date (forward-fill)
        -> per metric: z-score latest vs trailing window
        -> apply per-metric direction (+1 cost-like, -1 inverse)
        -> weighted average  -> composite z
        -> logistic squash    -> 0..100 margin-pressure index
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import mean, stdev

Number = float
Series = list[tuple[str, float]]          # [(date, value), ...]
SeriesMap = dict[str, Series]             # {metric: Series}

# Minimum baseline points before a z-score is meaningful. With fewer than this
# we return 0.0 (i.e. "looks normal") rather than a noisy or undefined number.
MIN_BASELINE_POINTS = 2


def zscore(value: float, baseline: Sequence[float]) -> float:
    """Standard score of `value` against a `baseline` sample.

    Returns 0.0 — meaning "indistinguishable from normal" — in the two cases
    where a z-score isn't well defined:
      * fewer than MIN_BASELINE_POINTS baseline observations, and
      * zero variance (a flat baseline; std == 0 would divide by zero).
    Using sample stdev (n-1) treats the trailing window as a sample of the
    metric's recent behaviour, not the whole population.
    """
    base = list(baseline)
    if len(base) < MIN_BASELINE_POINTS:
        return 0.0
    sd = stdev(base)
    if sd == 0:
        return 0.0
    return (value - mean(base)) / sd


def logistic(x: float) -> float:
    """Standard logistic 1/(1+e^-x), mapping any real to (0, 1).

    Guarded against overflow at the extremes so a huge composite z can't raise
    OverflowError — it just saturates at 0.0 / 1.0.
    """
    if x <= -60:
        return 0.0
    if x >= 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def align_by_date(series_map: SeriesMap) -> tuple[list[str], dict[str, list[float | None]]]:
    """Align several per-metric series onto one shared, sorted date axis.

    Returns (dates, aligned) where `dates` is the sorted union of every date
    seen across all metrics, and aligned[metric] is a list parallel to `dates`
    holding that metric's value carried forward (forward-fill). Positions
    before a metric's first observation are None.

    Forward-fill matters because sources disagree on which calendar days they
    report (a US market holiday vs. an ECB TARGET day, per the Phase 1 note):
    on a day crude printed but FX didn't, we reuse FX's last known value rather
    than dropping the row.
    """
    all_dates = sorted({d for series in series_map.values() for d, _ in series})
    aligned: dict[str, list[float | None]] = {}
    for metric, series in series_map.items():
        lookup = dict(series)
        carried: float | None = None
        column: list[float | None] = []
        for d in all_dates:
            if d in lookup:
                carried = lookup[d]
            column.append(carried)
        aligned[metric] = column
    return all_dates, aligned


def _combine(directional_z: dict[str, float], weights: dict[str, float], k: float) -> tuple[float, float]:
    """Weighted-average the directional z-scores and squash to 0..100.

    Weights are normalized by their total, so absolute weight magnitudes don't
    matter — only their ratios. With no usable components we return the neutral
    midpoint (50.0, composite 0.0).
    """
    total_w = sum(abs(w) for w in weights.values())
    if not directional_z or total_w == 0:
        return 50.0, 0.0
    composite = sum(directional_z[m] * weights[m] for m in directional_z) / total_w
    index = 100.0 * logistic(k * composite)
    return clamp(index), composite


def compute_pressure_series(series_map: SeriesMap, config: dict) -> list[dict]:
    """Compute the margin-pressure index at every date on the shared axis.

    For each date i and each configured component, the baseline is the up-to
    `trailing_window` prior (non-None) values strictly before i — today is
    never in its own baseline. Each record returned is:

        {date, index, composite_z, components: {metric: directional_z},
         sufficient: bool}

    `sufficient` is False when no component had enough trailing history for a
    real z-score (e.g. the first day of data) — the index is still emitted as a
    neutral 50 but flagged so callers can say "not enough history yet".
    """
    window = int(config.get("trailing_window", 20))
    k = float(config.get("logistic_k", 1.0))
    # Coverage gate: don't emit an index when too little of the basket is
    # present. Early history where only one source exists (e.g. propane before
    # the other feeds start) is a 1-of-7 "index" — noise, not signal. Default 0
    # = emit always (backwards compatible); config.json sets a real threshold.
    min_coverage = float(config.get("min_component_coverage", 0.0))
    # EWMA smoothing of the composite signal. span=1 (default) = no smoothing;
    # config.json sets a span to damp daily whipsaw into a readable cycle.
    span = max(1, int(config.get("index_smoothing_span", 1)))
    components: dict = config["components"]
    total_weight = sum(abs(float(c.get("weight", 0.0)))
                       for c in components.values()) or 1.0

    dates, aligned = align_by_date(series_map)

    # Pass 1: raw composite z per date (gated on coverage).
    raw: list[dict] = []
    for i, date in enumerate(dates):
        directional_z: dict[str, float] = {}
        weights: dict[str, float] = {}
        max_baseline_len = 0
        present_weight = 0.0

        for metric, cfg in components.items():
            column = aligned.get(metric)
            if column is None:
                continue
            current = column[i]
            if current is None:
                continue  # metric hasn't started reporting yet
            baseline = [v for v in column[max(0, i - window):i] if v is not None]
            max_baseline_len = max(max_baseline_len, len(baseline))
            z = zscore(current, baseline)
            direction = float(cfg.get("direction", 1))
            directional_z[metric] = z * direction
            w = float(cfg.get("weight", 0.0))
            weights[metric] = w
            present_weight += abs(w)

        if present_weight / total_weight < min_coverage:
            continue
        _, composite = _combine(directional_z, weights, k)
        raw.append({
            "date": date,
            "composite_z": composite,
            "components": directional_z,
            "sufficient": max_baseline_len >= MIN_BASELINE_POINTS,
        })

    # Pass 2: EWMA-smooth the composite signal, then squash to 0..100.
    alpha = 2.0 / (span + 1.0)
    smoothed: float | None = None
    results: list[dict] = []
    for r in raw:
        smoothed = (r["composite_z"] if smoothed is None
                    else alpha * r["composite_z"] + (1 - alpha) * smoothed)
        index = clamp(100.0 * logistic(k * smoothed))
        results.append({
            "date": r["date"],
            "index": round(index, 2),
            "composite_z": round(smoothed, 4),
            "components": {m: round(v, 4) for m, v in r["components"].items()},
            "sufficient": r["sufficient"],
        })

    return results


def compute_latest_pressure(series_map: SeriesMap, config: dict) -> dict:
    """Convenience: the most recent record from compute_pressure_series.

    Returns a neutral, flagged record if there's no data at all.
    """
    series = compute_pressure_series(series_map, config)
    if not series:
        return {
            "date": None,
            "index": 50.0,
            "composite_z": 0.0,
            "components": {},
            "sufficient": False,
        }
    return series[-1]
