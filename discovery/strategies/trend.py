import numpy as np
import pandas as pd
import pandas_ta as ta

from discovery.strategies.base import DiscoveryStrategy


class EMATrendStrategy(DiscoveryStrategy):
    """
    EMA crossover + ADX strength filter + RSI range gate.
    3 ema_short × 3 ema_long × 3 adx_threshold × 3 rsi_period × 2 rsi_gate = 162 combos.
    rsi_gate is a (low, high) tuple stored as a list in JSONB.
    """
    strategy_type = "ema_trend"
    param_grid = {
        "ema_short":     [10, 20, 30],
        "ema_long":      [50, 100, 200],
        "adx_threshold": [0, 20, 25],
        "rsi_period":    [10, 14, 21],
        "rsi_gate":      [(35, 65), (40, 60)],
    }

    def validate_combo(self, params: dict) -> bool:
        return params["ema_short"] < params["ema_long"]

    def compute_indicators(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        df = bars.copy()
        df["ema_s"] = ta.ema(df["close"], length=params["ema_short"])
        df["ema_l"] = ta.ema(df["close"], length=params["ema_long"])
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        df["adx"] = adx_df.iloc[:, 0] if (adx_df is not None and not adx_df.empty) else np.nan
        df["rsi"] = ta.rsi(df["close"], length=params["rsi_period"])
        return df

    def generate_signals(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        ema_s = ind_df["ema_s"]
        ema_l = ind_df["ema_l"]
        adx   = ind_df["adx"]
        rsi   = ind_df["rsi"]
        rsi_low, rsi_high = params["rsi_gate"]

        cross_above = (ema_s > ema_l) & (ema_s.shift(1) <= ema_l.shift(1))
        adx_ok      = adx >= params["adx_threshold"]
        rsi_ok      = rsi.between(rsi_low, rsi_high)

        return (cross_above & adx_ok & rsi_ok).fillna(False)
