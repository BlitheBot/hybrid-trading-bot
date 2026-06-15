"""
Scores a candidate indicator via Information Coefficient (IC).

IC = Spearman rank correlation between indicator values and N-day forward returns.
Walk-forward IC splits bars into n_splits folds and measures IC consistency
across folds, so a single lucky fold cannot pass validation.

Thresholds (configurable):
  ic_threshold     0.05  — minimum mean IC to be considered useful
  ic_std_threshold 0.10  — maximum IC std across folds (consistency gate)
"""
import numpy as np
import pandas as pd
from scipy.stats import spearmanr


class FitnessEvaluator:
    def __init__(
        self,
        forward_period: int  = 5,
        ic_threshold:   float = 0.05,
        ic_std_threshold: float = 0.10,
        val_pvalue_threshold: float = 0.01,
    ):
        self.forward_period    = forward_period
        self.ic_threshold      = ic_threshold
        self.ic_std_threshold  = ic_std_threshold
        # Task 7 graduation gate: validation-set p-value must be below this.
        self.val_pvalue_threshold = val_pvalue_threshold

    # ── Single-fold IC ────────────────────────────────────────────────────────

    def _forward_returns(self, prices: pd.Series) -> pd.Series:
        """N-day forward close-to-close returns, aligned to current bar."""
        return prices.pct_change(self.forward_period).shift(-self.forward_period)

    def evaluate(
        self,
        indicator_series: pd.Series,
        forward_returns:  pd.Series,
    ) -> dict:
        """
        Spearman IC between indicator and forward returns on the shared non-NaN rows.
        Returns {ic, ic_pvalue, passed}.
        """
        mask = indicator_series.notna() & forward_returns.notna()
        if mask.sum() < 20:
            return {"ic": 0.0, "ic_pvalue": 1.0, "passed": False}

        corr, pval = spearmanr(indicator_series[mask], forward_returns[mask])
        ic   = float(corr) if not np.isnan(corr) else 0.0
        pval = float(pval) if not np.isnan(pval)  else 1.0

        return {
            "ic":        round(ic,   4),
            "ic_pvalue": round(pval, 4),
            "passed":    abs(ic) > self.ic_threshold,
        }

    # ── Walk-forward IC ───────────────────────────────────────────────────────

    def walk_forward_ic(
        self,
        expression_tree,
        bars_df:  pd.DataFrame,
        n_splits: int = 5,
    ) -> dict:
        """
        Evaluates IC on each of n_splits equal test folds.
        Returns {mean_ic, std_ic, n_folds, passed}.
        passed = mean_ic > ic_threshold AND std_ic < ic_std_threshold.
        """
        _invalid = {"mean_ic": 0.0, "std_ic": 1.0, "n_folds": 0, "passed": False}

        try:
            indicator = expression_tree.evaluate(bars_df)
        except Exception:
            return _invalid

        if indicator.isna().all():
            return _invalid

        prices  = bars_df["close"]
        fwd_ret = self._forward_returns(prices)

        fold_size = len(bars_df) // (n_splits + 1)
        if fold_size < 20:
            return _invalid

        ic_values: list[float] = []
        for i in range(n_splits):
            start = (i + 1) * fold_size
            end   = start + fold_size
            if end > len(bars_df):
                break
            result = self.evaluate(
                indicator.iloc[start:end],
                fwd_ret.iloc[start:end],
            )
            ic_values.append(result["ic"])

        if not ic_values:
            return _invalid

        mean_ic = float(np.mean(ic_values))
        std_ic  = float(np.std(ic_values))
        passed  = mean_ic > self.ic_threshold and std_ic < self.ic_std_threshold

        return {
            "mean_ic": round(mean_ic, 4),
            "std_ic":  round(std_ic,  4),
            "n_folds": len(ic_values),
            "passed":  passed,
        }

    # ── Train/validation IC (Task 7 GP fitness + graduation) ────────────────────

    def evaluate_on_set(self, expression_tree, bars_df: pd.DataFrame) -> dict:
        """
        Single-pass IC + p-value of an indicator over a whole bar slice. Used for the
        out-of-sample validation graduation gate. Returns {ic, ic_pvalue, n}.
        """
        try:
            indicator = expression_tree.evaluate(bars_df)
        except Exception:
            return {"ic": 0.0, "ic_pvalue": 1.0, "n": 0}
        fwd_ret = self._forward_returns(bars_df["close"])
        res = self.evaluate(indicator, fwd_ret)
        mask = indicator.notna() & fwd_ret.notna()
        return {"ic": res["ic"], "ic_pvalue": res["ic_pvalue"], "n": int(mask.sum())}

    def training_fitness(self, expression_tree, train_df: pd.DataFrame) -> dict:
        """Walk-forward IC on the TRAINING slice only (evolution fitness signal)."""
        return self.walk_forward_ic(expression_tree, train_df)

    def graduation_check(self, expression_tree, val_df: pd.DataFrame) -> dict:
        """
        Task 7 graduation gate, evaluated on the held-out VALIDATION slice:
        graduate iff |IC| > ic_threshold AND p-value < val_pvalue_threshold.

        Returns {val_ic, val_pvalue, val_n, graduated}.
        """
        res = self.evaluate_on_set(expression_tree, val_df)
        graduated = (
            abs(res["ic"]) > self.ic_threshold
            and res["ic_pvalue"] < self.val_pvalue_threshold
            and res["n"] >= 20
        )
        return {
            "val_ic": res["ic"],
            "val_pvalue": res["ic_pvalue"],
            "val_n": res["n"],
            "graduated": graduated,
        }
