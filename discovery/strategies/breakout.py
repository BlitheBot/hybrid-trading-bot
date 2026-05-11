import numpy as np
import pandas as pd
import pandas_ta as ta

from discovery.strategies.base import DiscoveryStrategy


class DonchianBreakoutStrategy(DiscoveryStrategy):
    """
    Donchian channel upper break + ATR move confirmation + volume surge.
    3 donchian_period × 3 atr_mult × 2 vol_surge × 3 vol_avg_period = 54 combos.
    ATR uses a fixed 14-period; atr_mult gates the minimum bar move required.
    """
    strategy_type = "donchian_breakout"
    param_grid = {
        "donchian_period": [20, 40, 60],
        "atr_mult":        [0.5, 1.0, 1.5],
        "vol_surge":       [1.5, 2.0],
        "vol_avg_period":  [10, 20, 30],
    }

    def compute_indicators(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        df = bars.copy()
        n = params["donchian_period"]
        dc = ta.donchian(df["high"], df["low"], lower_length=n, upper_length=n)
        if dc is not None and not dc.empty:
            df["dc_upper"] = dc.iloc[:, 2]
        else:
            df["dc_upper"] = np.nan
        atr = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["atr"]     = atr if atr is not None else np.nan
        df["vol_avg"] = df["volume"].rolling(params["vol_avg_period"]).mean()
        return df

    def generate_signals(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        close   = ind_df["close"]
        dc_up   = ind_df["dc_upper"]
        atr     = ind_df["atr"]
        vol     = ind_df["volume"]
        vol_avg = ind_df["vol_avg"]

        # Break above previous period's upper channel (first bar of breakout)
        breakout     = (close > dc_up.shift(1)) & (close.shift(1) <= dc_up.shift(2))
        # Minimum bar move in ATR terms
        bar_move     = close - close.shift(1)
        atr_confirm  = bar_move >= params["atr_mult"] * atr
        # Volume surge above rolling average
        vol_confirm  = vol >= params["vol_surge"] * vol_avg

        return (breakout & atr_confirm & vol_confirm).fillna(False)
