"""
Discovery family 8 — Sector Rotation (Task 6).

Position-vector adapter compatible with the permutation/regime validation pipeline
(``SwingPositionStrategy`` interface: ``name``, ``param_grid()``, ``position_vector()``).

Thesis
------
Capital rotates between the 11 GICS sectors with the economic cycle. A stock that
*leads* a top-ranked sector tends to keep outperforming; a stock *lagging* a
bottom-ranked sector tends to keep underperforming.

Signal logic (per bar, using ``rs_period``-day relative strength)
-----------------------------------------------------------------
  * LONG  : the symbol's sector is in the TOP   ``top_n_sectors`` by RS
            AND the stock's own ``rs_period``-day return > its sector's return
            AND sector-ETF volume > ``volume_threshold`` × its 20-day average.
  * SHORT : the symbol's sector is in the BOTTOM ``top_n_sectors`` by RS
            AND the stock's own ``rs_period``-day return < its sector's return
            AND sector-ETF volume > ``volume_threshold`` × its 20-day average.
Exit (permutation-safe, close-only): the sector leaves the top/bottom band, or the
stock stops leading/lagging its sector.

Data requirement & fail-open
----------------------------
Needs the per-bar sector columns (``sector_rank_{p}``, ``sector_ret_{p}``,
``sector_vol_ratio``) injected by
``discovery.data_feeds.sector_rotation_data.attach_sector_rotation`` via the
``enrich`` hook below. Those columns are derived from the sector ETFs, not the
permuted stock price, so they survive MCPT. With no columns / all-NaN data the
strategy returns an all-flat vector and never validates — it does NOT crash.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

_N_SECTORS = 11


class SectorRotationPositionStrategy:
    name = "sector_rotation"

    PARAM_GRID = {
        "rs_period": [10, 20, 30],
        "top_n_sectors": [2, 3, 4],
        "volume_threshold": [1.1, 1.2, 1.5],
    }

    def param_grid(self) -> list[dict]:
        return [
            {"rs_period": rp, "top_n_sectors": tn, "volume_threshold": vt}
            for rp, tn, vt in itertools.product(
                self.PARAM_GRID["rs_period"],
                self.PARAM_GRID["top_n_sectors"],
                self.PARAM_GRID["volume_threshold"],
            )
        ]

    @staticmethod
    def enrich(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Attach the per-bar sector-rotation columns (used by the engine)."""
        try:
            from discovery.data_feeds.sector_rotation_data import attach_sector_rotation
            return attach_sector_rotation(bars, symbol)
        except Exception:
            import traceback
            print(f"[SectorRot] enrich failed for {symbol}:\n{traceback.format_exc()}")
            return bars

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        n = len(df)
        pos = np.zeros(n, dtype=float)

        rs_period = int(params["rs_period"])
        top_n = int(params["top_n_sectors"])
        vol_thresh = float(params["volume_threshold"])

        rank_col = f"sector_rank_{rs_period}"
        sret_col = f"sector_ret_{rs_period}"
        if rank_col not in df.columns or sret_col not in df.columns:
            return pos  # documented fail-open: no sector feed → no trades
        if n < rs_period + 2:
            return pos

        close = df["close"].to_numpy(dtype=float)
        rank = df[rank_col].to_numpy(dtype=float)
        sector_ret = df[sret_col].to_numpy(dtype=float)
        vol_ratio = (
            df["sector_vol_ratio"].to_numpy(dtype=float)
            if "sector_vol_ratio" in df.columns else np.full(n, np.nan)
        )

        bottom_floor = _N_SECTORS - top_n + 1  # rank >= this == bottom-n

        state = 0.0
        for i in range(rs_period, n):
            r = rank[i]
            sret = sector_ret[i]
            if not (np.isfinite(r) and np.isfinite(sret)) or close[i - rs_period] <= 0:
                pos[i] = state
                continue

            stock_ret = close[i] / close[i - rs_period] - 1.0
            # Volume confirmation: require it only when data is present (else neutral-pass).
            vol_ok = (not np.isfinite(vol_ratio[i])) or (vol_ratio[i] > vol_thresh)

            in_top = r <= top_n
            in_bottom = r >= bottom_floor
            leads = stock_ret > sret
            lags = stock_ret < sret

            if state == 0.0:
                if in_top and leads and vol_ok:
                    state = 1.0
                elif in_bottom and lags and vol_ok:
                    state = -1.0
            elif state == 1.0:
                # Exit long when the sector falls out of the top band or the stock
                # stops leading.
                if (not in_top) or (not leads):
                    state = 0.0
            elif state == -1.0:
                if (not in_bottom) or (not lags):
                    state = 0.0

            pos[i] = state

        return pos
