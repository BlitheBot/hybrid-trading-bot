"""
Out-of-sample integrity enforcement (Task 2).

A :class:`DataPartitioner` splits a symbol's historical bars into three disjoint,
chronologically ordered regions and *enforces* that optimization code can never
peek at data it must not see:

    [ ───── training 70% ───── ][ ─ validation 15% ─ ][ ─ holdout 15% ─ ]
      parameter optimization      walk-forward OOS       NEVER touched until
      (always accessible)         (must be unlocked)     a live-deploy decision

The wall is enforced at the accessor level:

    * ``get_training()``    — always allowed.
    * ``get_validation()``  — raises :class:`PartitionViolation` (a ``ValueError``)
                              unless ``unlock_validation()`` was called first.
    * ``get_holdout()``     — raises unless ``unlock_holdout(reason=...)`` was called,
                              which also logs *why* the final wall was breached.

This makes accidental leakage a loud, immediate failure rather than a silent bias.
Partition boundaries are persisted per symbol to the ``data_partitions`` table and
logged at Discovery Engine startup.
"""
from __future__ import annotations

import traceback

import pandas as pd


class PartitionViolation(ValueError):
    """Raised when guarded validation/holdout data is accessed without unlocking."""


def _fmt(ts) -> str:
    """Format a partition boundary timestamp/index value as a short string."""
    try:
        if hasattr(ts, "strftime"):
            return ts.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        pass
    return str(ts)


