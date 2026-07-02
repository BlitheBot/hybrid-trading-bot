"""
Historical + live FINRA consolidated short interest — the actual open short
position LEVEL data, replacing the daily short-VOLUME ratio
(``finra_historical.py``) as the primary signal for the short-interest-
momentum discovery family. An audit found the daily ratio measures order
flow (ShortVolume / TotalVolume on a given day), not open short positions,
and does not match the anomaly documented in the literature (Asquith/
Pathak/Ritter 2005; Boehmer/Jones/Zhang 2008; Rapach/Ringgenberg/Zhou 2016) —
which is measured on short interest as a fraction of float, and days-to-cover.

Data source
-----------
FINRA's public Query API, dataset otcMarket/consolidatedShortInterest
(``https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest``,
POST + JSON body, no auth required). One record per (symbol, settlementDate)
with the actual open short position (``currentShortPositionQuantity``), the
prior period's position (``previousShortPositionQuantity``), FINRA's own
``daysToCoverQuantity``, and ``averageDailyVolumeQuantity``. Verified live
against the API (schema fields confirmed by direct query) rather than assumed
from documentation.

Settlement dates
-----------------
FINRA settles short interest twice a month: the 15th (or the preceding
business day) and the last business day of the month, published roughly 1-2
weeks later. ``_candidate_settlement_dates`` generates weekend-adjusted
candidates; a date with no data yet (not published) is simply skipped by
``sync_short_interest_levels`` rather than treated as an error.

Storage: PostgreSQL ``short_interest_levels`` (symbol, report_date,
short_interest, float_shares, days_to_cover, avg_daily_volume). ``float_shares``
is NOT in FINRA's feed — it is backfilled separately (Finnhub /stock/profile2
shares-outstanding, used as a float proxy) by
``discovery/short_interest_universe.py``, which needs the same Finnhub call
for its market-cap filter anyway. ``avg_daily_volume`` IS free from this same
FINRA response (a plain liquidity figure, NOT the flawed daily short-volume
RATIO from finra_historical.py) and is kept because the universe builder needs
a liquidity floor to bound how many symbols it checks against Finnhub.

COST / FAIL-OPEN CONTRACT
--------------------------
Each settlement date is a bulk fetch covering every symbol (~22k rows across
~5 paginated pages of up to 5000 rows) — cheap relative to the per-symbol Form
4 fetches in edgar_historical.py. ``sync_short_interest_levels`` backfills up
to ``lookback_periods`` (default 48 ~= 2 years) settlement dates on first run;
dates already present in the table are skipped on every subsequent call, so
routine (e.g. weekly) syncs only fetch newly-published dates. All network/
parse failures are caught and logged with a full traceback; a failed or
not-yet-published date is skipped, never raises to the caller.
"""
from __future__ import annotations

import csv
import io
import json
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text as sql_text

from config import Config

TABLE = "short_interest_levels"

_API_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
_HEADERS = {"Content-Type": "application/json", "Accept": "text/plain"}
_PAGE_SIZE = 5000
_REQUEST_TIMEOUT = 30
_PAGE_SLEEP = 0.2

# FINRA's sentinel for daysToCoverQuantity when average daily volume is ~0
# (effectively undefined) — treated as "no usable liquidity data", not a
# genuine multi-hundred-day cover figure.
_DAYS_TO_COVER_SENTINEL = 900.0

# Excluded from the tradeable universe: OTC/pink-sheet and foreign-ordinary
# listings, not exchange-listed US common stock.
_EXCLUDED_MARKET_CLASSES = {"OTC"}

SI_LEVEL_PCT_COLUMN = "si_pct_of_float"
DAYS_TO_COVER_COLUMN = "days_to_cover"
SI_LEVEL_RISING_COLUMN = "si_level_rising"


# ── Engine helper ────────────────────────────────────────────────────────────

def _get_engine(db_engine=None):
    """Return (engine, owns_engine). Reuses a passed-in engine, or builds a
    short-lived one from Config.DATABASE_URL that the caller must dispose."""
    if db_engine is not None:
        return db_engine, False
    if not Config.DATABASE_URL:
        return None, False
    return create_engine(Config.DATABASE_URL, pool_pre_ping=True), True


