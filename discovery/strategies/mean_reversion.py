import numpy as np
import pandas as pd
import pandas_ta as ta

from discovery.strategies.base import DiscoveryStrategy


class BollingerMeanReversionStrategy(DiscoveryStrategy):
    """
    Bollinger Band lower break + RSI oversold entry; middle band cross or RSI exit.
    2 bb_period × 3 bb_std × 2 rsi_entry × 3 rsi_exit = 36 combos.
    RSI uses a fixed 14-period; bb_period and bb_std are the parametric levers.
    """
    strategy_type = "bb_mean_reversion"
    param_grid = {
        "bb_period": [20, 30],
        "bb_std":    [1.5, 2.0, 2.5],
        "rsi_entry": [30, 35],
        "rsi_exit":  [60, 65, 70],
    }

    def compute_indicators(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        df = bars.copy()
        bb = ta.bbands(df["close"], length=params["bb_period"], std=params["bb_std"])
        if bb is not None and not bb.empty:
            df["bb_lower"]  = bb.iloc[:, 0]
            df["bb_middle"] = bb.iloc[:, 1]
        else:
            df["bb_lower"] = df["bb_middle"] = np.nan
        df["rsi"] = ta.rsi(df["close"], length=14)
        return df

    def generate_signals(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        close = ind_df["close"]
        lower = ind_df["bb_lower"]
        rsi   = ind_df["rsi"]

        cross_below  = (close < lower) & (close.shift(1) >= lower.shift(1))
        rsi_oversold = rsi < params["rsi_entry"]

        return (cross_below & rsi_oversold).fillna(False)

    def exit_signal(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        close  = ind_df["close"]
        middle = ind_df["bb_middle"]
        rsi    = ind_df["rsi"]

        at_middle = (close >= middle) & (close.shift(1) < middle.shift(1))
        rsi_exit  = rsi > params["rsi_exit"]

        return (at_middle | rsi_exit).fillna(False)
