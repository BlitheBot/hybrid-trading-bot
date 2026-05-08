import math
from datetime import datetime

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import Config
from strategies.swing_strategy import SwingStrategy


# ── Metrics ──────────────────────────────────────────────────────────────────

def _compute_metrics(equity_curve: list, trades: list) -> dict:
    """Compute performance metrics from a daily equity curve and closed trade list."""
    if len(equity_curve) < 2:
        return dict(
            sharpe=0.0, max_drawdown_pct=0.0, cagr_pct=0.0,
            win_rate=0.0, profit_factor=0.0, total_trades=0,
            final_equity=round(equity_curve[-1], 2) if equity_curve else 0.0,
        )

    arr = np.array(equity_curve, dtype=float)
    daily_ret = np.diff(arr) / arr[:-1]
    std = daily_ret.std()
    sharpe = (daily_ret.mean() / std * math.sqrt(252)) if std > 0 else 0.0

    peak = np.maximum.accumulate(arr)
    max_dd = float(((arr - peak) / peak).min() * 100)

    cagr = ((arr[-1] / arr[0]) ** (252 / len(arr)) - 1) * 100 if arr[0] > 0 else 0.0

    if trades:
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(trades)
        gross_loss = abs(sum(losses))
        profit_factor = sum(wins) / gross_loss if gross_loss > 0 else float("inf")
    else:
        win_rate = profit_factor = 0.0

    return dict(
        sharpe=round(sharpe, 3),
        max_drawdown_pct=round(max_dd, 2),
        cagr_pct=round(cagr, 2),
        win_rate=round(win_rate, 3),
        profit_factor=round(profit_factor, 3),
        total_trades=len(trades),
        final_equity=round(float(arr[-1]), 2),
    )


# ── Backtester ────────────────────────────────────────────────────────────────

