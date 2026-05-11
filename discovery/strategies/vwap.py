import numpy as np
import pandas as pd
import pandas_ta as ta

from discovery.strategies.base import DiscoveryStrategy


class VWAPDeviationStrategy(DiscoveryStrategy):
    """
    Rolling VWAP deviation entry (price dips N% below volume-weighted average) +
    RSI oversold confirmation. Stop = 1.5× ATR, target = 4.5× ATR (3:1 R/R).
    4 vwap_period × 4 deviation_pct × 2 rsi_confirm = 32 combos.

    VWAP is computed as a rolling typical-price × volume sum / volume sum over
    vwap_period bars — a volume-weighted mean-reversion anchor for daily charts.
    """
    strategy_type = "vwap_deviation"
    param_grid = {
        "vwap_period":   [10, 20, 30, 50],
        "deviation_pct": [0.5, 1.0, 1.5, 2.0],
        "rsi_confirm":   [45, 50],
    }

    use_atr_stops = True
    atr_stop_mult = 1.5
    atr_tp_mult   = 4.5

    def compute_indicators(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        df = bars.copy()
        typical = (df["high"] + df["low"] + df["close"]) / 3
        vol     = df["volume"]
        n       = params["vwap_period"]
        df["vwap"] = (typical * vol).rolling(n).sum() / vol.rolling(n).sum()
        df["rsi"]  = ta.rsi(df["close"], length=14)
        df["atr"]  = ta.atr(df["high"], df["low"], df["close"], length=14)
        return df

    def generate_signals(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        close = ind_df["close"]
        vwap  = ind_df["vwap"]
        rsi   = ind_df["rsi"]

        threshold   = vwap * (1.0 - params["deviation_pct"] / 100.0)
        # First bar where price crosses below the deviation threshold
        cross_below = (close <= threshold) & (close.shift(1) > threshold.shift(1))
        rsi_ok      = rsi < params["rsi_confirm"]

        return (cross_below & rsi_ok).fillna(False)
