"""
Short-interest small/mid-cap universe — a SEPARATE symbol universe from the
top-250-by-dollar-volume list the rest of the swing/discovery pipeline uses.

Why a separate universe
------------------------
The short-interest-momentum family's squeeze/continuation edge is documented
in small/mid-cap names (Asquith/Pathak/Ritter 2005; Boehmer/Jones/Zhang 2008),
not the megacap-heavy top-250-by-volume universe the rest of the bot screens
(which is ranked by dollar volume and dominated by large caps). This module
builds a dedicated candidate list: symbols with FINRA short-interest data
available AND a Finnhub market cap between
Config.SHORT_INTEREST_UNIVERSE_MIN_MARKET_CAP and
Config.SHORT_INTEREST_UNIVERSE_MAX_MARKET_CAP ($500M-$10B by default).

Cost / candidate-bounding
--------------------------
FINRA's short-interest feed covers ~22,000 symbols on a single settlement
date — far too many to market-cap-check via Finnhub on a free-tier rate limit.
This module first narrows that to a liquid, exchange-listed candidate pool
using data already sitting in ``short_interest_levels`` (avg_daily_volume,
free from the same FINRA response — NOT the flawed daily short-volume RATIO
from finra_historical.py, a different and unrelated figure), then checks
market cap (Finnhub /stock/profile2, ~1 req/sec) for at most
Config.SHORT_INTEREST_UNIVERSE_MAX_CANDIDATES symbols, bounding the run to a
few minutes rather than hours. The same profile2 call backfills
``float_shares`` (shares outstanding, used as a float proxy — Finnhub's free
tier does not expose free-float specifically) into short_interest_levels so a
second round-trip per symbol isn't needed. A symbol is only admitted when
Finnhub's ``finnhubIndustry`` is non-empty — a cheap proxy for "this is an
operating company", since Finnhub does not GICS-classify ETFs/funds, and
FINRA's feed mixes leveraged/buffer ETFs in heavily.

Storage: PostgreSQL ``short_interest_universe`` (symbol, market_cap, updated_at).
"""
from __future__ import annotations

import time
import traceback

from sqlalchemy import bindparam, text as sql_text

from config import Config
from discovery.data_feeds.finra_short_interest_levels import (
    TABLE as _LEVELS_TABLE,
    _get_engine,
)

TABLE = "short_interest_universe"


def _ensure_table(db_engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                symbol     VARCHAR(10) PRIMARY KEY,
                market_cap BIGINT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))


def _candidate_symbols(db_engine, max_candidates: int, min_avg_volume: int) -> list[str]:
    """Most-liquid symbols (by FINRA avg_daily_volume) from the latest synced
    settlement report, bounded to max_candidates -- these are what get checked
    against Finnhub for market cap."""
    try:
        with db_engine.connect() as conn:
            latest = conn.execute(sql_text(f"SELECT MAX(report_date) FROM {_LEVELS_TABLE}")).scalar()
            if latest is None:
                return []
            rows = conn.execute(sql_text(f"""
                SELECT symbol FROM {_LEVELS_TABLE}
                WHERE report_date = :latest
                  AND avg_daily_volume >= :min_vol
                  AND days_to_cover IS NOT NULL
                ORDER BY avg_daily_volume DESC
                LIMIT :n
            """), {"latest": latest, "min_vol": min_avg_volume, "n": max_candidates}).fetchall()
        return [r[0] for r in rows]
    except Exception:
        print(f"[SIUniverse] candidate query failed:\n{traceback.format_exc()}")
        return []


def _fetch_market_cap_and_shares(symbol: str) -> tuple[float | None, float | None, bool]:
    """
    Returns (market_cap_usd, shares_outstanding, is_probably_equity) via
    Finnhub /stock/profile2. shares_outstanding is used as a float-shares
    proxy. is_probably_equity is False when finnhubIndustry is empty (funds/
    ETFs are not GICS-classified by Finnhub).
    """
    api_key = Config.FINNHUB_API_KEY
    if not api_key:
        return None, None, True
    try:
        import requests
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/profile2",
            params={"symbol": symbol, "token": api_key},
            timeout=10,
        )
        if resp.status_code != 200:
            return None, None, True
        data = resp.json() or {}
        mkt_cap = data.get("marketCapitalization")
        shares = data.get("shareOutstanding")
        industry = (data.get("finnhubIndustry") or "").strip()
        mkt_cap_usd = float(mkt_cap) * 1_000_000 if mkt_cap else None
        shares_count = float(shares) * 1_000_000 if shares else None
        return mkt_cap_usd, shares_count, bool(industry)
    except Exception:
        print(f"[SIUniverse] {symbol}: Finnhub profile2 fetch failed:\n{traceback.format_exc()}")
        return None, None, True


