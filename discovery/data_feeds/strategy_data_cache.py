"""
Shared Postgres-backed cache for the strategy alt-data feeds
(edgar_historical.py, fmp_earnings_calendar.py, finra_historical.py).

Why this exists
----------------
These three feeds previously cached their per-symbol history to parquet files
under discovery/data/**. Railway's filesystem is ephemeral — every redeploy
wipes those files, so PEAD (earnings), insider-flow, and short-interest-ratio
history all restarted from empty on every deploy, defeating the point of
caching multi-year backtest data at all. This module replaces that with one
shared Postgres table so history survives redeploys.

Table: ``strategy_data_cache`` (feed_name, symbol, data_date, data_json,
updated_at), one row per (feed_name, symbol, data_date). ``data_json`` holds
whatever fields that feed's per-date record needs (e.g. ``{"net_value": ...}``
for EDGAR, ``{"eps_actual": ..., "surprise_pct": ...}`` for FMP,
``{"short_ratio": ...}`` for FINRA) — all three feeds share one table and one
set of read/write/freshness helpers without forcing a common row shape beyond
the (feed, symbol, date) key.

Freshness / background refresh
--------------------------------
``read_frame`` never blocks on the network — it only reads whatever is
already in Postgres. ``maybe_trigger_refresh`` checks whether the newest
``updated_at`` for a (feed_name, symbol) is older than that feed's natural
refresh interval and, if so, launches ``refresh_fn`` in a background daemon
thread (deduplicated per (feed_name, symbol) so concurrent callers don't spawn
redundant fetches) rather than silently serving indefinitely-stale data.
Staleness is measured on ``updated_at`` (when we last checked the source), not
``data_date`` (the date the underlying data point represents) — a symbol with
no new filings in the refresh window is not "stale" just because nothing
changed; we only want to know whether we've *checked* recently.

Each feed module still does a synchronous fetch-and-store on a genuinely cold
cache (nothing stored yet for that symbol) so the first call for a symbol
returns real data immediately rather than empty — the background-refresh path
only applies once something is already cached but has aged past its TTL.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine, text as sql_text

from config import Config

TABLE = "strategy_data_cache"

_refresh_in_flight: set[tuple[str, str]] = set()
_refresh_lock = threading.Lock()


def _get_engine(db_engine=None):
    """Return (engine, owns_engine). Reuses a passed-in engine, or builds a
    short-lived one from Config.DATABASE_URL that the caller must dispose."""
    if db_engine is not None:
        return db_engine, False
    if not Config.DATABASE_URL:
        return None, False
    return create_engine(Config.DATABASE_URL, pool_pre_ping=True), True


def ensure_table(db_engine=None) -> None:
    engine, owns = _get_engine(db_engine)
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(sql_text(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    feed_name  VARCHAR(50) NOT NULL,
                    symbol     VARCHAR(20) NOT NULL,
                    data_date  DATE        NOT NULL,
                    data_json  JSONB       NOT NULL,
                    updated_at TIMESTAMP   DEFAULT NOW(),
                    PRIMARY KEY (feed_name, symbol, data_date)
                )
            """))
            conn.execute(sql_text(
                f"CREATE INDEX IF NOT EXISTS {TABLE}_feed_symbol_idx "
                f"ON {TABLE} (feed_name, symbol, data_date)"
            ))
    except Exception:
        print(f"[StrategyCache] ensure_table failed:\n{traceback.format_exc()}")
    finally:
        if owns:
            engine.dispose()


def read_frame(feed_name: str, symbol: str, db_engine=None) -> pd.DataFrame:
    """
    All cached rows for (feed_name, symbol), sorted ascending by data_date.
    Returns a DataFrame with a ``date`` column plus every key present in
    ``data_json`` expanded as its own column. Empty (just a ``date`` column,
    no rows) if nothing is cached or on any error — never raises, never hits
    the network.
    """
    empty = pd.DataFrame(columns=["date"])
    engine, owns = _get_engine(db_engine)
    if engine is None:
        return empty
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text(f"""
                SELECT data_date, data_json FROM {TABLE}
                WHERE feed_name = :feed AND symbol = :symbol
                ORDER BY data_date ASC
            """), {"feed": feed_name, "symbol": symbol}).fetchall()
        if not rows:
            return empty
        records = []
        for data_date, data_json in rows:
            rec = dict(data_json) if isinstance(data_json, dict) else {}
            rec["date"] = pd.Timestamp(data_date)
            records.append(rec)
        return pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    except Exception:
        print(f"[StrategyCache] read failed for {feed_name}/{symbol}:\n{traceback.format_exc()}")
        return empty
    finally:
        if owns:
            engine.dispose()


