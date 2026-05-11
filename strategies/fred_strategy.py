import asyncio
import csv
import io
import time
from datetime import datetime, timedelta
import urllib.request
import pytz

# ── Module-level macro snapshot ───────────────────────────────────────────────
# Updated by FREDStrategy daily at 7 PM EST.
# Readable by any strategy or bot loop via:
#   from strategies.fred_strategy import MACRO_SNAPSHOT, get_conviction_multiplier
MACRO_SNAPSHOT: dict = {
    # Current indicator values (None until first fetch completes)
    "fed_funds_rate":         None,   # float, %
    "vix":                    None,   # float
    "treasury_10y":           None,   # float, %
    "unemployment":           None,   # float, %
    "cpi_yoy":                None,   # float, % (computed from CPIAUCSL level data)
    # Previous-week baseline — None until first Sunday summary fires
    "prev_week_fed_funds":    None,
    "prev_week_vix":          None,
    "prev_week_treasury":     None,
    "prev_week_unemployment": None,
    "prev_week_cpi_yoy":      None,
    # Macro regime flags
    "fed_rate_cut":           False,  # latest FF rate < prev month
    "yield_rising_fast":      False,  # 10Y up >0.2% in 30 days
    "vix_extreme_fear":       False,  # VIX > 40
    # Metadata
    "last_updated":           None,   # ISO string (UTC)
}

_SERIES_URLS = {
    "fed_funds": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",
    "vix":       "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
    "treasury":  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
    "unemp":     "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE",
    "cpi":       "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL",
}

_HEADERS = {"User-Agent": "HybridTradingBot/1.0 contact@hybridtradingbot.com"}


def get_conviction_multiplier() -> float:
    """
    Returns a macro-regime multiplier for auto-trade strength gates.
    Applied in news_loop and sec_edgar_loop before the auto-trade threshold check.
      1.0 = normal (VIX ≤ 30, or FRED data not yet loaded)
      0.7 = elevated fear (VIX > 30)
    At 0.7× a signal needs strength ~18.6 to cross a threshold of 13 —
    effectively suppressing all auto-trades during high-volatility regimes.
    """
    vix = MACRO_SNAPSHOT.get("vix")
    if vix is not None and vix > 30:
        return 0.7
    return 1.0


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _fetch_csv(url: str) -> list[tuple[str, float]]:
    """
    Fetch a FRED fredgraph.csv file and return sorted (date_str, value) tuples.
    Skips rows where VALUE is '.' (FRED convention for missing data).
    Returns [] on any network or parse error.
    """
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        series_id = url.split("id=")[-1]
        print(f"[FRED] Fetch failed ({series_id}): {e}")
        return []

    rows: list[tuple[str, float]] = []
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # skip header row: DATE,VALUE
    for row in reader:
        if len(row) < 2:
            continue
        date_str, val_str = row[0].strip(), row[1].strip()
        if val_str == ".":
            continue
        try:
            rows.append((date_str, float(val_str)))
        except ValueError:
            continue
    return sorted(rows)


def _latest(series: list[tuple[str, float]]) -> float | None:
    return series[-1][1] if series else None


def _value_n_months_ago(series: list[tuple[str, float]], n: int) -> float | None:
    """Return the value closest to n calendar months before the last entry."""
    if not series:
        return None
    try:
        last_date = datetime.strptime(series[-1][0], "%Y-%m-%d").date()
    except ValueError:
        return None
    target_month = last_date.month - n
    target_year  = last_date.year
    while target_month <= 0:
        target_month += 12
        target_year  -= 1
    best_val, best_diff = None, None
    for date_str, val in series:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = abs((d.year - target_year) * 12 + (d.month - target_month))
        if best_diff is None or diff < best_diff:
            best_diff, best_val = diff, val
    return best_val


def _value_n_days_ago(series: list[tuple[str, float]], n: int) -> float | None:
    """Return the value closest to n calendar days before the last entry."""
    if not series:
        return None
    try:
        last_date = datetime.strptime(series[-1][0], "%Y-%m-%d").date()
    except ValueError:
        return None
    target = last_date - timedelta(days=n)
    best_val, best_diff = None, None
    for date_str, val in series:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = abs((d - target).days)
        if best_diff is None or diff < best_diff:
            best_diff, best_val = diff, val
    return best_val


# ── Strategy class ────────────────────────────────────────────────────────────

