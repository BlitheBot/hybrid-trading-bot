"""
Symbol Universe — weekly refresh of top S&P 500 symbols by average daily volume.

Runs every Sunday at midnight EST via symbol_universe_loop in bot.py.
Stores results in PostgreSQL `symbol_universe` table (symbol, avg_volume, rank, updated_at).
`get_discovery_candidates()` returns the top N symbols for the Discovery Engine.
"""

import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz
from sqlalchemy import text as sql_text

from config import Config
from data.sp500_tickers import SP500_TICKERS

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def _ensure_symbol_universe_table(db_engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS symbol_universe (
                symbol     VARCHAR(10) PRIMARY KEY,
                avg_volume BIGINT,
                rank       INTEGER,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))


def refresh_symbol_universe(
    db_engine,
    stock_data_client,
    top_n: int = 100,
) -> None:
    """Fetch 5-day avg volume for all SP500_TICKERS and UPSERT top top_n into DB."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    est = pytz.timezone("America/New_York")
    end = datetime.now(est).replace(hour=0, minute=0, second=0, microsecond=0)
    # 10 calendar days → guarantees at least 5 trading days of bars
    start = end - timedelta(days=10)

    # Strip symbols that Alpaca IEX feed won't accept (dots, slashes)
    tickers = [t for t in SP500_TICKERS if t and "." not in t and "/" not in t]

    batch_size = Config.NEWS_BATCH_SIZE
    vol_map: dict[str, float] = {}

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="iex",
            )
            bars = stock_data_client.get_stock_bars(req)
            df = bars.df
            if df is None or df.empty:
                continue
            if isinstance(df.index, pd.MultiIndex):
                df = df.reset_index()
            else:
                df = df.reset_index()

            if "symbol" not in df.columns:
                continue

            for sym, grp in df.groupby("symbol"):
                if "volume" in grp.columns:
                    vol_map[sym] = float(grp["volume"].tail(5).mean())
        except Exception as e:
            print(f"[SymbolUniverse] Batch {i // batch_size + 1} error: {e}")
        time.sleep(0.2)  # light courtesy sleep between Alpaca batches

    if not vol_map:
        print("[SymbolUniverse] No volume data fetched — skipping DB update")
        return

    sorted_syms = sorted(vol_map, key=lambda s: vol_map[s], reverse=True)[:top_n]
    sorted_syms = [s for s in sorted_syms if vol_map[s] >= 5_000_000]  # 5M ADV floor
    ranked = [(sym, int(vol_map[sym]), rank + 1) for rank, sym in enumerate(sorted_syms)]

    _ensure_symbol_universe_table(db_engine)
    with db_engine.begin() as conn:
        for sym, avg_vol, rank in ranked:
            conn.execute(sql_text("""
                INSERT INTO symbol_universe (symbol, avg_volume, rank, updated_at)
                VALUES (:sym, :vol, :rank, NOW())
                ON CONFLICT (symbol) DO UPDATE SET
                    avg_volume = EXCLUDED.avg_volume,
                    rank       = EXCLUDED.rank,
                    updated_at = EXCLUDED.updated_at
            """), {"sym": sym, "vol": avg_vol, "rank": rank})

    print(f"[SymbolUniverse] Refreshed — top {len(ranked)} symbols by 5-day avg volume")


def get_discovery_candidates(
    db_engine,
    exclude: list[str] | None = None,
    top_n: int = 20,
) -> list[str]:
    """Return top top_n symbols from symbol_universe, excluding symbols in exclude list."""
    if db_engine is None:
        return []
    exclude_set = set(exclude or [])
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT symbol FROM symbol_universe
                ORDER BY rank ASC
                LIMIT :limit
            """), {"limit": top_n + len(exclude_set) + 10}).mappings().fetchall()
        candidates = [r["symbol"] for r in rows if r["symbol"] not in exclude_set]
        return candidates[:top_n]
    except Exception as e:
        print(f"[SymbolUniverse] get_discovery_candidates error: {e}")
        return []