def write_rows(feed_name: str, symbol: str, rows: list[tuple], db_engine=None) -> int:
    """
    Upsert rows: a list of ``(date_like, dict_of_json_fields)``. Returns the
    count written (0 on any failure — fail-open, never raises).
    """
    if not rows:
        return 0
    engine, owns = _get_engine(db_engine)
    if engine is None:
        return 0
    try:
        ensure_table(engine)
        with engine.begin() as conn:
            for d, fields in rows:
                d_str = pd.to_datetime(d).date().isoformat()
                # NOTE: `:data_json::jsonb` (Postgres's shorthand cast) is NOT
                # safe inside SQLAlchemy text() — the `::` collides with the
                # `:name` bind-param parser and leaves a literal `:data_json`
                # in the compiled SQL (verified against a live Postgres
                # instance during development). CAST(... AS jsonb) avoids the
                # ambiguity entirely.
                conn.execute(sql_text(f"""
                    INSERT INTO {TABLE} (feed_name, symbol, data_date, data_json, updated_at)
                    VALUES (:feed, :symbol, :data_date, CAST(:data_json AS jsonb), NOW())
                    ON CONFLICT (feed_name, symbol, data_date) DO UPDATE SET
                        data_json  = EXCLUDED.data_json,
                        updated_at = NOW()
                """), {
                    "feed": feed_name, "symbol": symbol, "data_date": d_str,
                    "data_json": json.dumps(fields, default=str),
                })
        return len(rows)
    except Exception:
        print(f"[StrategyCache] write failed for {feed_name}/{symbol}:\n{traceback.format_exc()}")
        return 0
    finally:
        if owns:
            engine.dispose()


def get_last_synced_at(feed_name: str, symbol: str, db_engine=None) -> datetime | None:
    """Most recent updated_at across this (feed, symbol)'s cached rows —
    when we last successfully wrote data, used for staleness checks. None
    if nothing is cached yet."""
    engine, owns = _get_engine(db_engine)
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            result = conn.execute(sql_text(f"""
                SELECT MAX(updated_at) FROM {TABLE}
                WHERE feed_name = :feed AND symbol = :symbol
            """), {"feed": feed_name, "symbol": symbol}).scalar()
        return result
    except Exception:
        print(f"[StrategyCache] last-synced query failed for {feed_name}/{symbol}:\n{traceback.format_exc()}")
        return None
    finally:
        if owns:
            engine.dispose()


def maybe_trigger_refresh(
    feed_name: str,
    symbol: str,
    max_age_days: float,
    refresh_fn,
    *refresh_args,
    db_engine=None,
    **refresh_kwargs,
) -> bool:
    """
    If (feed_name, symbol)'s cache is older than ``max_age_days`` (or has
    never been synced), schedule ``refresh_fn(*refresh_args, **refresh_kwargs)``
    to run in a background daemon thread and return True. Deduplicated: a
    second call while a refresh for the same (feed_name, symbol) is already
    running is a no-op. Never blocks — callers should serve whatever is
    currently cached (even if stale/empty) rather than wait on this.
    """
    last_synced = get_last_synced_at(feed_name, symbol, db_engine=db_engine)
    if last_synced is not None:
        age = datetime.utcnow() - last_synced.replace(tzinfo=None)
        if age < timedelta(days=max_age_days):
            return False

    key = (feed_name, symbol)
    with _refresh_lock:
        if key in _refresh_in_flight:
            return False
        _refresh_in_flight.add(key)

    def _run():
        try:
            print(f"[StrategyCache] {feed_name}/{symbol}: cache stale — background refresh starting")
            refresh_fn(*refresh_args, **refresh_kwargs)
            print(f"[StrategyCache] {feed_name}/{symbol}: background refresh complete")
        except Exception:
            print(f"[StrategyCache] {feed_name}/{symbol}: background refresh failed:\n{traceback.format_exc()}")
        finally:
            with _refresh_lock:
                _refresh_in_flight.discard(key)

    threading.Thread(target=_run, daemon=True, name=f"refresh-{feed_name}-{symbol}").start()
    return True


if __name__ == "__main__":
    # Offline shape test only (no DB) — full read/write is exercised by
    # running any of the three feed modules against a real DATABASE_URL.
    assert TABLE == "strategy_data_cache"
    print("strategy_data_cache module loaded OK (no DB configured for a live smoke test here)")