def refresh_short_interest_universe(
    db_engine=None,
    max_candidates: int | None = None,
    min_avg_volume: int | None = None,
) -> int:
    """
    Rebuild short_interest_universe: symbols with FINRA short-interest data
    available AND Finnhub market cap in [MIN_MARKET_CAP, MAX_MARKET_CAP].
    Also backfills float_shares into short_interest_levels for every candidate
    checked (same Finnhub call). Bounded to max_candidates Finnhub calls — see
    module docstring. Fail-open throughout; returns the number of symbols
    admitted to the universe.
    """
    engine, owns_engine = _get_engine(db_engine)
    if engine is None:
        print("[SIUniverse] No DATABASE_URL — skipping universe refresh")
        return 0

    max_candidates = max_candidates or Config.SHORT_INTEREST_UNIVERSE_MAX_CANDIDATES
    min_avg_volume = min_avg_volume or Config.SHORT_INTEREST_UNIVERSE_MIN_AVG_VOLUME
    min_cap = Config.SHORT_INTEREST_UNIVERSE_MIN_MARKET_CAP
    max_cap = Config.SHORT_INTEREST_UNIVERSE_MAX_MARKET_CAP

    try:
        _ensure_table(engine)
        candidates = _candidate_symbols(engine, max_candidates, min_avg_volume)
        if not candidates:
            print("[SIUniverse] No candidate symbols (short_interest_levels empty?) — skipping")
            return 0

        with engine.connect() as conn:
            latest = conn.execute(sql_text(f"SELECT MAX(report_date) FROM {_LEVELS_TABLE}")).scalar()

        print(f"[SIUniverse] Checking market cap for {len(candidates)} candidate(s)...")
        admitted: list[tuple[str, float]] = []
        for i, symbol in enumerate(candidates):
            mkt_cap, shares, is_equity = _fetch_market_cap_and_shares(symbol)

            if shares and latest is not None:
                try:
                    with engine.begin() as conn:
                        conn.execute(sql_text(f"""
                            UPDATE {_LEVELS_TABLE} SET float_shares = :shares
                            WHERE symbol = :symbol AND report_date = :latest
                        """), {"shares": int(shares), "symbol": symbol, "latest": latest})
                except Exception:
                    print(f"[SIUniverse] {symbol}: float_shares backfill failed:\n{traceback.format_exc()}")

            if is_equity and mkt_cap is not None and min_cap <= mkt_cap <= max_cap:
                admitted.append((symbol, mkt_cap))

            if (i + 1) % 25 == 0:
                print(f"[SIUniverse] Checked {i + 1}/{len(candidates)} — {len(admitted)} admitted so far")
            time.sleep(1.0)  # Finnhub free-tier courtesy pacing

        with engine.begin() as conn:
            for symbol, mkt_cap in admitted:
                conn.execute(sql_text(f"""
                    INSERT INTO {TABLE} (symbol, market_cap, updated_at)
                    VALUES (:symbol, :market_cap, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        market_cap = EXCLUDED.market_cap,
                        updated_at = NOW()
                """), {"symbol": symbol, "market_cap": int(mkt_cap)})

            # Drop members that were checked this run but no longer qualify
            # (grew past max_cap, shrank below min_cap, or turned out to be a fund).
            admitted_syms = [s for s, _ in admitted]
            if admitted_syms:
                stmt = sql_text(
                    f"DELETE FROM {TABLE} WHERE symbol IN :checked AND symbol NOT IN :keep"
                ).bindparams(bindparam("checked", expanding=True), bindparam("keep", expanding=True))
                conn.execute(stmt, {"checked": candidates, "keep": admitted_syms})
            else:
                stmt = sql_text(
                    f"DELETE FROM {TABLE} WHERE symbol IN :checked"
                ).bindparams(bindparam("checked", expanding=True))
                conn.execute(stmt, {"checked": candidates})

        print(
            f"[SIUniverse] Universe refreshed — {len(admitted)}/{len(candidates)} symbols "
            f"in ${min_cap/1e6:.0f}M-${max_cap/1e6:.0f}M market-cap band"
        )
        return len(admitted)
    finally:
        if owns_engine:
            engine.dispose()


def get_short_interest_universe(db_engine=None) -> list[str]:
    engine, owns_engine = _get_engine(db_engine)
    if engine is None:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text(f"SELECT symbol FROM {TABLE} ORDER BY symbol")).fetchall()
        return [r[0] for r in rows]
    except Exception:
        print(f"[SIUniverse] get_short_interest_universe failed:\n{traceback.format_exc()}")
        return []
    finally:
        if owns_engine:
            engine.dispose()