class DataPartitioner:
    """Three-way train/validation/holdout splitter with a guarded data wall."""

    def __init__(
        self,
        df: pd.DataFrame,
        symbol: str,
        train_frac: float = 0.70,
        val_frac: float = 0.15,
    ):
        if df is None or len(df) == 0:
            raise ValueError(f"DataPartitioner: empty data for {symbol}")
        if not (0.0 < train_frac < 1.0) or not (0.0 < val_frac < 1.0):
            raise ValueError("train_frac and val_frac must be in (0, 1)")
        if train_frac + val_frac >= 1.0:
            raise ValueError("train_frac + val_frac must leave a non-empty holdout")

        self.symbol = symbol
        self._df = df
        n = len(df)
        self.n_bars = n
        self.train_frac = train_frac
        self.val_frac = val_frac
        self.holdout_frac = round(1.0 - train_frac - val_frac, 6)

        # Disjoint, contiguous index ranges. train = [0, train_end),
        # val = [train_end, val_end), holdout = [val_end, n).
        self.train_end = int(n * train_frac)
        self.val_end = int(n * (train_frac + val_frac))
        # Guarantee every region holds at least one bar on tiny inputs.
        self.train_end = max(1, min(self.train_end, n - 2))
        self.val_end = max(self.train_end + 1, min(self.val_end, n - 1))

        self._validation_unlocked = False
        self._holdout_unlocked = False

    # ── boundary metadata ──────────────────────────────────────────────────────

    @property
    def boundaries(self) -> dict:
        idx = self._df.index
        return {
            "symbol": self.symbol,
            "n_bars": self.n_bars,
            "train_start": _fmt(idx[0]),
            "train_end": _fmt(idx[self.train_end - 1]),
            "val_start": _fmt(idx[self.train_end]),
            "val_end": _fmt(idx[self.val_end - 1]),
            "holdout_start": _fmt(idx[self.val_end]),
            "holdout_end": _fmt(idx[-1]),
        }

    def log_boundaries(self) -> None:
        b = self.boundaries
        print(
            f"[Partition] {self.symbol} "
            f"train={b['train_start']}→{b['train_end']} "
            f"val={b['val_start']}→{b['val_end']} "
            f"holdout={b['holdout_start']}→{b['holdout_end']}"
        )

    # ── unlock controls ────────────────────────────────────────────────────────

    def unlock_validation(self) -> "DataPartitioner":
        """Permit validation-set access (walk-forward OOS evaluation)."""
        self._validation_unlocked = True
        print(f"[Partition] {self.symbol}: validation set unlocked for OOS evaluation")
        return self

    def unlock_holdout(self, reason: str) -> "DataPartitioner":
        """Permit holdout access. Requires an explicit reason — the final wall."""
        if not reason:
            raise PartitionViolation(
                f"{self.symbol}: holdout unlock requires a non-empty reason"
            )
        self._holdout_unlocked = True
        print(f"[Partition] {self.symbol}: HOLDOUT unlocked — reason: {reason}")
        return self

    # ── guarded accessors ──────────────────────────────────────────────────────

    def get_training(self) -> pd.DataFrame:
        """First ``train_frac`` of the data — the only region optimization may use."""
        return self._df.iloc[: self.train_end]

    def get_validation(self) -> pd.DataFrame:
        if not self._validation_unlocked:
            raise PartitionViolation(
                f"{self.symbol}: validation data accessed during optimization — "
                f"call unlock_validation() first"
            )
        return self._df.iloc[self.train_end : self.val_end]

    def get_holdout(self) -> pd.DataFrame:
        if not self._holdout_unlocked:
            raise PartitionViolation(
                f"{self.symbol}: holdout data accessed before a deploy decision — "
                f"call unlock_holdout(reason=...) first"
            )
        return self._df.iloc[self.val_end :]

    def get_non_holdout(self) -> pd.DataFrame:
        """Training + validation (everything except the reserved holdout).

        Used by the existing MCPT/walk-forward pipeline, which performs its own
        internal in-sample/out-of-sample split. The holdout is *never* included,
        so it stays pristine until ``get_holdout`` is explicitly unlocked.
        """
        return self._df.iloc[: self.val_end]

    # ── persistence ────────────────────────────────────────────────────────────

    def persist(self, conn) -> None:
        """Upsert this symbol's partition boundaries into ``data_partitions``.

        ``conn`` is a psycopg2 connection. Fail-open: a DB error logs and returns.
        """
        if conn is None:
            return
        b = self.boundaries
        try:
            ensure_partition_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO data_partitions (
                        symbol, n_bars, train_frac, val_frac, holdout_frac,
                        train_start, train_end, val_start, val_end,
                        holdout_start, holdout_end, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        n_bars        = EXCLUDED.n_bars,
                        train_frac    = EXCLUDED.train_frac,
                        val_frac      = EXCLUDED.val_frac,
                        holdout_frac  = EXCLUDED.holdout_frac,
                        train_start   = EXCLUDED.train_start,
                        train_end     = EXCLUDED.train_end,
                        val_start     = EXCLUDED.val_start,
                        val_end       = EXCLUDED.val_end,
                        holdout_start = EXCLUDED.holdout_start,
                        holdout_end   = EXCLUDED.holdout_end,
                        updated_at    = NOW()
                    """,
                    (
                        self.symbol, self.n_bars, self.train_frac, self.val_frac,
                        self.holdout_frac, b["train_start"], b["train_end"],
                        b["val_start"], b["val_end"], b["holdout_start"], b["holdout_end"],
                    ),
                )
            conn.commit()
            print(f"[Partition] {self.symbol}: boundaries persisted to data_partitions")
        except Exception:
            print(f"[Partition] {self.symbol}: persist failed:\n{traceback.format_exc()}")
            try:
                conn.rollback()
            except Exception:
                pass


def ensure_partition_table(conn) -> None:
    """Create the ``data_partitions`` table if it does not already exist."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS data_partitions (
                symbol        VARCHAR(10) PRIMARY KEY,
                n_bars        INTEGER,
                train_frac    FLOAT,
                val_frac      FLOAT,
                holdout_frac  FLOAT,
                train_start   VARCHAR(20),
                train_end     VARCHAR(20),
                val_start     VARCHAR(20),
                val_end       VARCHAR(20),
                holdout_start VARCHAR(20),
                holdout_end   VARCHAR(20),
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
    conn.commit()
