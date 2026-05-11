"""
Ranks S&P 500 symbols by trailing 30-day average daily volume via Alpaca.
Returns the top N by volume, filtering out illiquid symbols (< 5M avg shares/day).
Cache TTL: 168 hours (1 week). Falls back to cached list, then hardcoded fallback.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from data.sp500_tickers import SP500_TICKERS

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
_CACHE_PATH     = DATA_DIR / "symbol_universe.parquet"
_CACHE_MAX_AGE  = timedelta(hours=168)
_MIN_AVG_VOL    = 5_000_000
_BATCH_SIZE     = 50

# Hardcoded fallback — 50 large-cap S&P 500 symbols with reliable liquidity
_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "BRK.B",
    "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "PG", "MA", "HD", "CVX", "MRK",
    "ABBV", "LLY", "COST", "PEP", "KO", "AVGO", "ORCL", "TMO", "ACN", "MCD",
    "BAC", "NFLX", "ADBE", "CRM", "AMD", "INTC", "TXN", "QCOM", "HON", "UNP",
    "SPY", "QQQ", "GS", "MS", "AXP", "C", "WFC", "BMY", "AMGN", "GILD",
]


def _load_cache() -> list[str] | None:
    if not _CACHE_PATH.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(_CACHE_PATH.stat().st_mtime)
    if age > _CACHE_MAX_AGE:
        return None
    try:
        import pyarrow.parquet as pq
        df = pq.read_table(str(_CACHE_PATH)).to_pandas()
        return df["symbol"].tolist()
    except Exception:
        return None


def _save_cache(symbols: list[str]):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        df = pd.DataFrame({"symbol": symbols})
        pq.write_table(pa.Table.from_pandas(df), str(_CACHE_PATH))
    except Exception as e:
        print(f"[SymbolUniverse] Cache write failed: {e}")


def get_top_n(
    data_client: StockHistoricalDataClient,
    limit: int = 100,
    min_avg_vol: int = _MIN_AVG_VOL,
) -> list[str]:
    """
    Returns up to `limit` S&P 500 symbols ranked by 30-day average daily volume.
    Uses parquet cache (168h TTL). Falls back to cached list on API failure.
    """
    cached = _load_cache()
    if cached is not None:
        print(f"[SymbolUniverse] Using cached universe ({len(cached)} symbols)")
        return cached[:limit]

    print(f"[SymbolUniverse] Fetching 30-day volume for {len(SP500_TICKERS)} S&P 500 symbols...")
    end   = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=35)  # fetch 35 days → ~21 trading days

    vol_map: dict[str, float] = {}
    tickers = list(SP500_TICKERS)

    for i in range(0, len(tickers), _BATCH_SIZE):
        batch = tickers[i : i + _BATCH_SIZE]
        try:
            req  = StockBarsRequest(symbol_or_symbols=batch, timeframe=TimeFrame.Day, start=start, end=end)
            bars = data_client.get_stock_bars(req)
            df   = bars.df
            if df is None or df.empty:
                continue
            if isinstance(df.index, pd.MultiIndex):
                df = df.reset_index()
                for sym, grp in df.groupby("symbol"):
                    vol_map[sym] = grp["volume"].mean()
            else:
                df = df.reset_index()
                for sym, grp in df.groupby("symbol"):
                    vol_map[sym] = grp["volume"].mean()
        except Exception as e:
            print(f"[SymbolUniverse] Batch {i//50+1} fetch error: {e}")

    if not vol_map:
        print("[SymbolUniverse] Volume fetch failed — using fallback list")
        fallback = _load_cache() or _FALLBACK
        return fallback[:limit]

    ranked = sorted(
        [(sym, avg) for sym, avg in vol_map.items() if avg >= min_avg_vol],
        key=lambda x: x[1],
        reverse=True,
    )

    symbols = [sym for sym, _ in ranked[:limit]]
    print(f"[SymbolUniverse] {len(symbols)} symbols pass {min_avg_vol/1e6:.0f}M volume filter (from {len(vol_map)} fetched)")
    _save_cache(symbols)
    return symbols


def refresh(data_client: StockHistoricalDataClient) -> list[str]:
    """Force-expire cache and rebuild. Called Sunday midnight by discovery_loop."""
    if _CACHE_PATH.exists():
        _CACHE_PATH.unlink()
    return get_top_n(data_client)