def _ensure_table(db_engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                symbol           VARCHAR(10)  NOT NULL,
                report_date      DATE         NOT NULL,
                short_interest   BIGINT,
                float_shares     BIGINT,
                days_to_cover    FLOAT,
                avg_daily_volume BIGINT,
                updated_at       TIMESTAMPTZ  DEFAULT NOW(),
                PRIMARY KEY (symbol, report_date)
            )
        """))
        conn.execute(sql_text(
            f"CREATE INDEX IF NOT EXISTS {TABLE}_symbol_idx ON {TABLE} (symbol, report_date)"
        ))


# ── FINRA fetch ──────────────────────────────────────────────────────────────

def _post(body: dict) -> str | None:
    """POST to the FINRA Query API; returns the raw text/plain CSV body, or None on failure."""
    try:
        req = urllib.request.Request(
            _API_URL, data=json.dumps(body).encode("utf-8"),
            headers=_HEADERS, method="POST",
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            detail = str(e)
        print(f"[FINRA-SI] POST failed: HTTP {e.code} — {detail}")
        return None
    except Exception:
        print(f"[FINRA-SI] POST failed:\n{traceback.format_exc()}")
        return None


def _parse_csv(text: str) -> list[dict]:
    if not text or not text.strip():
        return []
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def _fetch_settlement_date(settlement_date: str, symbol: str | None = None) -> list[dict]:
    """Fetch every FINRA consolidated-short-interest row for one settlement date, paginated."""
    compare_filters = [
        {"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": settlement_date}
    ]
    if symbol:
        compare_filters.append({"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": symbol})

    all_rows: list[dict] = []
    offset = 0
    while True:
        text = _post({"limit": _PAGE_SIZE, "offset": offset, "compareFilters": compare_filters})
        if text is None:
            break
        page = _parse_csv(text)
        if not page:
            break
        all_rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
        time.sleep(_PAGE_SLEEP)
    return all_rows


def _candidate_settlement_dates(n: int) -> list[str]:
    """Last ``n`` FINRA settlement dates (mid-month + month-end), most recent first.

    Weekend-adjusted (Sat/Sun -> preceding Friday) but not holiday-aware;
    a 1-2 day miss just means that specific date returns zero rows and is
    skipped by the caller, not a wrong result.
    """
    def _prev_business_day(d: date) -> date:
        while d.weekday() >= 5:  # Sat=5, Sun=6
            d -= timedelta(days=1)
        return d

    def _month_end(year: int, month: int) -> date:
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return nxt - timedelta(days=1)

    today = date.today()
    out: list[date] = []
    year, month = today.year, today.month
    while len(out) < n * 2:
        for d in (_prev_business_day(_month_end(year, month)), _prev_business_day(date(year, month, 15))):
            if d <= today:
                out.append(d)
        month -= 1
        if month == 0:
            month, year = 12, year - 1

    out = sorted(set(out), reverse=True)
    return [d.isoformat() for d in out[:n]]


def _already_synced_dates(db_engine) -> set[str]:
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(sql_text(f"SELECT DISTINCT report_date FROM {TABLE}")).fetchall()
        return {r[0].isoformat() for r in rows}
    except Exception:
        return set()


def sync_short_interest_levels(db_engine=None, lookback_periods: int | None = None) -> int:
    """
    Backfill/refresh ``short_interest_levels`` from FINRA's public consolidated-
    short-interest API. Idempotent: settlement dates already present are
    skipped, so repeated (e.g. weekly) calls only fetch newly-published dates.
    Returns the number of (symbol, date) rows written. Fail-open: a date that
    errors or has no data yet is skipped; never raises.
    """
    engine, owns_engine = _get_engine(db_engine)
    if engine is None:
        print("[FINRA-SI] No DATABASE_URL — skipping short_interest_levels sync")
        return 0

    lookback_periods = lookback_periods or Config.SHORT_INTEREST_LEVELS_LOOKBACK_PERIODS
    try:
        _ensure_table(engine)
        already = _already_synced_dates(engine)
        candidates = _candidate_settlement_dates(lookback_periods)
        pending = [d for d in candidates if d not in already]

        if not pending:
            print(f"[FINRA-SI] short_interest_levels already covers the last {lookback_periods} settlement periods")
            return 0

        print(f"[FINRA-SI] Syncing {len(pending)} settlement date(s): {pending}")
        total_written = 0
        for settlement_date in pending:
            try:
                rows = _fetch_settlement_date(settlement_date)
            except Exception:
                print(f"[FINRA-SI] {settlement_date}: fetch raised (non-fatal):\n{traceback.format_exc()}")
                continue
            if not rows:
                print(f"[FINRA-SI] {settlement_date}: no data yet (not published) — skipping")
                continue

            records = []
            for r in rows:
                try:
                    sym = (r.get("symbolCode") or "").strip()
                    mkt_class = (r.get("marketClassCode") or "").strip()
                    if not sym or mkt_class in _EXCLUDED_MARKET_CLASSES:
                        continue
                    si = int(float(r.get("currentShortPositionQuantity") or 0))
                    adv_raw = r.get("averageDailyVolumeQuantity")
                    adv = int(float(adv_raw)) if adv_raw not in (None, "") else None
                    dtc_raw = r.get("daysToCoverQuantity")
                    dtc = float(dtc_raw) if dtc_raw not in (None, "") else None
                    if dtc is not None and dtc >= _DAYS_TO_COVER_SENTINEL:
                        dtc = None  # FINRA's "undefined" sentinel, not a real figure
                    records.append({
                        "symbol": sym, "report_date": settlement_date,
                        "short_interest": si, "days_to_cover": dtc, "avg_daily_volume": adv,
                    })
                except (ValueError, TypeError):
                    continue

            if not records:
                continue

            with engine.begin() as conn:
                for rec in records:
                    conn.execute(sql_text(f"""
                        INSERT INTO {TABLE}
                            (symbol, report_date, short_interest, days_to_cover, avg_daily_volume, updated_at)
                        VALUES (:symbol, :report_date, :short_interest, :days_to_cover, :avg_daily_volume, NOW())
                        ON CONFLICT (symbol, report_date) DO UPDATE SET
                            short_interest   = EXCLUDED.short_interest,
                            days_to_cover    = EXCLUDED.days_to_cover,
                            avg_daily_volume = EXCLUDED.avg_daily_volume,
                            updated_at       = NOW()
                    """), rec)
            total_written += len(records)
            print(f"[FINRA-SI] {settlement_date}: stored {len(records)} symbols")

        return total_written
    finally:
        if owns_engine:
            engine.dispose()


# ── Enrichment (consumed by ShortInterestMomentumPositionStrategy) ──────────

def get_symbol_history(symbol: str, db_engine=None) -> pd.DataFrame:
    """This symbol's full short_interest_levels history, sorted ascending by report_date."""
    engine, owns_engine = _get_engine(db_engine)
    empty = pd.DataFrame(columns=["report_date", "short_interest", "float_shares", "days_to_cover"])
    if engine is None:
        return empty
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text(f"""
                SELECT report_date, short_interest, float_shares, days_to_cover
                FROM {TABLE}
                WHERE symbol = :symbol
                ORDER BY report_date ASC
            """), {"symbol": symbol}).mappings().fetchall()
        return pd.DataFrame([dict(r) for r in rows]) if rows else empty
    except Exception:
        print(f"[FINRA-SI] history read failed for {symbol}:\n{traceback.format_exc()}")
        return empty
    finally:
        if owns_engine:
            engine.dispose()


