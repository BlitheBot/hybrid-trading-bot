"""
Ticker Prioritizer — 30-minute refresh of top 250 S&P 500 stocks by today's volume.

Pulls volume via Alpaca snapshot API in batches of 100, ranks by volume descending,
and UPSERTs top 250 into the `active_tickers` PostgreSQL table.
The news sentiment scorer reads from this table to avoid scanning all 500 tickers.
"""

import time
from datetime import datetime, timedelta

from sqlalchemy import text as sql_text

from data.sp500_tickers import SP500_TICKERS

BATCH_SIZE = 100
TOP_N = 250
INTERVAL_SECONDS = 30 * 60


def _ensure_table(db_engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS active_tickers (
                ticker       VARCHAR(10) PRIMARY KEY,
                volume_1d    BIGINT,
                rank         INT,
                last_updated TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        conn.execute(sql_text("""
            CREATE INDEX IF NOT EXISTS active_tickers_rank_idx ON active_tickers (rank)
        """))


def refresh_active_tickers(db_engine, stock_data_client) -> None:
    """Fetch today's volume for all S&P 500 tickers via Alpaca snapshot API and UPSERT top 250.

    Failed batches assign volume 0 to affected tickers so they are not dropped entirely;
    they will rank at the bottom and only enter the top 250 if < 250 tickers have real data.
    """
    from alpaca.data.requests import StockSnapshotRequest

    tickers = [t for t in SP500_TICKERS if t and "." not in t and "/" not in t]
    vol_map: dict[str, int] = {}

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        try:
            snapshots = stock_data_client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=batch)
            )
            for sym, snap in snapshots.items():
                try:
                    vol = snap.daily_bar.volume if snap.daily_bar else 0
                    vol_map[sym] = int(vol or 0)
                except Exception:
                    vol_map[sym] = 0
            # Tickers absent from the response get volume 0
            for sym in batch:
                if sym not in vol_map:
                    vol_map[sym] = 0
        except Exception as e:
            print(f"[TickerPrioritizer] Batch {batch_num} error: {e} — assigning volume 0 to batch")
            for sym in batch:
                if sym not in vol_map:
                    vol_map[sym] = 0
        time.sleep(0.1)

    if not vol_map:
        print("[TickerPrioritizer] No volume data returned — skipping DB update")
        return

    sorted_syms = sorted(vol_map, key=lambda s: vol_map[s], reverse=True)[:TOP_N]
    ranked = [(sym, vol_map[sym], rank + 1) for rank, sym in enumerate(sorted_syms)]

    _ensure_table(db_engine)
    with db_engine.begin() as conn:
        for sym, vol, rank in ranked:
            conn.execute(sql_text("""
                INSERT INTO active_tickers (ticker, volume_1d, rank, last_updated)
                VALUES (:ticker, :vol, :rank, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    volume_1d    = EXCLUDED.volume_1d,
                    rank         = EXCLUDED.rank,
                    last_updated = EXCLUDED.last_updated
            """), {"ticker": sym, "vol": vol, "rank": rank})

    top = ranked[0]
    bot = ranked[-1]
    print(
        f"[TickerPrioritizer] Updated {len(ranked)} active tickers — "
        f"#1: {top[0]} ({top[1]:,}), #{TOP_N}: {bot[0]} ({bot[1]:,})"
    )


def get_active_tickers(db_engine) -> list[str]:
    """Return tickers from active_tickers updated within the last 35 minutes, ordered by rank."""
    if db_engine is None:
        return []
    cutoff = datetime.utcnow() - timedelta(minutes=35)
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT ticker FROM active_tickers
                WHERE last_updated > :cutoff
                ORDER BY rank ASC
            """), {"cutoff": cutoff}).mappings().fetchall()
        return [r["ticker"] for r in rows]
    except Exception as e:
        print(f"[TickerPrioritizer] get_active_tickers error: {e}")
        return []
