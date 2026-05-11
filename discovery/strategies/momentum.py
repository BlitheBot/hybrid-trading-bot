import numpy as np
import pandas as pd
import pandas_ta as ta

from discovery.strategies.base import DiscoveryStrategy


class ROCMomentumStrategy(DiscoveryStrategy):
    """
    Rate of Change crosses zero + RSI momentum confirmation + volume surge.
    3 roc_period × 2 rsi_min × 3 vol_mult × 3 rsi_period = 54 combos.
    Volume baseline uses a fixed 20-day rolling mean.
    """
    strategy_type = "roc_momentum"
    param_grid = {
        "roc_period": [10, 20, 30],
        "rsi_min":    [50, 60],
        "vol_mult":   [1.2, 1.5, 2.0],
        "rsi_period": [10, 14, 21],
    }

    def compute_indicators(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        df = bars.copy()
        df["roc"]     = ta.roc(df["close"], length=params["roc_period"])
        df["rsi"]     = ta.rsi(df["close"], length=params["rsi_period"])
        df["vol_avg"] = df["volume"].rolling(20).mean()
        return df

    def generate_signals(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        roc     = ind_df["roc"]
        rsi     = ind_df["rsi"]
        vol     = ind_df["volume"]
        vol_avg = ind_df["vol_avg"]

        roc_cross = (roc > 0) & (roc.shift(1) <= 0)
        rsi_ok    = rsi >= params["rsi_min"]
        vol_ok    = vol >= params["vol_mult"] * vol_avg

        return (roc_cross & rsi_ok & vol_ok).fillna(False)
