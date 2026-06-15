"""
Historical SEC Form 4 insider-filing loader — fixes Strategy Family 4
(InsiderFlowStrategy), which previously degraded to all-flat because the
backtester had no historical insider feed.

Approach
--------
For a symbol we resolve its CIK from SEC's company-ticker map, pull the EDGAR
submissions JSON (``https://data.sec.gov/submissions/CIK##########.json``), filter
the ``form == "4"`` filings inside the requested date window, fetch + parse each
Form 4 XML (reusing the live strategy's parser), and aggregate the net open-market
insider dollar value (buys − sells) per filing date.

The task description referenced the EDGAR full-text search endpoint
(``efts.sec.gov/LATEST/search-index``); that index is keyed for free-text queries
and is awkward to scope reliably to one issuer's Form 4s. The submissions API is
the documented per-entity filing index and is used here instead (noted as a
deliberate deviation).

COST / FAIL-OPEN CONTRACT
-------------------------
Each Form 4 is a separate XML fetch, so building a full multi-year history is
network-heavy and SEC-rate-limited. Each call is therefore bounded by
``max_filings`` (most-recent first) and everything is cached:
  * the ticker→CIK map → discovery/data/insider/_ticker_cik.json
  * per-symbol net-value history → discovery/data/insider/{symbol}_insider.parquet
Over a long backtest window the available history is PARTIAL. With no data the
``insider_buy_value`` column is all-zeros and InsiderFlowStrategy stays all-flat —
it does NOT crash the pipeline. All failures are caught and logged with a full
traceback (no bare excepts).
"""
from __future__ import annotations

import json
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import Config
from strategies.sec_edgar_strategy import (
    _find_form4_xml_url,
    _http_get,
    _parse_form4_xml,
)

INSIDER_COLUMN = "insider_buy_value"

_INSIDER_DIR = Path(__file__).resolve().parent.parent / "data" / "insider"
_TICKER_MAP_PATH = _INSIDER_DIR / "_ticker_cik.json"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_RATE_LIMIT_SLEEP = 0.15

# In-process caches.
_ticker_cik_map: dict[str, int] | None = None


def _load_ticker_cik_map() -> dict[str, int]:
    """Return {TICKER: cik_int}, cached on disk + in memory. Empty on failure."""
    global _ticker_cik_map
    if _ticker_cik_map is not None:
        return _ticker_cik_map

    # Disk cache (refreshed monthly — CIKs are stable).
    if _TICKER_MAP_PATH.exists():
        age = datetime.now() - datetime.fromtimestamp(_TICKER_MAP_PATH.stat().st_mtime)
        if age < timedelta(days=30):
            try:
                _ticker_cik_map = json.loads(_TICKER_MAP_PATH.read_text(encoding="utf-8"))
                return _ticker_cik_map
            except Exception:
                print(f"[EDGAR] ticker map cache read failed:\n{traceback.format_exc()}")

    data = _http_get(_TICKER_MAP_URL)
    if not data:
        _ticker_cik_map = {}
        return _ticker_cik_map
    try:
        raw = json.loads(data)
        mapping: dict[str, int] = {}
        for row in raw.values():
            ticker = str(row.get("ticker", "")).upper().strip()
            cik = row.get("cik_str")
            if ticker and cik is not None:
                mapping[ticker] = int(cik)
        _ticker_cik_map = mapping
        try:
            _INSIDER_DIR.mkdir(parents=True, exist_ok=True)
            _TICKER_MAP_PATH.write_text(json.dumps(mapping), encoding="utf-8")
        except Exception:
            print(f"[EDGAR] ticker map cache write failed:\n{traceback.format_exc()}")
        return mapping
    except Exception:
        print(f"[EDGAR] ticker map parse failed:\n{traceback.format_exc()}")
        _ticker_cik_map = {}
        return _ticker_cik_map


def _ticker_to_cik(symbol: str) -> int | None:
    return _load_ticker_cik_map().get(symbol.upper().strip())


def _per_symbol_cache(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(".", "_")
    return _INSIDER_DIR / f"{safe}_insider.parquet"


def get_form4_history(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    max_filings: int = 150,
) -> pd.DataFrame:
    """
    Return a DataFrame [date, net_value] of net open-market insider dollar value
    (buys − sells) per Form 4 filing date for ``symbol`` within [start, end].

    Cached per symbol. Empty DataFrame on any failure (fail-open).
    """
    empty = pd.DataFrame(columns=["date", "net_value"])

    cache = _per_symbol_cache(symbol)
    if cache.exists():
        age = datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)
        if age < timedelta(hours=168):
            try:
                import pyarrow.parquet as pq
                return pq.read_table(str(cache)).to_pandas()
            except Exception:
                print(f"[EDGAR] per-symbol cache read failed for {symbol}:\n{traceback.format_exc()}")

    cik = _ticker_to_cik(symbol)
    if cik is None:
        return empty

    data = _http_get(_SUBMISSIONS_URL.format(cik=cik))
    time.sleep(_RATE_LIMIT_SLEEP)
    if not data:
        return empty

    try:
        subs = json.loads(data)
        recent = subs.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
    except Exception:
        print(f"[EDGAR] submissions parse failed for {symbol}:\n{traceback.format_exc()}")
        return empty

    start_d = pd.to_datetime(start).date() if start else None
    end_d = pd.to_datetime(end).date() if end else None

    # Collect candidate Form 4 filings (most recent first), bounded by max_filings.
    candidates: list[tuple[str, str]] = []  # (filingDate, accession)
    for form, fdate, accn in zip(forms, dates, accns):
        if form != "4":
            continue
        try:
            d = pd.to_datetime(fdate).date()
        except Exception:
            continue
        if start_d and d < start_d:
            continue
        if end_d and d > end_d:
            continue
        candidates.append((fdate, accn))
        if len(candidates) >= max_filings:
            break

    if not candidates:
        return empty

    records: dict[str, float] = {}  # filingDate → net value
    for fdate, accn in candidates:
        cik_acc = str(int(accn.split("-")[0])) if "-" in accn else str(cik)
        xml_url = _find_form4_xml_url([str(cik), cik_acc], accn)
        if not xml_url:
            continue
        time.sleep(_RATE_LIMIT_SLEEP)
        xml_data = _http_get(xml_url)
        if not xml_data:
            continue
        parsed = _parse_form4_xml(xml_data)
        if not parsed:
            continue
        net = float(parsed.get("total_buy", 0.0)) - float(parsed.get("total_sell", 0.0))
        records[fdate] = records.get(fdate, 0.0) + net

    if not records:
        return empty

    df = pd.DataFrame(
        {"date": [pd.to_datetime(d).normalize() for d in records.keys()],
         "net_value": list(records.values())}
    ).sort_values("date").reset_index(drop=True)

    try:
        _INSIDER_DIR.mkdir(parents=True, exist_ok=True)
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pandas(df), str(cache))
    except Exception:
        print(f"[EDGAR] per-symbol cache write failed for {symbol}:\n{traceback.format_exc()}")
    return df


