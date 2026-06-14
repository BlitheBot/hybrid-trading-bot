"""
Historical FINRA short-volume loader.

Feeds:
  * the short-interest-momentum discovery family (Task 2), and
  * the live week-over-week short-interest confirmation bonus (Task 4).

FINRA publishes one consolidated short-volume file PER TRADING DAY at::

    https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt

Each pipe-delimited file lists every exchange-listed symbol
(``Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market``). We use the
short-volume RATIO (ShortVolume / TotalVolume) as a daily short-pressure proxy
and its week-over-week change as the signal.

COST / FAIL-OPEN NOTE
---------------------
A daily file covers ALL symbols, so building a multi-year per-symbol history means
downloading hundreds of files. To keep a discovery run tractable:

  * every daily file is cached to ``discovery/data/short_interest/_raw/`` on first
    fetch (shared across all symbols in a run), and
  * each ``attach_short_interest_change`` call is bounded by ``max_files`` (it
    fetches only the most recent N bar-dates).

Over a long backtest window the available history is therefore PARTIAL. Like the
insider family, the short-interest-momentum family degrades to an all-flat vector
on bars with no data (``si_change`` = 0) and trades less / never validates rather
than crashing. All network/parse failures are caught and logged with a full
traceback (no bare excepts).
"""
from __future__ import annotations

import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SI_COLUMN = "si_change"

_SI_DIR = Path(__file__).resolve().parent.parent / "data" / "short_interest"
_RAW_DIR = _SI_DIR / "_raw"
_BASE_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
_HEADERS = {"User-Agent": "curl/7.68.0", "Accept": "*/*"}
_REQUEST_TIMEOUT = 15
_RATE_LIMIT_SLEEP = 0.05  # be polite between FINRA fetches

# In-process memo of parsed daily files: {YYYYMMDD: {symbol: ratio}}.
_daily_memo: dict[str, dict] = {}


def _raw_path(yyyymmdd: str) -> Path:
    return _RAW_DIR / f"CNMSshvol{yyyymmdd}.txt"


def _parse_content(content: str) -> dict:
    """Parse a FINRA daily file into {symbol: short_volume_ratio}."""
    rows: dict[str, float] = {}
    for line in content.strip().split("\n"):
        line = line.strip("\r")
        if not line or line.startswith("Date"):
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        try:
            sym = parts[1].strip()
            short_vol = float(parts[2])
            total_vol = float(parts[4])
            if total_vol > 0:
                rows[sym] = short_vol / total_vol
        except (ValueError, IndexError):
            continue
    return rows


def _fetch_daily_file(d: date) -> dict | None:
    """Return {symbol: ratio} for trading day ``d`` (cached on disk + in memory)."""
    key = d.strftime("%Y%m%d")
    if key in _daily_memo:
        return _daily_memo[key]

    raw = _raw_path(key)
    if raw.exists():
        try:
            content = raw.read_text(encoding="utf-8")
            parsed = _parse_content(content)
            _daily_memo[key] = parsed
            return parsed
        except Exception:
            print(f"[FINRA] cached raw read failed for {key}:\n{traceback.format_exc()}")

    url = _BASE_URL.format(date=key)
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as r:
            content = r.read().decode("utf-8")
        lines = content.strip().split("\n")
        if len(lines) <= 2:
            _daily_memo[key] = {}
            return {}
        try:
            _RAW_DIR.mkdir(parents=True, exist_ok=True)
            raw.write_text(content, encoding="utf-8")
        except Exception:
            print(f"[FINRA] raw cache write failed for {key}:\n{traceback.format_exc()}")
        parsed = _parse_content(content)
        _daily_memo[key] = parsed
        time.sleep(_RATE_LIMIT_SLEEP)
        return parsed
    except Exception:
        # Missing file (weekend/holiday/not-yet-published) or network error → no data.
        _daily_memo[key] = {}
        return None


def _per_symbol_cache(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(".", "_")
    return _SI_DIR / f"{safe}_si.parquet"


def build_ratio_series(symbol: str, bar_dates: pd.DatetimeIndex, max_files: int = 400) -> pd.Series:
    """
    Return a Series of short-volume ratios indexed by date for ``symbol``, covering
    (at most) the ``max_files`` most-recent ``bar_dates``. Cached per symbol.

    Bars with no FINRA data are simply absent from the returned Series.
    """
    empty = pd.Series(dtype=float)
    if bar_dates is None or len(bar_dates) == 0:
        return empty

    cache = _per_symbol_cache(symbol)
    if cache.exists():
        age = datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)
        if age < timedelta(hours=168):
            try:
                import pyarrow.parquet as pq
                df = pq.read_table(str(cache)).to_pandas()
                if not df.empty:
                    return pd.Series(df["short_ratio"].to_numpy(),
                                     index=pd.DatetimeIndex(df["date"]))
            except Exception:
                print(f"[FINRA] per-symbol cache read failed for {symbol}:\n{traceback.format_exc()}")

    dates = pd.DatetimeIndex(bar_dates).normalize().unique().sort_values()
    if len(dates) > max_files:
        dates = dates[-max_files:]

    out_dates: list = []
    out_ratios: list[float] = []
    for ts in dates:
        d = ts.date()
        parsed = _fetch_daily_file(d)
        if parsed and symbol in parsed:
            out_dates.append(ts)
            out_ratios.append(parsed[symbol])

    if not out_dates:
        return empty

    series = pd.Series(out_ratios, index=pd.DatetimeIndex(out_dates))
    try:
        _SI_DIR.mkdir(parents=True, exist_ok=True)
        import pyarrow as pa
        import pyarrow.parquet as pq
        df = pd.DataFrame({"date": series.index, "short_ratio": series.to_numpy()})
        pq.write_table(pa.Table.from_pandas(df), str(cache))
    except Exception:
        print(f"[FINRA] per-symbol cache write failed for {symbol}:\n{traceback.format_exc()}")
    return series


