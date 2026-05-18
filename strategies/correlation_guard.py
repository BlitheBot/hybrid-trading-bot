import time
import numpy as np
import pandas as pd


class CorrelationGuard:
    SECTOR_MAP = {
        "JPM":   "Financials",
        "V":     "Financials",
        "BRK.B": "Financials",
        "COST":  "Consumer/Defensive",
        "PG":    "Consumer/Defensive",
        "SPY":   "Broad market",
    }
    _CACHE_TTL = 30 * 60  # seconds

    def __init__(
        self,
        price_lookback_days: int = 60,
        max_portfolio_correlation: float = 0.7,
        max_correlated_positions: int = 2,
        correlation_threshold: float = 0.75,
    ):
        self.price_lookback_days = price_lookback_days
        self.max_portfolio_correlation = max_portfolio_correlation
        self.max_correlated_positions = max_correlated_positions
        self.correlation_threshold = correlation_threshold
        self._cache: dict = {}
        self._cache_time: float = 0.0

    def _cache_valid(self) -> bool:
        return (time.monotonic() - self._cache_time) < self._CACHE_TTL

    def _fetch(self, symbol: str, bar_fetcher) -> "pd.Series | None":
        if self._cache_valid() and symbol in self._cache:
            return self._cache[symbol]
        try:
            df = bar_fetcher(symbol)
        except Exception as exc:
            print(f"[CorrelationGuard] bar_fetcher error for {symbol}: {exc}")
            return None
        if df is None or df.empty or "close" not in df.columns:
            print(f"[CorrelationGuard] No price data for {symbol} — skipping")
            return None
        series = df["close"].dropna()
        if len(series) < 20:
            print(f"[CorrelationGuard] Insufficient bars for {symbol} ({len(series)}) — skipping")
            return None
        self._cache[symbol] = series
        return series

    def _prime_cache(self, symbols: list, bar_fetcher) -> None:
        if not self._cache_valid():
            self._cache = {}
            self._cache_time = time.monotonic()
        for sym in symbols:
            if sym not in self._cache:
                self._fetch(sym, bar_fetcher)

    @staticmethod
    def _pearson(s1: pd.Series, s2: pd.Series) -> "float | None":
        common = s1.index.intersection(s2.index)
        if len(common) < 20:
            return None
        a = s1.loc[common].to_numpy(dtype=float)
        b = s2.loc[common].to_numpy(dtype=float)
        if np.std(a) == 0 or np.std(b) == 0:
            return None
        return float(np.corrcoef(a, b)[0, 1])

    def _sector_block(self, incoming: str, open_symbols: list) -> "tuple[bool, str]":
        incoming_sector = self.SECTOR_MAP.get(incoming)
        if incoming_sector is None:
            return False, ""
        same = [s for s in open_symbols if self.SECTOR_MAP.get(s) == incoming_sector]
        if len(same) >= 2:
            return True, (
                f"Sector concentration: already {len(same)} "
                f"{incoming_sector} open ({', '.join(same)})"
            )
        return False, ""

    def check(self, incoming_symbol: str, open_position_symbols: list, bar_fetcher) -> dict:
        def _allow(reason="", corr_map=None, avg=0.0):
            return {"allowed": True, "reason": reason,
                    "correlation_map": corr_map or {}, "avg_correlation": avg}

        def _block(reason, corr_map=None, avg=0.0):
            return {"allowed": False, "reason": reason,
                    "correlation_map": corr_map or {}, "avg_correlation": avg}

        if len(open_position_symbols) < 2:
            return _allow()

        blocked, reason = self._sector_block(incoming_symbol, open_position_symbols)
        if blocked:
            return _block(reason)

        all_syms = list({incoming_symbol} | set(open_position_symbols))
        self._prime_cache(all_syms, bar_fetcher)

        incoming_prices = self._cache.get(incoming_symbol)
        if incoming_prices is None:
            print(f"[CorrelationGuard] {incoming_symbol} prices unavailable — failing open")
            return _allow("price data unavailable — failing open")

        corr_map: dict = {}
        skipped: list = []
        for sym in open_position_symbols:
            sym_prices = self._cache.get(sym)
            if sym_prices is None:
                skipped.append(sym)
                continue
            r = self._pearson(incoming_prices, sym_prices)
            if r is not None:
                corr_map[sym] = round(r, 3)
            else:
                skipped.append(sym)

        if skipped:
            print(f"[CorrelationGuard] Skipped {skipped} — insufficient data; using available data only")

        if not corr_map:
            print("[CorrelationGuard] No correlation data computable — failing open")
            return _allow("correlation data unavailable — failing open")

        avg_corr = float(np.mean(list(corr_map.values())))
        highly_correlated = [s for s, r in corr_map.items() if r >= self.correlation_threshold]

        if len(highly_correlated) >= self.max_correlated_positions:
            return _block(
                f"Correlation block: {len(highly_correlated)} open positions "
                f"with r>={self.correlation_threshold} to {incoming_symbol} "
                f"({', '.join(highly_correlated)})",
                corr_map, avg_corr,
            )

        if avg_corr >= self.max_portfolio_correlation:
            return _block(
                f"Portfolio correlation block: avg r={avg_corr:.2f} >= "
                f"{self.max_portfolio_correlation} to open positions",
                corr_map, avg_corr,
            )

        return _allow("", corr_map, avg_corr)