class FREDStrategy:
    """
    Fetches 5 FRED macro indicators and updates the module-level MACRO_SNAPSHOT.
    Does not generate trade signals — exposes get_conviction_multiplier() for
    other loops to gate auto-trades during elevated-VIX regimes.

    VIX > 30  → get_conviction_multiplier() returns 0.7 (applied in news + EDGAR loops)
    VIX > 40  → extreme fear event returned for one-time Slack alert per calendar day
    FF cut    → fed_rate_cut flag (bullish macro context, informational)
    10Y +0.2% in 30d → yield_rising_fast flag (growth concern, informational)
    CPI YoY   → computed from CPIAUCSL level data (latest / 12m-ago − 1) × 100
    """

    def __init__(self):
        self._last_extreme_fear_date: str | None = None

    def _scan_sync(self) -> list[str]:
        """
        Fetches all 5 series, updates MACRO_SNAPSHOT, returns a list of event
        strings for the loop to route (e.g. ['vix_extreme_fear']).
        """
        # Fetch with 0.5s between requests — polite to FRED's public servers
        fed_series  = _fetch_csv(_SERIES_URLS["fed_funds"]); time.sleep(0.5)
        vix_series  = _fetch_csv(_SERIES_URLS["vix"]);       time.sleep(0.5)
        t10_series  = _fetch_csv(_SERIES_URLS["treasury"]);  time.sleep(0.5)
        ur_series   = _fetch_csv(_SERIES_URLS["unemp"]);     time.sleep(0.5)
        cpi_series  = _fetch_csv(_SERIES_URLS["cpi"])

        ff_latest  = _latest(fed_series)
        ff_prev    = _value_n_months_ago(fed_series, 1)
        vix_latest = _latest(vix_series)
        t10_latest = _latest(t10_series)
        t10_30d    = _value_n_days_ago(t10_series, 30)
        ur_latest  = _latest(ur_series)

        # CPI YoY from index levels
        cpi_yoy = None
        if len(cpi_series) >= 13:
            cpi_now = cpi_series[-1][1]
            cpi_12m = _value_n_months_ago(cpi_series, 12)
            if cpi_12m and cpi_12m > 0:
                cpi_yoy = round((cpi_now / cpi_12m - 1) * 100, 2)

        # Compute flags
        fed_cut      = ff_latest is not None and ff_prev is not None and ff_latest < ff_prev
        yield_rising = t10_latest is not None and t10_30d is not None and (t10_latest - t10_30d) > 0.2
        vix_extreme  = vix_latest is not None and vix_latest > 40

        # Extreme fear alert: fire only once per calendar day
        events: list[str] = []
        today = datetime.now(pytz.utc).date().isoformat()
        if vix_extreme and self._last_extreme_fear_date != today:
            self._last_extreme_fear_date = today
            events.append("vix_extreme_fear")

        # Update snapshot in place (module-level dict — shared across all loops)
        MACRO_SNAPSHOT["fed_funds_rate"]    = ff_latest
        MACRO_SNAPSHOT["vix"]               = vix_latest
        MACRO_SNAPSHOT["treasury_10y"]      = t10_latest
        MACRO_SNAPSHOT["unemployment"]      = ur_latest
        MACRO_SNAPSHOT["cpi_yoy"]           = cpi_yoy
        MACRO_SNAPSHOT["fed_rate_cut"]      = fed_cut
        MACRO_SNAPSHOT["yield_rising_fast"] = yield_rising
        MACRO_SNAPSHOT["vix_extreme_fear"]  = vix_extreme
        MACRO_SNAPSHOT["last_updated"]      = datetime.now(pytz.utc).isoformat()

        def _f(v, d=2):
            return f"{v:.{d}f}" if v is not None else "N/A"

        print(
            f"[FRED] Snapshot — FF:{_f(ff_latest)}% "
            f"VIX:{_f(vix_latest,1)} "
            f"10Y:{_f(t10_latest)}% "
            f"UR:{_f(ur_latest)}% "
            f"CPI:{_f(cpi_yoy)}%yoy"
            + (" [RATE CUT]"     if fed_cut      else "")
            + (" [YIELD RISING]" if yield_rising  else "")
            + (" [EXTREME FEAR]" if vix_extreme   else "")
        )
        return events

    async def scan_once(self) -> list[str]:
        return await asyncio.to_thread(self._scan_sync)
