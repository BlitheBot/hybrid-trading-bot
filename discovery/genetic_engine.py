"""
Genetic engine — evolves novel indicators via GP and saves validated ones.

Designed to be CPU-bound / sync; callers should wrap in asyncio.to_thread().
One GeneticEngine instance per overnight run; reuse across symbols is safe
(no mutable shared state between run() calls).

Lifecycle per symbol:
  1. Initialize random population of ExpressionNode trees
  2. Walk-forward IC evaluation on each candidate
  3. Select top 20% as elites (parents)
  4. Generate next generation via crossover + mutation
  5. Repeat for n_generations
  6. Save all candidates; graduate those with mean_ic > 0.05
"""
import random

import numpy as np
import pandas as pd

from .expression_tree import ExpressionNode
from .fitness_evaluator import FitnessEvaluator
from .indicator_library import IndicatorLibrary


class GeneticEngine:
    def __init__(
        self,
        population_size: int   = 50,
        n_generations:   int   = 20,
        mutation_rate:   float = 0.3,
        crossover_rate:  float = 0.5,
        max_tree_depth:  int   = 4,
    ):
        self.population_size = population_size
        self.n_generations   = n_generations
        self.mutation_rate   = mutation_rate
        self.crossover_rate  = crossover_rate
        self.max_tree_depth  = max_tree_depth
        self._evaluator      = FitnessEvaluator()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _init_population(self, rng: random.Random) -> list[ExpressionNode]:
        return [
            ExpressionNode.random_tree(self.max_tree_depth, rng)
            for _ in range(self.population_size)
        ]

    def _score_population(
        self, population: list[ExpressionNode], bars_df: pd.DataFrame
    ) -> list[tuple[ExpressionNode, dict]]:
        scored = []
        for tree in population:
            try:
                fitness = self._evaluator.walk_forward_ic(tree, bars_df)
            except Exception:
                fitness = {"mean_ic": 0.0, "std_ic": 1.0, "n_folds": 0, "passed": False}
            scored.append((tree, fitness))
        return scored

    def _select_elites(
        self, scored: list[tuple[ExpressionNode, dict]], top_pct: float = 0.20
    ) -> list[ExpressionNode]:
        ranked = sorted(scored, key=lambda x: x[1]["mean_ic"], reverse=True)
        n      = max(2, int(len(ranked) * top_pct))
        return [tree for tree, _ in ranked[:n]]

    def _next_generation(
        self, elites: list[ExpressionNode], rng: random.Random
    ) -> list[ExpressionNode]:
        next_gen = list(elites)  # elites carry over unchanged
        while len(next_gen) < self.population_size:
            if rng.random() < self.crossover_rate and len(elites) >= 2:
                a, b  = rng.sample(elites, 2)
                child = ExpressionNode.crossover(a, b, rng)
            else:
                parent = rng.choice(elites)
                child  = parent.mutate(rng)
            next_gen.append(child)
        return next_gen[: self.population_size]

    # ── Main entry point ──────────────────────────────────────────────────────

    def run(
        self,
        bars_df:   pd.DataFrame,
        symbol:    str,
        regime:    str,
        db_engine,
    ) -> list[dict]:
        """
        Evolves indicators for one symbol. Returns list of graduated indicator dicts.
        Saves ALL candidates (graduated + rejected) to discovered_indicators for analysis.
        """
        rng     = random.Random(42)
        library = IndicatorLibrary(db_engine)
        library.create_table_if_not_exists()

        population = self._init_population(rng)

        for gen in range(self.n_generations):
            scored       = self._score_population(population, bars_df)
            best_tree, best_fitness = max(scored, key=lambda x: x[1]["mean_ic"])
            print(
                f"[GENETIC] {symbol} Gen {gen + 1}/{self.n_generations}: "
                f"best_ic={best_fitness['mean_ic']:.3f} "
                f"formula={best_tree.to_string()[:60]}"
            )
            elites     = self._select_elites(scored)
            population = self._next_generation(elites, rng)

        # Final evaluation pass on the last generation
        final_scored  = self._score_population(population, bars_df)
        seen_formulas: set[str] = set()
        graduated: list[dict]   = []
        n_rejected = 0

        for tree, fitness in final_scored:
            formula = tree.to_string()
            if formula in seen_formulas:
                continue
            seen_formulas.add(formula)

            try:
                library.save(tree, fitness, symbol, regime)
            except Exception as e:
                print(f"[GENETIC] {symbol}: DB save failed for '{formula[:40]}': {e}")
                continue

            if fitness["passed"]:
                graduated.append({"formula": formula, **fitness})
            else:
                n_rejected += 1

        print(
            f"[GENETIC] {symbol}: {len(graduated)} indicators graduated, "
            f"{n_rejected} rejected"
        )
        return graduated


if __name__ == "__main__":
    import numpy as np

    print("--- GeneticEngine smoke test (no DB, no Alpaca) ---")
    np.random.seed(0)
    n = 300
    t = np.linspace(0, 4 * np.pi, n)
    _closes = 100 + np.cumsum(np.random.randn(n) * 0.5)
    _df = pd.DataFrame({
        "close":  _closes,
        "high":   _closes * 1.005,
        "low":    _closes * 0.995,
        "open":   _closes,
        "volume": np.random.randint(1_000_000, 5_000_000, n).astype(float),
    })

    class _NoOpLibrary:
        def create_table_if_not_exists(self): pass
        def save(self, *a, **kw): pass

    engine = GeneticEngine(population_size=10, n_generations=3, max_tree_depth=2)
    engine._evaluator.ic_threshold = -1.0  # pass everything for smoke test

    class _FakeDBEngine:
        pass

    import unittest.mock as mock
    with mock.patch("discovery.indicator_library.IndicatorLibrary", return_value=_NoOpLibrary()):
        graduated = engine.run(_df, "TEST", "any", _FakeDBEngine())

    print(f"Smoke test complete — {len(graduated)} indicators returned (expected >=0)")