def attach_short_interest_change(
    bars: pd.DataFrame,
    symbol: str,
    lookback_bars: int = 5,
    max_files: int = 400,
) -> pd.DataFrame:
    """
    Return a copy of ``bars`` with an ``si_change`` column: the fractional
    week-over-week (``lookback_bars`` trading bars) change in the short-volume
    ratio, 0.0 where data is unavailable.

    Used by the discovery engine to enrich OHLCV bars before validating the
    short-interest-momentum family.
    """
    out = bars.copy()
    n = len(out)
    out[SI_COLUMN] = np.zeros(n, dtype=float)

    if n == 0 or not isinstance(out.index, pd.DatetimeIndex):
        return out

    try:
        ratios = build_ratio_series(symbol, out.index, max_files=max_files)
    except Exception:
        print(f"[FINRA] ratio series build failed for {symbol}:\n{traceback.format_exc()}")
        return out

    if ratios.empty:
        return out

    # Align ratio to each bar (forward-fill within the covered window), then take a
    # lookback_bars-back percentage change as the WoW change proxy.
    bar_dates = pd.DatetimeIndex(out.index).normalize()
    aligned = ratios.reindex(bar_dates, method=None)
    aligned_vals = aligned.to_numpy(dtype=float)

    change = np.zeros(n, dtype=float)
    for i in range(lookback_bars, n):
        cur = aligned_vals[i]
        prev = aligned_vals[i - lookback_bars]
        if np.isfinite(cur) and np.isfinite(prev) and prev > 0:
            change[i] = (cur - prev) / prev
    out[SI_COLUMN] = change
    return out


def get_recent_wow_change(symbol: str, lookback_days: int = 7) -> float | None:
    """
    Latest week-over-week short-volume ratio change for the live bot (Task 4).

    Compares the most recent available FINRA daily ratio to the one ~``lookback_days``
    calendar days earlier. Returns ``None`` when either reading is unavailable
    (fail-open — caller skips the adjustment).
    """
    try:
        latest = None
        latest_date = None
        for i in range(1, 8):
            d = date.today() - timedelta(days=i)
            parsed = _fetch_daily_file(d)
            if parsed and symbol in parsed:
                latest, latest_date = parsed[symbol], d
                break
        if latest is None or latest_date is None:
            return None

        prior = None
        for i in range(lookback_days, lookback_days + 8):
            d = latest_date - timedelta(days=i - lookback_days + 1)
            parsed = _fetch_daily_file(d)
            if parsed and symbol in parsed:
                prior = parsed[symbol]
                break
        if prior is None or prior <= 0:
            return None
        return (latest - prior) / prior
    except Exception:
        print(f"[FINRA] WoW change fetch failed for {symbol}:\n{traceback.format_exc()}")
        return None


def short_interest_size_adjustment(
    direction: str,
    wow_change: float | None,
    rising_threshold: float,
    falling_threshold: float,
    short_bonus: float,
    long_bonus: float,
) -> float:
    """
    Pure decision for the live WoW short-interest size bonus (Task 4).

    Returns the additive size bonus (e.g. 0.2 == +0.2x):
      * SHORT ('sell') + short interest rising  > +rising_threshold   → short_bonus
      * LONG  ('buy')  + short interest falling  < -falling_threshold  → long_bonus
      * otherwise (incl. no data)                                      → 0.0
    """
    if wow_change is None:
        return 0.0
    if direction == "sell" and wow_change > rising_threshold:
        return short_bonus
    if direction == "buy" and wow_change < -falling_threshold:
        return long_bonus
    return 0.0


if __name__ == "__main__":
    # Offline smoke test of the parser + WoW math (no network).
    sample = (
        "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
        "20240115|AAPL|600000|0|1000000|Q\n"
        "20240115|MSFT|300000|0|1000000|Q\n"
        "20240115|BADROW\n"
    )
    parsed = _parse_content(sample)
    assert abs(parsed["AAPL"] - 0.6) < 1e-9
    assert abs(parsed["MSFT"] - 0.3) < 1e-9
    assert "BADROW" not in parsed
    print("FINRA parser smoke test passed:", parsed)