def attach_short_interest_level(bars: pd.DataFrame, symbol: str, db_engine=None) -> pd.DataFrame:
    """
    Return a copy of ``bars`` with three per-bar columns derived from the real
    FINRA short-interest LEVEL history (not the daily volume ratio):

      * ``si_pct_of_float`` — short_interest / float_shares (NaN if float_shares
        unknown; the squeeze-LONG precondition also accepts days_to_cover alone
        so this being unavailable does not silently disable the family).
      * ``days_to_cover``   — FINRA's own days-to-cover figure, as of the most
        recently published settlement report.
      * ``si_level_rising`` — +1.0 if short_interest increased vs the *prior*
        settlement report, -1.0 if it decreased, 0.0 if flat/unknown. This is
        "this period vs last period" from consecutive stored rows, not a
        single-row lookback.

    All three are forward-filled between report dates — short interest is a
    snapshot state that persists until the next report, unlike a single-day
    event (contrast with edgar_historical.attach_insider_buy_value). Fail-open:
    no history -> all-NaN/0.0 columns, family stays all-flat.
    """
    out = bars.copy()
    n = len(out)
    out[SI_LEVEL_PCT_COLUMN] = np.nan
    out[DAYS_TO_COVER_COLUMN] = np.nan
    out[SI_LEVEL_RISING_COLUMN] = 0.0

    if n == 0 or not isinstance(out.index, pd.DatetimeIndex):
        return out

    hist = get_symbol_history(symbol, db_engine=db_engine)
    if hist.empty:
        return out

    hist = hist.sort_values("report_date").reset_index(drop=True)
    hist["report_date"] = pd.to_datetime(hist["report_date"])
    hist["si_pct_of_float"] = hist["short_interest"] / hist["float_shares"].replace(0, np.nan)
    hist["si_level_rising"] = np.sign(hist["short_interest"].diff().fillna(0.0))

    report_series = pd.DataFrame(
        {
            SI_LEVEL_PCT_COLUMN: hist["si_pct_of_float"].to_numpy(),
            DAYS_TO_COVER_COLUMN: hist["days_to_cover"].to_numpy(),
            SI_LEVEL_RISING_COLUMN: hist["si_level_rising"].to_numpy(),
        },
        index=pd.DatetimeIndex(hist["report_date"]),
    )

    bar_dates = pd.DatetimeIndex(out.index).normalize()
    unioned = report_series.reindex(bar_dates.union(report_series.index)).sort_index().ffill()
    aligned = unioned.reindex(bar_dates)

    out[SI_LEVEL_PCT_COLUMN] = aligned[SI_LEVEL_PCT_COLUMN].to_numpy()
    out[DAYS_TO_COVER_COLUMN] = aligned[DAYS_TO_COVER_COLUMN].to_numpy()
    out[SI_LEVEL_RISING_COLUMN] = aligned[SI_LEVEL_RISING_COLUMN].fillna(0.0).to_numpy()
    return out


