"""
Persistence layer for discovered indicators.

Table: discovered_indicators
  status values: 'candidate', 'graduated', 'rejected'

graduated  = passed walk_forward_ic (mean_ic > 0.05 AND std_ic < 0.10)
rejected   = failed validation
candidate  = saved but not yet evaluated (unused in scaffold — reserved for future streaming)
"""
from sqlalchemy import text as sql_text


class IndicatorLibrary:
    def __init__(self, db_engine):
        self._engine = db_engine

    # ── Schema ────────────────────────────────────────────────────────────────

    def create_table_if_not_exists(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(sql_text("""
                CREATE TABLE IF NOT EXISTS discovered_indicators (
                    id            SERIAL PRIMARY KEY,
                    formula       TEXT,
                    mean_ic       FLOAT,
                    std_ic        FLOAT,
                    n_folds       INT,
                    discovered_at TIMESTAMP DEFAULT NOW(),
                    symbol        TEXT,
                    regime        TEXT,
                    status        TEXT DEFAULT 'candidate'
                )
            """))

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(
        self,
        expression_tree,
        fitness_result: dict,
        symbol: str,
        regime: str,
    ) -> None:
        status = "graduated" if fitness_result.get("passed") else "rejected"
        with self._engine.begin() as conn:
            conn.execute(sql_text("""
                INSERT INTO discovered_indicators
                    (formula, mean_ic, std_ic, n_folds, symbol, regime, status)
                VALUES
                    (:formula, :mean_ic, :std_ic, :n_folds, :symbol, :regime, :status)
            """), {
                "formula": expression_tree.to_string(),
                "mean_ic": float(fitness_result.get("mean_ic", 0.0)),
                "std_ic":  float(fitness_result.get("std_ic",  1.0)),
                "n_folds": int(fitness_result.get("n_folds",   0)),
                "symbol":  symbol,
                "regime":  regime,
                "status":  status,
            })

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_graduated(self, symbol: str, regime: str) -> list[dict]:
        """Returns all graduated indicators for a symbol, matching regime or 'any'."""
        with self._engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT formula, mean_ic, std_ic, n_folds, discovered_at, regime
                FROM   discovered_indicators
                WHERE  symbol = :symbol
                  AND  status = 'graduated'
                  AND  (regime = :regime OR regime = 'any')
                ORDER BY mean_ic DESC
            """), {"symbol": symbol, "regime": regime}).mappings().fetchall()
        return [dict(r) for r in rows]