if __name__ == "__main__":
    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    np.random.seed(0)
    spy_close = pd.Series(np.cumsum(np.random.randn(80)) + 100, index=dates)
    np.random.seed(42)
    uncorrelated_close = pd.Series(np.cumsum(np.random.randn(80)) + 50, index=dates)

    prices = {
        "SPY":       spy_close,
        "COST":      spy_close * 1.02 + 0.5,
        "V":         spy_close * 0.98,
        "AMZN":      uncorrelated_close,
        "AMZN_CORR": spy_close * 1.05,
    }

    def make_fetcher(price_map):
        def fetcher(sym):
            if sym not in price_map:
                return None
            return pd.DataFrame({"close": price_map[sym]})
        return fetcher

    fetcher = make_fetcher(prices)
    guard = CorrelationGuard(
        price_lookback_days=60,
        max_portfolio_correlation=0.7,
        max_correlated_positions=2,
        correlation_threshold=0.75,
    )

    print("--- Test 1: highly correlated incoming blocked ---")
    guard._cache = {}; guard._cache_time = 0
    r = guard.check("AMZN_CORR", ["SPY", "COST", "V"], fetcher)
    assert not r["allowed"], f"Expected block, got: {r}"
    print(f"  BLOCKED: {r['reason']}")
    print(f"  corr_map: {r['correlation_map']}")

    print("--- Test 2: uncorrelated incoming allowed ---")
    guard._cache = {}; guard._cache_time = 0
    r = guard.check("AMZN", ["SPY", "COST"], fetcher)
    assert r["allowed"], f"Expected allow, got: {r}"
    print(f"  ALLOWED | avg_correlation={r['avg_correlation']:.3f}")

    print("--- Test 3: sector concentration blocks ---")
    guard._cache = {}; guard._cache_time = 0
    r = guard.check("JPM", ["V", "BRK.B"], fetcher)
    assert not r["allowed"] and "Sector" in r["reason"], f"Expected sector block, got: {r}"
    print(f"  BLOCKED: {r['reason']}")

    print("--- Test 4: fewer than 2 open positions always allows ---")
    guard._cache = {}; guard._cache_time = 0
    r = guard.check("JPM", ["SPY"], fetcher)
    assert r["allowed"], f"Expected allow (1 position), got: {r}"
    print(f"  ALLOWED (1 open position)")
    guard._cache = {}; guard._cache_time = 0
    r = guard.check("COST", [], fetcher)
    assert r["allowed"], f"Expected allow (0 positions), got: {r}"
    print(f"  ALLOWED (0 open positions)")

    print("\nAll smoke tests passed.")