if __name__ == "__main__":
    # Offline smoke test of the settlement-date math + enrichment alignment
    # (no network). Live fetch/store is exercised by running
    # sync_short_interest_levels() against a real DATABASE_URL.
    dates = _candidate_settlement_dates(6)
    print("Recent candidate settlement dates:", dates)
    assert len(dates) == 6
    assert all(date.fromisoformat(d) <= date.today() for d in dates)

    hist = pd.DataFrame({
        "report_date": pd.to_datetime(["2026-05-15", "2026-05-29", "2026-06-15"]),
        "short_interest": [1_000_000, 1_500_000, 1_200_000],
        "float_shares": [10_000_000, 10_000_000, 10_000_000],
        "days_to_cover": [2.0, 3.5, 2.8],
    })
    globals()["get_symbol_history"] = lambda *a, **k: hist
    idx = pd.date_range("2026-05-10", "2026-06-20", freq="B")
    bars = pd.DataFrame({"close": np.linspace(100, 110, len(idx))}, index=idx)
    enriched = attach_short_interest_level(bars, "TEST")
    assert SI_LEVEL_PCT_COLUMN in enriched.columns
    # After 2026-05-29 and before 2026-06-15, si should reflect the 05-29 report (rising).
    mid_window = enriched.loc["2026-06-01":"2026-06-12"]
    assert (mid_window[SI_LEVEL_RISING_COLUMN] == 1.0).all(), mid_window[SI_LEVEL_RISING_COLUMN]
    assert abs(mid_window[SI_LEVEL_PCT_COLUMN].iloc[0] - 0.15) < 1e-9
    print("\nAll finra_short_interest_levels smoke tests passed.")
