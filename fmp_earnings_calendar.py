"""
FMP historical earnings-calendar loader — feeds the PEAD discovery family (Task 1).

NOTE ON PROVENANCE
------------------
The overnight-build task description assumed this module "already existed and was
already integrated". It did not — it is built here from scratch. It pulls
historical earnings surprises (actual vs estimated EPS) from Financial Modeling
Prep's ``/v3/historical/earning_calendar/{symbol}`` endpoint and caches them to
``discovery/data/earnings/{symbol}_earnings.parquet``.

FAIL-OPEN CONTRACT
------------------
Every public function degrades gracefully:
  * No ``FMP_API_KEY``           → empty DataFrame (logged once)
  * HTTP / parse error           → empty DataFrame (full traceback logged)
  * No earnings rows for symbol  → empty DataFrame
``attach_earnings_surprise`` therefore leaves the bar frame unchanged when no
data is available, and ``PEADPositionStrategy`` then returns an all-flat vector
(no trades) rather than crashing the discovery pipeline.

Surprise magnitude is ``(actual - estimate) / abs(estimate)`` as a fraction
(0.05 == +5%). Rows where the estimate is missing or zero are dropped (a surprise
percentage is undefined).
"""
from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from config import Config

_EARNINGS_DIR = Path(__file__).parent / "discovery" / "data" / "earnings"
_BASE_URL = "https://financialmodelingprep.com/api/v3/historical/earning_calendar/{symbol}"
_CACHE_MAX_AGE_HOURS = 168  # one week — the discovery engine runs weekly
_REQUEST_TIMEOUT = 20

_EARNINGS_COLUMN = "earnings_surprise"

# Emit the "no API key" warning only once per process to avoid log spam.
_warned_no_key = False


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(".", "_")
    return _EARNINGS_DIR / f"{safe}_earnings.parquet"


def _read_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age >= timedelta(hours=_CACHE_MAX_AGE_HOURS):
        return None
    try:
        import pyarrow.parquet as pq
        return pq.read_table(str(path)).to_pandas()
    except Exception:
        print(f"[FMP] earnings cache read failed for {path.name}:\n{traceback.format_exc()}")
        return None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        _EARNINGS_DIR.mkdir(parents=True, exist_ok=True)
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pandas(df), str(path))
    except Exception:
        print(f"[FMP] earnings cache write failed for {path.name}:\n{traceback.format_exc()}")


def _parse_rows(rows: list) -> pd.DataFrame:
    """Normalise FMP JSON rows into [date, eps_actual, eps_estimated, surprise_pct]."""
    records = []
    for r in rows:
        try:
            date_str = r.get("date")
            actual = r.get("eps")
            estimate = r.get("epsEstimated")
            if date_str is None or actual is None or estimate is None:
                continue
            actual = float(actual)
            estimate = float(estimate)
            if estimate == 0.0:
                continue  # surprise percentage is undefined
            surprise_pct = (actual - estimate) / abs(estimate)
            records.append({
                "date": pd.to_datetime(date_str).normalize(),
                "eps_actual": actual,
                "eps_estimated": estimate,
                "surprise_pct": surprise_pct,
            })
        except (ValueError, TypeError):
            continue
    if not records:
        return pd.DataFrame(columns=["date", "eps_actual", "eps_estimated", "surprise_pct"])
    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    return df


def get_historical_earnings(symbol: str, *, session: requests.Session | None = None) -> pd.DataFrame:
    """
    Return a DataFrame of historical earnings surprises for ``symbol`` with columns
    [date, eps_actual, eps_estimated, surprise_pct], sorted ascending by date.

    Cached per-symbol to parquet. Returns an empty (correctly-typed) DataFrame on
    any failure or when no API key is configured (fail-open).
    """
    global _warned_no_key
    empty = pd.DataFrame(columns=["date", "eps_actual", "eps_estimated", "surprise_pct"])

    path = _cache_path(symbol)
    cached = _read_cache(path)
    if cached is not None:
        return cached

    if not Config.FMP_API_KEY:
        if not _warned_no_key:
            print("[FMP] FMP_API_KEY not set — PEAD earnings family will degrade to all-flat")
            _warned_no_key = True
        return empty

    try:
        url = _BASE_URL.format(symbol=symbol)
        getter = session.get if session is not None else requests.get
        resp = getter(url, params={"apikey": Config.FMP_API_KEY, "limit": 200},
                      timeout=_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"[FMP] {symbol}: HTTP {resp.status_code} — degrading to all-flat")
            return empty
        payload = resp.json()
        if not isinstance(payload, list) or not payload:
            return empty
        df = _parse_rows(payload)
        if not df.empty:
            _write_cache(path, df)
        return df
    except Exception:
        print(f"[FMP] {symbol}: earnings fetch failed:\n{traceback.format_exc()}")
        return empty


def attach_earnings_surprise(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Return a copy of ``bars`` with an ``earnings_surprise`` column: the EPS surprise
    fraction placed on the first trading bar on or after each announcement date, and
    0.0 everywhere else.

    Used by the discovery engine to enrich OHLCV bars before validating the PEAD
    family. If no earnings data is available the column is added as all-zeros so the
    family runs (and trades nothing) instead of crashing.
    """
    out = bars.copy()
    n = len(out)
    surprise = np.zeros(n, dtype=float)
    out[_EARNINGS_COLUMN] = surprise

    if n == 0 or not isinstance(out.index, pd.DatetimeIndex):
        return out

    earnings = get_historical_earnings(symbol)
    if earnings.empty:
        return out

    idx = out.index
    # Normalize the bar index to date precision for alignment.
    bar_dates = pd.DatetimeIndex(idx).normalize()
    for _, row in earnings.iterrows():
        ann_date = row["date"]
        # First bar at or after the announcement date.
        pos = bar_dates.searchsorted(ann_date, side="left")
        if pos < n:
            surprise[pos] = float(row["surprise_pct"])

    out[_EARNINGS_COLUMN] = surprise
    return out


if __name__ == "__main__":
    # Smoke test with synthetic rows (no network).
    sample = [
        {"date": "2023-01-15", "eps": 1.10, "epsEstimated": 1.00},   # +10%
        {"date": "2023-04-15", "eps": 0.90, "epsEstimated": 1.00},   # -10%
        {"date": "2023-07-15", "eps": 1.00, "epsEstimated": 0.0},    # dropped (estimate 0)
    ]
    parsed = _parse_rows(sample)
    print(parsed)
    assert len(parsed) == 2, "estimate==0 row should be dropped"
    assert abs(parsed.iloc[0]["surprise_pct"] - 0.10) < 1e-9
    assert abs(parsed.iloc[1]["surprise_pct"] + 0.10) < 1e-9

    idx = pd.date_range("2023-01-01", periods=120, freq="B")
    bars = pd.DataFrame({"close": np.linspace(100, 120, 120)}, index=idx)
    # Monkeypatch the loader (current module's global) to use the parsed sample.
    globals()["get_historical_earnings"] = lambda s: parsed
    enriched = attach_earnings_surprise(bars, "TEST")
    assert _EARNINGS_COLUMN in enriched.columns
    nz = enriched[_EARNINGS_COLUMN].to_numpy().nonzero()[0]
    print(f"non-zero surprise bars at indices: {nz.tolist()}")
    assert len(nz) == 2
    print("\nAll fmp_earnings_calendar smoke tests passed.")
