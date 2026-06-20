"""
store.py — SQLite persistence layer for the feedstock margin monitor.

One job: take observations (a measured value for a metric on a date from a
source) and persist them durably and *idempotently*. Re-running the pipeline
on the same day must never create duplicate rows — that property is what lets
a scheduled job run unattended and lets us re-run after a failure without
corrupting history.

Schema is deliberately narrow and generic (a "long" / tidy table):

    observations(date, source, metric, value, ingested_at)

Every data point — a crude price, an FX rate, later a computed index — is one
row. Adding a new metric never requires a schema migration; you just write
rows with a new `metric` name. The UNIQUE(date, source, metric) constraint is
the backbone of idempotency.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

# Single source of truth for where the DB lives. Everything (ingest, analysis,
# briefing, the Flask app) imports this so they all read/write the same file.
from feedstock.paths import DB_PATH


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults.

    - row_factory=sqlite3.Row lets callers use column names (row["value"])
      instead of positional indexes, which keeps call sites readable.
    - PRAGMA foreign_keys is harmless here but good hygiene as the schema grows.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create the table and indexes if they don't already exist.

    Safe to call on every run — `IF NOT EXISTS` makes it a no-op once the
    schema is in place. We call it at the start of each ingest so a fresh
    checkout (or a fresh CI runner) bootstraps itself with no manual step.
    """
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,   -- market/observation date, ISO 'YYYY-MM-DD'
                source      TEXT    NOT NULL,   -- e.g. 'yahoo_finance', 'frankfurter'
                metric      TEXT    NOT NULL,   -- e.g. 'brent_crude_usd_bbl'
                value       REAL    NOT NULL,   -- the number itself
                ingested_at TEXT    NOT NULL,   -- UTC timestamp of when we wrote the row
                UNIQUE (date, source, metric)   -- the idempotency guarantee
            );
            """
        )
        # Index the dimensions we filter/sort by most: pulling a metric's
        # history ordered by date is the hot path for analysis and charts.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_metric_date "
            "ON observations (metric, date);"
        )
        # Daily AI/templated narrative — one row per date. This is free-form
        # prose, not a numeric observation, so it gets its own table rather
        # than being shoehorned into `observations`. `source` records whether
        # the text came from the LLM ('ai') or the fallback ('template').
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS narratives (
                date       TEXT PRIMARY KEY,   -- one narrative per day
                text       TEXT NOT NULL,
                source     TEXT NOT NULL,      -- 'ai' | 'template'
                created_at TEXT NOT NULL
            );
            """
        )
        # Records which days we've already emailed an alert for, so re-running
        # the pipeline on the same day doesn't fire duplicate emails. The
        # written flag in the briefing is always shown (idempotent display);
        # only the email side effect is deduped here.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_emails (
                date      TEXT PRIMARY KEY,
                idx       REAL NOT NULL,   -- index value at the time we emailed
                threshold REAL NOT NULL,
                sent_at   TEXT NOT NULL
            );
            """
        )
        # Latest backtest report (index vs FRED coatings PPI). Stored as a JSON
        # blob keyed by run date so the briefing/dashboard can display the most
        # recent validation without recomputing. One row per run date.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_results (
                date        TEXT PRIMARY KEY,
                report_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
        conn.commit()


def record_observation(
    conn: sqlite3.Connection,
    date: str,
    source: str,
    metric: str,
    value: float,
) -> bool:
    """Insert one observation. Returns True if a new row was written.

    Idempotency mechanism: `INSERT ... ON CONFLICT(date, source, metric) DO
    NOTHING`. If a row for this (date, source, metric) already exists, the
    insert is silently skipped rather than raising or duplicating. We detect
    whether a row was actually written via `cursor.rowcount` (1 = inserted,
    0 = skipped) so callers can report "new vs. already had it".

    Note we do NOT update the value on conflict. A given metric on a given
    market date is a fact that shouldn't change between runs; keeping the first
    write avoids silently rewriting history. (If a source revises a number, we
    can add an explicit upsert path later.)
    """
    ingested_at = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO observations (date, source, metric, value, ingested_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date, source, metric) DO NOTHING;
        """,
        (date, source, metric, float(value), ingested_at),
    )
    return cur.rowcount == 1


