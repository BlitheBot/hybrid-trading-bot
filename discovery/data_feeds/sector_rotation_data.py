"""
Historical sector-ETF relative-strength loader — feeds the Sector Rotation
discovery family (Strategy Family 8, Task 6).

Thesis
------
Money rotates between the 11 GICS sectors with the economic cycle. Trading a
stock that *leads* a top-performing sector (or *lags* a bottom-performing one)
has a higher base rate than trading it in isolation.

What this module provides
-------------------------
For one symbol's OHLCV bars, ``attach_sector_rotation`` adds these per-bar columns
(all derived from the 11 sector ETFs, *not* from the permuted stock price — so they
survive the MCPT bar permutation exactly like the earnings / short-interest feeds):

  * ``sector_rank_{p}``  for p in {10,20,30}: 1-based rank (1 = strongest) of this
    symbol's sector ETF among the 11, by p-day return. NaN where unavailable.
  * ``sector_ret_{p}``   for p in {10,20,30}: this symbol's sector ETF p-day return.
  * ``sector_vol_ratio``: sector ETF volume / its 20-day average volume (institutional
    participation proxy), aligned to each bar date.

Ranking sectors by "relative strength vs SPY" is order-equivalent to ranking by the
sector's own return (subtracting the common SPY return preserves order), and
"stock leads its sector" reduces to ``stock_return > sector_return`` (SPY cancels),
so SPY is not needed here — kept intentionally simple.

COST / FAIL-OPEN CONTRACT
-------------------------
The 11 ETF daily histories are fetched once via Alpaca and cached to
``discovery/data/sector/{ETF}.parquet`` (7-day TTL), shared across every symbol in a
run. A symbol whose sector ETF can't be resolved, or any fetch/parse failure, yields
all-NaN columns → ``SectorRotationPositionStrategy`` stays all-flat. It never crashes
the pipeline. All exceptions are caught and logged with a full traceback (no bare
excepts).
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import Config

# The 11 GICS sector SPDR ETFs.
SECTOR_ETFS = [
    "XLK",   # Information Technology
    "XLV",   # Health Care
    "XLF",   # Financials
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLU",   # Utilities
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLC",   # Communication Services
]
RS_PERIODS = (10, 20, 30)
_VOL_AVG_WINDOW = 20

# GICS-equivalent sector name → sector ETF. Covers CorrelationGuard.SECTOR_MAP's
# label vocabulary plus the canonical GICS names Finnhub returns.
GICS_TO_ETF = {
    "Information Technology": "XLK",
    "Technology": "XLK",
    "Tech": "XLK",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Cyclical": "XLY",
    "Consumer Staples": "XLP",
    "Consumer Defensive": "XLP",
    "Consumer/Defensive": "XLP",
    "Utilities": "XLU",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Communications": "XLC",
}

# Well-known large caps → sector ETF, so the most-traded names resolve without a
# network profile lookup. Extend as the live universe grows.
SYMBOL_TO_ETF = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AVGO": "XLK", "ORCL": "XLK",
    "CRM": "XLK", "ADBE": "XLK", "AMD": "XLK", "CSCO": "XLK", "ACN": "XLK",
    "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "T": "XLC", "VZ": "XLC", "CMCSA": "XLC",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "LOW": "XLY", "SBUX": "XLY", "BKNG": "XLY",
    "JPM": "XLF", "V": "XLF", "MA": "XLF", "BAC": "XLF", "WFC": "XLF",
    "BRK.B": "XLF", "GS": "XLF", "MS": "XLF", "AXP": "XLF", "BLK": "XLF",
    "UNH": "XLV", "JNJ": "XLV", "LLY": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "PFE": "XLV", "TMO": "XLV", "ABT": "XLV",
    "COST": "XLP", "PG": "XLP", "WMT": "XLP", "KO": "XLP", "PEP": "XLP",
    "PM": "XLP", "MO": "XLP",
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE",
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU",
    "CAT": "XLI", "BA": "XLI", "HON": "XLI", "GE": "XLI", "UPS": "XLI",
    "LIN": "XLB", "SHW": "XLB", "FCX": "XLB",
    "PLD": "XLRE", "AMT": "XLRE", "EQIX": "XLRE",
}

_SECTOR_DIR = Path(__file__).resolve().parent.parent / "data" / "sector"
_RESOLVE_CACHE_PATH = _SECTOR_DIR / "_symbol_etf.json"
_CACHE_TTL_HOURS = 168  # 7 days

# In-process caches.
_data_client = None
_etf_history: dict[str, pd.DataFrame] = {}
_symbol_etf_resolved: dict[str, str | None] | None = None


def _get_data_client():
    """Lazy singleton Alpaca stock data client (built from Config). None on failure."""
    global _data_client
    if _data_client is not None:
        return _data_client
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from utils import apply_http_timeout
        _data_client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
        )
        apply_http_timeout(_data_client)
    except Exception:
        print(f"[Sector] Alpaca client init failed:\n{traceback.format_exc()}")
        _data_client = None
    return _data_client


def _etf_cache(etf: str) -> Path:
    return _SECTOR_DIR / f"{etf}.parquet"


def _fetch_etf_history(etf: str, days_back: int = 400) -> pd.DataFrame:
    """
    Return daily [close, volume] for ``etf`` indexed by date, cached to parquet
    (7-day TTL). Empty DataFrame on any failure (fail-open).
    """
    empty = pd.DataFrame(columns=["close", "volume"])
    if etf in _etf_history:
        return _etf_history[etf]

    cache = _etf_cache(etf)
    if cache.exists():
        age = datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)
        if age < timedelta(hours=_CACHE_TTL_HOURS):
            try:
                import pyarrow.parquet as pq
                df = pq.read_table(str(cache)).to_pandas()
                df = df.set_index(pd.DatetimeIndex(df["date"])).drop(columns=["date"])
                _etf_history[etf] = df
                return df
            except Exception:
                print(f"[Sector] cache read failed for {etf}:\n{traceback.format_exc()}")

    client = _get_data_client()
    if client is None:
        _etf_history[etf] = empty
        return empty
    try:
        from alpaca.data.timeframe import TimeFrame
        from utils import get_historical_bars
        bars = get_historical_bars(etf, TimeFrame.Day, days_back, client, is_crypto=False)
        if bars is None or bars.empty or "close" not in bars.columns:
            _etf_history[etf] = empty
            return empty
        df = pd.DataFrame({
            "close": bars["close"].to_numpy(dtype=float),
            "volume": bars["volume"].to_numpy(dtype=float) if "volume" in bars.columns
            else np.full(len(bars), np.nan),
        }, index=pd.DatetimeIndex(bars.index).normalize())
        df = df[~df.index.duplicated(keep="last")].sort_index()
        _etf_history[etf] = df
        try:
            _SECTOR_DIR.mkdir(parents=True, exist_ok=True)
            import pyarrow as pa
            import pyarrow.parquet as pq
            out = df.reset_index().rename(columns={"index": "date"})
            pq.write_table(pa.Table.from_pandas(out), str(cache))
        except Exception:
            print(f"[Sector] cache write failed for {etf}:\n{traceback.format_exc()}")
        return df
    except Exception:
        print(f"[Sector] history fetch failed for {etf}:\n{traceback.format_exc()}")
        _etf_history[etf] = empty
        return empty


def _load_resolve_cache() -> dict[str, str | None]:
    global _symbol_etf_resolved
    if _symbol_etf_resolved is not None:
        return _symbol_etf_resolved
    if _RESOLVE_CACHE_PATH.exists():
        try:
            _symbol_etf_resolved = json.loads(_RESOLVE_CACHE_PATH.read_text(encoding="utf-8"))
            return _symbol_etf_resolved
        except Exception:
            print(f"[Sector] resolve cache read failed:\n{traceback.format_exc()}")
    _symbol_etf_resolved = {}
    return _symbol_etf_resolved


def _save_resolve_cache() -> None:
    try:
        _SECTOR_DIR.mkdir(parents=True, exist_ok=True)
        _RESOLVE_CACHE_PATH.write_text(json.dumps(_symbol_etf_resolved or {}), encoding="utf-8")
    except Exception:
        print(f"[Sector] resolve cache write failed:\n{traceback.format_exc()}")


def resolve_sector_etf(symbol: str) -> str | None:
    """
    Map ``symbol`` to its sector ETF. Resolution order:
      1. built-in SYMBOL_TO_ETF table,
      2. CorrelationGuard.SECTOR_MAP (GICS name → ETF),
      3. Finnhub /stock/profile2 sector → ETF (cached to disk),
      4. None (caller degrades to all-flat).
    """
    sym = symbol.upper().strip()
    if sym in SYMBOL_TO_ETF:
        return SYMBOL_TO_ETF[sym]

    try:
        from strategies.correlation_guard import CorrelationGuard
        gics = CorrelationGuard.SECTOR_MAP.get(sym)
        if gics and gics in GICS_TO_ETF:
            return GICS_TO_ETF[gics]
    except Exception:
        print(f"[Sector] SECTOR_MAP lookup failed for {sym}:\n{traceback.format_exc()}")

    cache = _load_resolve_cache()
    if sym in cache:
        return cache[sym]

    etf = _resolve_via_finnhub(sym)
    cache[sym] = etf
    _save_resolve_cache()
    return etf


def _resolve_via_finnhub(symbol: str) -> str | None:
    """Look up a symbol's sector via Finnhub profile2 and map to an ETF. None on failure."""
    key = getattr(Config, "FINNHUB_API_KEY", None)
    if not key:
        return None
    try:
        import requests
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/profile2",
            params={"symbol": symbol, "token": key},
            timeout=6,
        )
        data = resp.json() if resp.status_code == 200 else {}
        industry = (data.get("finnhubIndustry") or "").strip()
        return GICS_TO_ETF.get(industry)
    except Exception:
        print(f"[Sector] Finnhub sector lookup failed for {symbol}:\n{traceback.format_exc()}")
        return None


