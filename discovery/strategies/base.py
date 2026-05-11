import itertools
from abc import ABC, abstractmethod

import pandas as pd


class DiscoveryStrategy(ABC):
    strategy_type: str
    param_grid: dict

    # Override in subclasses that use ATR-based stops instead of Config flat %
    use_atr_stops: bool = False
    atr_stop_mult: float = 1.5
    atr_tp_mult:   float = 4.5

    @abstractmethod
    def compute_indicators(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        """Precompute all indicators on the full dataset O(n). Return augmented DataFrame."""

    @abstractmethod
    def generate_signals(self, ind_df: pd.DataFrame, params: dict) -> pd.Series:
        """Return boolean Series aligned with ind_df index. True = buy entry on that bar."""

    def exit_signal(self, ind_df: pd.DataFrame, params: dict) -> pd.Series | None:
        """Optional early-exit signal. None = rely on stop/target only."""
        return None

    def validate_combo(self, params: dict) -> bool:
        """Return False to skip this parameter combination (e.g. ema_short >= ema_long)."""
        return True

    def get_combos(self) -> list[dict]:
        keys, values = list(self.param_grid), list(self.param_grid.values())
        return [
            dict(zip(keys, combo))
            for combo in itertools.product(*values)
            if self.validate_combo(dict(zip(keys, combo)))
        ]
