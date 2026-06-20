"""
spreads.py — petrochemical crack spreads and the coatings value-chain model.

This is the domain core. Two ideas a commodity desk cares about that the naive
"z-score the crude price" version missed:

1. MARGINS ARE SPREADS, NOT LEVELS. A refiner/petrochemical producer's margin
   is the *crack spread* — the value of refined products minus the crude that
   made them — not the crude price itself. High crude with even-higher products
   is a fat margin; high crude with weak products is a squeeze. We compute the
   real crack spreads (gasoline, distillate, 3:2:1) from product-vs-crude
   futures, correctly converting $/gal products to a $/bbl basis (42 gal/bbl).

2. THE COATINGS COST STACK ISN'T CRUDE. Paint margin pressure comes from a
   specific bill of materials — pigment, resins, solvents, energy, logistics —
   each derived from a different node of the petrochemical chain. COATINGS_BOM
   below documents that stack and maps each input to the best free/official
   proxy, and is explicit about the one input we genuinely can't price for free
   (TiO2), rather than pretending crude stands in for everything.
"""

from __future__ import annotations

from feedstock.store import fetch_metric_history, record_observation

# A US barrel is 42 US gallons. Product futures (RBOB, ULSD) quote in $/gal;
# crude (WTI, Brent) quotes in $/bbl. To compare like-for-like in a crack
# spread, lift the product to a $/bbl basis with this factor.
GAL_PER_BBL = 42.0


# ---------------------------------------------------------------------------
# Coatings bill-of-materials → proxy map (the honest value chain).
# Shares are indicative of architectural/industrial coatings raw-material cost
# and align with what PPG / Sherwin-Williams / AkzoNobel / Axalta cite on
# earnings calls. `proxy` is what we can actually observe for free/officially.
# ---------------------------------------------------------------------------
COATINGS_BOM = {
    "tio2_pigment": {
        "cost_share": "~20-25%",
        "chain": "Ilmenite/rutile ore -> chloride/sulfate process -> TiO2",
        "proxy": None,
        "note": "Largest single input. No free price feed. Energy- and "
                "chlorine-intensive, so partially tracked via natural gas; "
                "validated indirectly via FRED chemical PPI. This is the "
                "model's biggest acknowledged gap.",
    },
    "resins_binders": {
        "cost_share": "~25-35%",
        "chain": "Propane/naphtha -> propylene/ethylene -> acrylic/epoxy resin",
        "proxy": "propane_mont_belvieu_usd_gal (EIA) / rbob_gasoline (proxy)",
        "note": "Core petrochemical-linked cost; propylene via PDH from propane.",
    },
    "solvents": {
        "cost_share": "~5-15%",
        "chain": "Crude -> light distillates -> hydrocarbon solvents",
        "proxy": "rbob_gasoline_usd_gal (naphtha-range light ends)",
        "note": "Higher share in solvent-borne vs water-borne coatings.",
    },
    "energy_process": {
        "cost_share": "~5-10%",
        "chain": "Natural gas / power for reactors, dispersion, drying",
        "proxy": "henry_hub_natgas_usd_mmbtu",
        "note": "Also the feed+fuel for upstream petrochemical crackers.",
    },
    "logistics_packaging": {
        "cost_share": "~5-10%",
        "chain": "Diesel-driven inbound/outbound freight; steel/resin packaging",
        "proxy": "ulsd_diesel_usd_gal",
        "note": "Distillate crack is a clean read on freight cost pressure.",
    },
}


# --- crack spread math (pure) ---------------------------------------------

def crack_spread(product_usd_per_gal: float, crude_usd_per_bbl: float) -> float:
    """Single-product crack spread in $/bbl: product (lifted to $/bbl) - crude."""
    return product_usd_per_gal * GAL_PER_BBL - crude_usd_per_bbl


def crack_321(rbob_usd_gal: float, ulsd_usd_gal: float, crude_usd_bbl: float) -> float:
    """The 3:2:1 crack — 3 bbl crude -> 2 bbl gasoline + 1 bbl distillate.

    The standard benchmark refining margin. Equals the weighted average of the
    gasoline and distillate cracks in 2:1 proportion.
    """
    gas = crack_spread(rbob_usd_gal, crude_usd_bbl)
    dist = crack_spread(ulsd_usd_gal, crude_usd_bbl)
    return (2.0 * gas + dist) / 3.0


def compute_spreads(rbob_usd_gal: float, ulsd_usd_gal: float, crude_usd_bbl: float) -> dict:
    """All crack spreads for a given (RBOB, ULSD, crude) snapshot, in $/bbl."""
    return {
        "gasoline_crack_usd_bbl": round(crack_spread(rbob_usd_gal, crude_usd_bbl), 3),
        "distillate_crack_usd_bbl": round(crack_spread(ulsd_usd_gal, crude_usd_bbl), 3),
        "crack_321_usd_bbl": round(crack_321(rbob_usd_gal, ulsd_usd_gal, crude_usd_bbl), 3),
    }


# --- persistence wrapper --------------------------------------------------

def _latest(conn, metric: str):
    """Most recent (date, value) for a metric, or None."""
    rows = fetch_metric_history(conn, metric, limit=1)
    return (rows[-1]["date"], rows[-1]["value"]) if rows else None


def compute_and_store_spreads(conn, crude_metric: str = "wti_crude_usd_bbl") -> list:
    """Read the latest product & crude observations, compute crack spreads,
    and persist them as `source='derived'` metrics. Returns what was stored.

    Skips quietly if the necessary inputs aren't in the DB yet (e.g. before the
    first ingest that includes product futures).
    """
    rb = _latest(conn, "rbob_gasoline_usd_gal")
    ho = _latest(conn, "ulsd_diesel_usd_gal")
    crude = _latest(conn, crude_metric)
    if not (rb and ho and crude):
        return []

    date = max(rb[0], ho[0], crude[0])  # most recent common trading date
    spreads = compute_spreads(rb[1], ho[1], crude[1])
    stored = []
    for metric, value in spreads.items():
        record_observation(conn, date, "derived", metric, value)
        stored.append((date, metric, value))
    conn.commit()
    return stored