def _all_etf_closes(bar_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Return a [n_bars × 11] DataFrame of each ETF's close forward-filled onto
    ``bar_dates``. Missing ETFs are all-NaN columns.
    """
    cols: dict[str, np.ndarray] = {}
    for etf in SECTOR_ETFS:
        hist = _fetch_etf_history(etf)
        if hist.empty:
            cols[etf] = np.full(len(bar_dates), np.nan)
            continue
        aligned = hist["close"].reindex(
            hist["close"].index.union(bar_dates)
        ).sort_index().ffill().reindex(bar_dates)
        cols[etf] = aligned.to_numpy(dtype=float)
    return pd.DataFrame(cols, index=bar_dates)


def attach_sector_rotation(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Return a copy of ``bars`` enriched with the sector-rotation columns described in
    the module docstring. All-NaN (→ all-flat strategy) when the symbol's sector ETF
    can't be resolved or no ETF data is available.
    """
    out = bars.copy()
    n = len(out)
    for p in RS_PERIODS:
        out[f"sector_rank_{p}"] = np.full(n, np.nan)
        out[f"sector_ret_{p}"] = np.full(n, np.nan)
    out["sector_vol_ratio"] = np.full(n, np.nan)

    if n == 0 or not isinstance(out.index, pd.DatetimeIndex):
        return out

    if not getattr(Config, "DISCOVERY_SECTOR_ROTATION_ENABLED", True):
        return out

    own_etf = resolve_sector_etf(symbol)
    if own_etf is None:
        print(f"[Sector] {symbol}: sector ETF unresolved — rotation family stays all-flat")
        return out

    try:
        bar_dates = pd.DatetimeIndex(out.index).normalize()
        closes = _all_etf_closes(bar_dates)
        if own_etf not in closes.columns or closes[own_etf].isna().all():
            print(f"[Sector] {symbol}: no {own_etf} data — rotation family stays all-flat")
            return out

        # Per-period returns for every ETF, then cross-sectional rank (1 = strongest).
        for p in RS_PERIODS:
            rets = closes / closes.shift(p) - 1.0          # n × 11
            # rank descending: highest return → rank 1. NaNs keep NaN rank.
            ranks = rets.rank(axis=1, ascending=False, method="min")
            out[f"sector_rank_{p}"] = ranks[own_etf].to_numpy(dtype=float)
            out[f"sector_ret_{p}"] = rets[own_etf].to_numpy(dtype=float)

        # Sector ETF volume ratio aligned to the bars.
        hist = _fetch_etf_history(own_etf)
        if not hist.empty and "volume" in hist.columns:
            vol = hist["volume"].reindex(
                hist["volume"].index.union(bar_dates)
            ).sort_index().ffill().reindex(bar_dates)
            vol_avg = vol.rolling(_VOL_AVG_WINDOW, min_periods=1).mean()
            ratio = (vol / vol_avg.replace(0.0, np.nan)).to_numpy(dtype=float)
            out["sector_vol_ratio"] = ratio
    except Exception:
        print(f"[Sector] enrich failed for {symbol}:\n{traceback.format_exc()}")
        return out

    return out


if __name__ == "__main__":
    # Offline smoke test of the ranking math via a stubbed ETF history (no network).
    idx = pd.date_range("2023-01-02", periods=80, freq="B")
    # XLK strongest uptrend, XLU flat — rank should place XLK near 1.
    for etf in SECTOR_ETFS:
        slope = 0.5 if etf == "XLK" else (0.0 if etf == "XLU" else 0.1)
        _etf_history[etf] = pd.DataFrame(
            {"close": 100 + slope * np.arange(80), "volume": np.full(80, 1e6)},
            index=idx,
        )
    SYMBOL_TO_ETF["FAKEK"] = "XLK"
    bars = pd.DataFrame({"close": np.linspace(50, 70, 80),
                         "volume": np.full(80, 5e5)}, index=idx)
    enr = attach_sector_rotation(bars, "FAKEK")
    last_rank = enr["sector_rank_20"].dropna().iloc[-1]
    print("XLK rank (should be 1):", last_rank)
    assert last_rank == 1.0
    print("All sector_rotation_data smoke tests passed.")