def get_insider_signal(symbol: str, on_date, lookback_days: int = 5) -> float:
    """
    Net insider buying ($) over the ``lookback_days``-calendar-day window ending on
    ``on_date``. Positive = net buying, negative = net selling, 0.0 = no data.
    """
    try:
        hist = get_form4_history(symbol)
        if hist.empty:
            return 0.0
        end_d = pd.to_datetime(on_date).normalize()
        start_d = end_d - pd.Timedelta(days=lookback_days)
        window = hist[(hist["date"] > start_d) & (hist["date"] <= end_d)]
        return float(window["net_value"].sum())
    except Exception:
        print(f"[EDGAR] get_insider_signal failed for {symbol}:\n{traceback.format_exc()}")
        return 0.0


def is_data_stale(symbol: str, stale_days: int | None = None) -> bool:
    """True if the newest Form 4 filing is older than ``stale_days`` (default config)."""
    stale_days = stale_days if stale_days is not None else Config.INSIDER_DATA_STALE_DAYS
    try:
        hist = get_form4_history(symbol)
        if hist.empty:
            return True
        newest = pd.to_datetime(hist["date"]).max().date()
        return (date.today() - newest).days > stale_days
    except Exception:
        print(f"[EDGAR] staleness check failed for {symbol}:\n{traceback.format_exc()}")
        return True


def attach_insider_buy_value(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Return a copy of ``bars`` with a per-bar ``insider_buy_value`` column: the net
    insider dollar value (buys − sells) of any Form 4 filings landing on that bar's
    date, 0.0 elsewhere. Used by the discovery engine to enrich OHLCV bars before
    validating InsiderFlowStrategy.
    """
    out = bars.copy()
    n = len(out)
    out[INSIDER_COLUMN] = np.zeros(n, dtype=float)

    if n == 0 or not isinstance(out.index, pd.DatetimeIndex):
        return out

    start = str(pd.DatetimeIndex(out.index).min().date())
    end = str(pd.DatetimeIndex(out.index).max().date())
    try:
        hist = get_form4_history(symbol, start=start, end=end)
    except Exception:
        print(f"[EDGAR] history fetch failed for {symbol}:\n{traceback.format_exc()}")
        return out
    if hist.empty:
        if Config.DISCOVERY_INSIDER_FEED_ENABLED:
            print(f"[EDGAR] {symbol}: no historical Form 4 data — insider family stays all-flat")
        return out

    if is_data_stale(symbol):
        print(f"[EDGAR] {symbol}: insider data STALE (newest filing > "
              f"{Config.INSIDER_DATA_STALE_DAYS}d old)")

    values = out[INSIDER_COLUMN].to_numpy(dtype=float).copy()
    bar_dates = pd.DatetimeIndex(out.index).normalize()
    for _, row in hist.iterrows():
        pos = bar_dates.searchsorted(row["date"], side="left")
        if pos < n:
            values[pos] += float(row["net_value"])
    out[INSIDER_COLUMN] = values
    return out


if __name__ == "__main__":
    # Offline smoke test of the windowing math (no network) via a stubbed history.
    hist = pd.DataFrame({
        "date": pd.to_datetime(["2023-01-10", "2023-01-12", "2023-02-01"]),
        "net_value": [100_000.0, 50_000.0, -25_000.0],
    })
    globals()["get_form4_history"] = lambda *a, **k: hist
    sig = get_insider_signal("TEST", "2023-01-13", lookback_days=5)
    print("5-day net insider buying ending 2023-01-13:", sig)
    assert abs(sig - 150_000.0) < 1e-6
    idx = pd.date_range("2023-01-01", periods=40, freq="B")
    bars = pd.DataFrame({"close": np.linspace(100, 110, 40)}, index=idx)
    enriched = attach_insider_buy_value(bars, "TEST")
    assert INSIDER_COLUMN in enriched.columns
    assert abs(enriched[INSIDER_COLUMN].sum() - 125_000.0) < 1e-6
    print("Insider enrich non-zero bars:",
          int((enriched[INSIDER_COLUMN] != 0).sum()))
    print("\nAll edgar_historical smoke tests passed.")