class Backtester:
    def __init__(
        self,
        strategy,
        symbols: list,
        initial_capital: float = 10_000.0,
        risk_pct: float = 2.0,
        commission_per_share: float = 0.005,
        slippage_bps: float = 2.0,
    ):
        self.strategy = strategy
        self.symbols = symbols
        self.initial_capital = initial_capital
        self.risk_pct = risk_pct
        self.commission_per_share = commission_per_share
        self.slippage_bps = slippage_bps
        self._client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
        )
        # Bars SwingStrategy needs before it can produce a valid signal
        self._warmup = (
            getattr(strategy, "ema_long", 200)
            + getattr(strategy, "macd_slow", 26)
            + getattr(strategy, "macd_signal", 9)
            + 14
        )

    # ── Data ─────────────────────────────────────────────────────────────────

    def _fetch_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Fetch daily OHLCV bars from Alpaca. Returns a tz-naive DatetimeIndex DataFrame."""
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        df = self._client.get_stock_bars(req).df
        if df.empty:
            return df
        # Alpaca returns MultiIndex (symbol, timestamp) — flatten to DatetimeIndex
        df = df.reset_index().set_index("timestamp")
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert(None)
        df.columns = [c.lower() for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]]

    # ── Position sizing ───────────────────────────────────────────────────────

    def _calc_shares(self, equity: float, entry: float, stop: float) -> int:
        """Risk-based sizing: floor((equity * risk_pct%) / (entry - stop))."""
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            return 0
        return math.floor((equity * self.risk_pct / 100.0) / risk_per_share)

    # ── Core simulation ───────────────────────────────────────────────────────

    def _run_single(self, symbol: str, bars: pd.DataFrame, skip_bars: int = 0) -> dict:
        """
        Bar-by-bar simulation for one symbol.

        skip_bars: rows at the start used only as indicator warmup context — no trades
        are opened and the equity curve begins at row skip_bars. Used by walk_forward()
        so each test window inherits the preceding training window as context.
        """
        equity = self.initial_capital
        position = None
        equity_curve: list[float] = []
        trades: list[dict] = []
        slip = self.slippage_bps / 10_000.0

        for i in range(len(bars)):
            if i < skip_bars:
                # SwingStrategy is stateless (recomputes from the window each call),
                # so we don't need to call generate_signals during warmup.
                continue

            bar = bars.iloc[i]

            # Generate signal once per bar — window includes all bars up to now,
            # giving the strategy full historical context even in the test window.
            signal = None
            if i >= self._warmup:
                window = bars.iloc[: i + 1].copy()
                window["symbol"] = symbol
                signal = self.strategy.generate_signals(window)

            # ── Manage open position ──────────────────────────────────────────
            if position is not None:
                stop_hit = bar["low"] <= position["stop"]
                target_hit = bar["high"] >= position["target"]

                if stop_hit:
                    # Stop: conservative fill (stop wins if both stop & target hit same bar)
                    fill = position["stop"] * (1 - slip)
                    commission = self.commission_per_share * position["shares"]
                    pnl = fill * position["shares"] - commission - position["cost"]
                    equity += fill * position["shares"] - commission
                    trades.append({"type": "stop", "pnl": pnl})
                    position = None

                elif target_hit:
                    fill = position["target"] * (1 - slip)
                    commission = self.commission_per_share * position["shares"]
                    pnl = fill * position["shares"] - commission - position["cost"]
                    equity += fill * position["shares"] - commission
                    trades.append({"type": "target", "pnl": pnl})
                    position = None

                elif signal and signal.get("signal") == "sell":
                    fill = bar["close"] * (1 - slip)
                    commission = self.commission_per_share * position["shares"]
                    pnl = fill * position["shares"] - commission - position["cost"]
                    equity += fill * position["shares"] - commission
                    trades.append({"type": "sell_signal", "pnl": pnl})
                    position = None

            # ── Enter position ────────────────────────────────────────────────
            if position is None and signal and signal.get("signal") == "buy":
                entry = signal["entry_price"]
                stop = signal["stop_price"]
                target = signal["target_price"]
                shares = self._calc_shares(equity, entry, stop)
                if shares > 0:
                    fill = entry * (1 + slip)
                    commission = self.commission_per_share * shares
                    cost = fill * shares + commission
                    if cost <= equity:
                        equity -= cost
                        position = {
                            "stop": stop,
                            "target": target,
                            "shares": shares,
                            "cost": cost,
                        }

            pos_value = position["shares"] * bar["close"] if position else 0.0
            equity_curve.append(equity + pos_value)

        # Force-close any open position at end of period
        if position is not None:
            fill = bars["close"].iloc[-1] * (1 - slip)
            commission = self.commission_per_share * position["shares"]
            pnl = fill * position["shares"] - commission - position["cost"]
            equity += fill * position["shares"] - commission
            trades.append({"type": "end_of_period", "pnl": pnl})
            if equity_curve:
                equity_curve[-1] = equity

        result = _compute_metrics(equity_curve, trades)
        result["symbol"] = symbol
        result["equity_curve"] = equity_curve
        result["trades"] = trades
        return result

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, start: str = None, end: str = None) -> pd.DataFrame:
        """
        Backtest all symbols over the date range. Each symbol gets its own
        isolated capital pool so results are directly comparable.
        Returns a DataFrame ranked by Sharpe ratio (descending).
        """
        start = start or Config.BACKTEST_START_DATE
        end = end or Config.BACKTEST_END_DATE

        print(f"\n{'='*60}")
        print(f"Backtest  {start} → {end}")
        print(f"Capital ${self.initial_capital:,.0f}/symbol  |  Risk {self.risk_pct}%  "
              f"|  Slippage {self.slippage_bps}bps  |  Commission ${self.commission_per_share}/sh")
        print(f"Symbols: {', '.join(self.symbols)}")
        print(f"{'='*60}")

        rows = []
        for symbol in self.symbols:
            print(f"  {symbol}... ", end="", flush=True)
            try:
                bars = self._fetch_bars(symbol, start, end)
                if len(bars) < self._warmup + 10:
                    print(f"insufficient data ({len(bars)} bars, need {self._warmup + 10}). Skipping.")
                    continue
                r = self._run_single(symbol, bars)
                print(
                    f"Sharpe={r['sharpe']:.2f}  CAGR={r['cagr_pct']:.1f}%  "
                    f"DD={r['max_drawdown_pct']:.1f}%  "
                    f"WinRate={r['win_rate']:.0%}  Trades={r['total_trades']}"
                )
                rows.append({k: v for k, v in r.items() if k not in ("equity_curve", "trades")})
            except Exception as e:
                print(f"ERROR: {e}")

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).set_index("symbol")
        return df.sort_values("sharpe", ascending=False)

    def walk_forward(
        self,
        start: str = None,
        end: str = None,
        train_months: int = None,
        test_months: int = None,
    ) -> pd.DataFrame:
        """
        Rolling walk-forward test with fixed-length train/test windows.

        Each test window uses the preceding training window as warmup context, so the
        strategy has full indicator history even on the first bar of the test period.
        The `degradation` column (train_sharpe - test_sharpe) is the primary signal
        for the Strategy Discovery Engine to filter overfit strategies.
        """
        start = start or Config.BACKTEST_START_DATE
        end = end or Config.BACKTEST_END_DATE
        train_months = train_months if train_months is not None else Config.WALK_FORWARD_TRAIN_MONTHS
        test_months = test_months if test_months is not None else Config.WALK_FORWARD_TEST_MONTHS

        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        # Build rolling windows — train window slides forward by test_months each step
        windows = []
        cursor = start_dt
        while True:
            train_end = cursor + relativedelta(months=train_months)
            test_end = train_end + relativedelta(months=test_months)
            if test_end > end_dt:
                break
            windows.append((
                cursor.strftime("%Y-%m-%d"),
                train_end.strftime("%Y-%m-%d"),
                test_end.strftime("%Y-%m-%d"),
            ))
            cursor += relativedelta(months=test_months)

        print(f"\n{'='*60}")
        print(f"Walk-Forward  {len(windows)} windows  "
              f"Train={train_months}mo  Test={test_months}mo")
        print(f"{'='*60}")

        rows = []
        for symbol in self.symbols:
            print(f"\n  {symbol}")
            try:
                all_bars = self._fetch_bars(symbol, start, end)
                if all_bars.empty:
                    print("    No data. Skipping.")
                    continue

                for w_idx, (win_start, train_end, test_end) in enumerate(windows):
                    train_bars = all_bars[win_start:train_end]
                    # combined: train bars provide warmup context, test bars are the live window
                    combined = all_bars[win_start:test_end]
                    skip_count = len(train_bars)
                    test_bar_count = len(combined) - skip_count

                    if len(train_bars) < self._warmup + 10 or test_bar_count < 10:
                        continue

                    tr = self._run_single(symbol, train_bars)
                    ts = self._run_single(symbol, combined, skip_bars=skip_count)
                    degradation = round(tr["sharpe"] - ts["sharpe"], 3)

                    rows.append({
                        "symbol": symbol,
                        "window": w_idx + 1,
                        "train_start": win_start,
                        "train_end": train_end,
                        "test_start": train_end,
                        "test_end": test_end,
                        "train_sharpe": tr["sharpe"],
                        "test_sharpe": ts["sharpe"],
                        "train_cagr_pct": tr["cagr_pct"],
                        "test_cagr_pct": ts["cagr_pct"],
                        "train_win_rate": tr["win_rate"],
                        "test_win_rate": ts["win_rate"],
                        "train_trades": tr["total_trades"],
                        "test_trades": ts["total_trades"],
                        "degradation": degradation,
                    })
                    print(
                        f"    W{w_idx + 1} [{win_start} → {test_end}]  "
                        f"train={tr['sharpe']:.2f}  test={ts['sharpe']:.2f}  "
                        f"degradation={degradation:+.2f}"
                    )
            except Exception as e:
                print(f"    ERROR: {e}")

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)


if __name__ == "__main__":
    strategy = SwingStrategy("Swing")
    bt = Backtester(
        strategy=strategy,
        symbols=Config.SWING_SYMBOLS,
        initial_capital=10_000.0,
    )

    print("\n=== Standard Backtest ===")
    results = bt.run()
    if not results.empty:
        print(results[[
            "sharpe", "cagr_pct", "max_drawdown_pct",
            "win_rate", "profit_factor", "total_trades",
        ]].to_string())

    print("\n=== Walk-Forward Test ===")
    wf = bt.walk_forward()
    if not wf.empty:
        print(wf.to_string(index=False))
