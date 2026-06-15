"""
Indicator Discovery Scheduler — orchestrates overnight GP runs across swing symbols.

Entry point when invoked as a subprocess by bot.py's indicator_discovery_loop():
    python -m discovery.discovery_scheduler

Reads bar data from the parquet cache in discovery/data/ that is populated by
discovery_engine_v2's Friday run. If a symbol's cache is missing, it is skipped
with a log message (run the v2 engine first to populate caches).

Regime detection via HurstSignal on the last 60 bars:
  H > 0.6  → 'trending'
  H < 0.4  → 'mean_reverting'
  else     → 'any'
"""
import asyncio
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz
import requests
from sqlalchemy import create_engine

from config import Config
from .genetic_engine import GeneticEngine
from .symbol_universe import get_discovery_candidates

_DATA_DIR = Path(__file__).parent / "data"
# discovery_engine_v2 also uses Path(__file__).parent / "data" — same directory, paths match.


class DiscoveryScheduler:
    def _slack(self, msg: str) -> None:
        webhook = Config.SLACK_DECISIONS_WEBHOOK
        if not webhook:
            return
        try:
            requests.post(webhook, json={"text": msg}, timeout=10)
        except Exception as e:
            print(f"[IndicatorDiscovery] Slack error: {e}")

    def _load_bars(self, symbol: str) -> pd.DataFrame:
        """Read the last 252 bars from the parquet cache populated by discovery_engine_v2."""
        cache_path = _DATA_DIR / f"{symbol}.parquet"
        if not cache_path.exists():
            return pd.DataFrame()
        try:
            import pyarrow.parquet as pq
            df = pq.read_table(str(cache_path)).to_pandas()
            return df.tail(252) if len(df) > 252 else df
        except Exception as e:
            print(f"[IndicatorDiscovery] {symbol}: cache read failed — {e}")
            return pd.DataFrame()

    def _detect_regime(self, bars_df: pd.DataFrame) -> str:
        """Return 'trending', 'mean_reverting', or 'any' via Hurst exponent."""
        if bars_df.empty or len(bars_df) < 60:
            return "any"
        try:
            from strategies.hurst_signal import HurstSignal
            hs     = HurstSignal()
            result = hs.compute_latest(bars_df["close"].tail(60))
            code   = result.get("regime_code", 0)
            if hasattr(code, "iloc"):  # handle Series vs scalar
                code = int(code.iloc[-1]) if len(code) > 0 else 0
            if code == 1:
                return "trending"
            if code == -1:
                return "mean_reverting"
            return "any"
        except Exception:
            return "any"

    async def run_overnight(self, db_engine) -> None:
        est = pytz.timezone("America/New_York")
        now = datetime.now(est)
        print(
            f"[IndicatorDiscovery] Starting overnight run at "
            f"{now.strftime('%Y-%m-%d %H:%M %Z')}"
        )

        symbols = get_discovery_candidates(db_engine, top_n=250)
        if not symbols:
            # symbol_universe is populated Sunday midnight; fall back to last-known
            # active_tickers (no recency filter) rather than the tiny SWING_SYMBOLS list.
            from .ticker_prioritizer import get_active_tickers
            symbols = get_active_tickers(db_engine)[:250]
            if symbols:
                print(
                    f"[IndicatorDiscovery] symbol_universe empty — using last-populated "
                    f"active_tickers ({len(symbols)} symbols)"
                )
            else:
                symbols = list(Config.SWING_SYMBOLS)
                print(
                    f"[IndicatorDiscovery] Both symbol_universe and active_tickers empty — "
                    f"falling back to SWING_SYMBOLS ({len(symbols)} symbols)"
                )
        else:
            print(f"[IndicatorDiscovery] {len(symbols)} symbols from symbol_universe")

        # Pre-flight: count how many candidate symbols have parquet bars cached by
        # discovery_engine_v2. Log clearly so the log makes it obvious when v2 hasn't
        # run yet rather than silently skipping most symbols.
        cached = [s for s in symbols if (_DATA_DIR / f"{s}.parquet").exists()]
        n_cached, n_total = len(cached), len(symbols)
        if n_cached == 0:
            print(
                f"[IndicatorDiscovery] WARNING: 0 of {n_total} candidate symbols have "
                f"cached bar files in discovery/data/. discovery_engine_v2 must complete "
                f"its Friday run first — parquet files are written there. "
                f"All symbols will be skipped this run."
            )
            self._slack(
                f":warning: Indicator Discovery skipped — no parquet bar cache found. "
                f"discovery_engine_v2 must run first (Friday) to populate discovery/data/. "
                f"Check Railway logs."
            )
        else:
            print(
                f"[IndicatorDiscovery] {n_cached}/{n_total} symbols have cached bars "
                f"— {n_total - n_cached} will be skipped (no parquet cache from v2)"
            )

        self._slack(
            f":dna: Indicator Discovery Engine starting overnight run — "
            f"{n_cached}/{n_total} symbols have cached bars | population=100 | 30 generations each."
        )

        # Task 7: 100-tree population, 30 generations, 20% mutation, tournament
        # selection, 70/30 train/validation split with an out-of-sample graduation
        # gate (validation IC > 0.05 AND p < 0.01).
        engine = GeneticEngine(
            population_size=100,
            n_generations=30,
            mutation_rate=0.2,
            crossover_rate=0.5,
            max_tree_depth=4,
            train_frac=0.7,
            tournament_size=3,
        )

        total_graduated = 0
        symbol_summaries: list[str] = []

        for symbol in symbols:
            bars_df = self._load_bars(symbol)
            if bars_df.empty:
                print(
                    f"[IndicatorDiscovery] {symbol}: no cached bars — skipping "
                    f"(discovery_engine_v2 must write {symbol}.parquet to discovery/data/ first)"
                )
                continue

            regime = self._detect_regime(bars_df)
            print(
                f"[IndicatorDiscovery] {symbol}: {len(bars_df)} bars | regime={regime}"
            )

            try:
                graduated = await asyncio.to_thread(
                    engine.run, bars_df, symbol, regime, db_engine
                )
                n_grad   = len(graduated)
                total_graduated += n_grad
                best_ic  = max((g["mean_ic"] for g in graduated), default=0.0)
                summary  = (
                    f"{symbol}: {n_grad} graduated | best_ic={best_ic:.3f} | regime={regime}"
                )
                print(f"[IndicatorDiscovery] {summary}")
                symbol_summaries.append(summary)
            except Exception as e:
                msg = f"{symbol}: engine error — {e}"
                print(f"[IndicatorDiscovery] {msg}")
                symbol_summaries.append(msg)

        slack_body = "\n".join([
            f":white_check_mark: Indicator Discovery complete — "
            f"{total_graduated} indicators graduated across {len(symbols)} symbols",
            *symbol_summaries,
        ])
        print(f"\n[IndicatorDiscovery] Run complete. {total_graduated} indicators graduated.")
        self._slack(slack_body)


if __name__ == "__main__":
    import sys

    if not Config.DATABASE_URL:
        print("[IndicatorDiscovery] No DATABASE_URL configured — exiting")
        sys.exit(0)

    _engine = create_engine(Config.DATABASE_URL, pool_pre_ping=True)
    try:
        asyncio.run(DiscoveryScheduler().run_overnight(_engine))
    finally:
        _engine.dispose()