def record_many(
    conn: sqlite3.Connection,
    observations: Iterable[Sequence],
) -> tuple[int, int]:
    """Persist a batch of observations in one transaction.

    Each item is (date, source, metric, value). Returns (inserted, skipped).
    Wrapping the batch in a single transaction keeps writes atomic and fast.
    """
    inserted = skipped = 0
    for date, source, metric, value in observations:
        if record_observation(conn, date, source, metric, value):
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


def fetch_metric_history(
    conn: sqlite3.Connection,
    metric: str,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Return rows for one metric, oldest → newest. Used by analysis & charts."""
    sql = "SELECT date, value FROM observations WHERE metric = ? ORDER BY date ASC"
    params: list = [metric]
    if limit is not None:
        # Grab the most recent `limit` rows, then re-sort ascending so callers
        # always receive chronological order.
        sql = (
            "SELECT date, value FROM ("
            "SELECT date, value FROM observations WHERE metric = ? "
            "ORDER BY date DESC LIMIT ?) ORDER BY date ASC"
        )
        params = [metric, limit]
    return conn.execute(sql, params).fetchall()


def latest_observations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Most recent value for every metric — handy for a quick status print."""
    return conn.execute(
        """
        SELECT o.metric, o.source, o.date, o.value
        FROM observations o
        JOIN (
            SELECT metric, MAX(date) AS max_date
            FROM observations GROUP BY metric
        ) m ON o.metric = m.metric AND o.date = m.max_date
        ORDER BY o.source, o.metric;
        """
    ).fetchall()


def count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def delete_metric(conn: sqlite3.Connection, metric: str) -> int:
    """Delete all rows for a metric. Used to cleanly regenerate derived/computed
    series (index, crack spreads) on backfill, since record_observation is
    first-write-wins and won't overwrite stale recomputations."""
    cur = conn.execute("DELETE FROM observations WHERE metric = ?", (metric,))
    conn.commit()
    return cur.rowcount


# --- narratives -----------------------------------------------------------

def save_narrative(
    conn: sqlite3.Connection,
    date: str,
    text: str,
    source: str,
) -> None:
    """Insert or replace the narrative for a date.

    Unlike observations (where the first write wins, treating a price as a
    settled fact), a narrative *should* refresh when re-run later in the day —
    new data warrants a new interpretation. So we REPLACE on the date key.
    """
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO narratives (date, text, source, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            text = excluded.text,
            source = excluded.source,
            created_at = excluded.created_at;
        """,
        (date, text, source, created_at),
    )
    conn.commit()


def latest_narrative(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT date, text, source, created_at FROM narratives "
        "ORDER BY date DESC LIMIT 1"
    ).fetchone()


# --- alert email dedupe ---------------------------------------------------

def alert_email_already_sent(conn: sqlite3.Connection, date: str) -> bool:
    """True if we've already emailed an alert for this date."""
    return conn.execute(
        "SELECT 1 FROM alert_emails WHERE date = ?", (date,)
    ).fetchone() is not None


def record_alert_email(
    conn: sqlite3.Connection, date: str, index: float, threshold: float
) -> None:
    sent_at = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO alert_emails (date, idx, threshold, sent_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO NOTHING;
        """,
        (date, float(index), float(threshold), sent_at),
    )
    conn.commit()


# --- backtest results -----------------------------------------------------

def save_backtest_result(conn: sqlite3.Connection, date: str, report_json: str) -> None:
    """Store/refresh the backtest report for a run date (JSON blob)."""
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO backtest_results (date, report_json, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            report_json = excluded.report_json,
            created_at  = excluded.created_at;
        """,
        (date, report_json, created_at),
    )
    conn.commit()


def latest_backtest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT date, report_json, created_at FROM backtest_results "
        "ORDER BY date DESC LIMIT 1"
    ).fetchone()
