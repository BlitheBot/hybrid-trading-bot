"""
ExpressionNode — a candidate indicator represented as a recursive tree.

Leaves are raw features or existing-signal wrappers (arity 0).
Internal nodes are unary (arity 1) or binary (arity 2) primitives.
"""
import copy
import random

import numpy as np
import pandas as pd

from .primitives import PRIMITIVE_REGISTRY, LEAF_NAMES, UNARY_NAMES, BINARY_NAMES


def _collect_nodes(
    node: "ExpressionNode",
    parent: "ExpressionNode | None" = None,
    child_idx: int = -1,
    result: list | None = None,
) -> list[tuple["ExpressionNode", "ExpressionNode | None", int]]:
    """BFS: returns list of (node, parent, child_index) for every node in the tree."""
    if result is None:
        result = []
    result.append((node, parent, child_idx))
    for i, child in enumerate(node.children):
        _collect_nodes(child, node, i, result)
    return result


class ExpressionNode:
    """
    op_name  — name of the primitive (key in PRIMITIVE_REGISTRY), or a leaf name
    children — list of ExpressionNode; empty for leaves
    depth    — depth of the subtree rooted here (leaves = 0)
    """

    def __init__(self, op_name: str, children: list, depth: int = 0):
        self.op_name  = op_name
        self.children = children
        self.depth    = depth

    # ── Evaluation ────────────────────────────────────────────────────────────

    def evaluate(self, bars_df: pd.DataFrame) -> pd.Series:
        arity, fn = PRIMITIVE_REGISTRY[self.op_name]
        if arity == 0:
            result = fn(bars_df)
        elif arity == 1:
            result = fn(self.children[0].evaluate(bars_df))
        else:
            result = fn(
                self.children[0].evaluate(bars_df),
                self.children[1].evaluate(bars_df),
            )
        return pd.to_numeric(result, errors="coerce").replace([np.inf, -np.inf], np.nan)

    # ── String representation ─────────────────────────────────────────────────

    def to_string(self) -> str:
        arity, _ = PRIMITIVE_REGISTRY[self.op_name]
        if arity == 0:
            return self.op_name
        if arity == 1:
            return f"{self.op_name}({self.children[0].to_string()})"
        return f"{self.op_name}({self.children[0].to_string()}, {self.children[1].to_string()})"

    # ── Random tree generation ────────────────────────────────────────────────

    @classmethod
    def random_tree(cls, max_depth: int, rng: random.Random) -> "ExpressionNode":
        """Build a random valid tree up to max_depth deep."""
        if max_depth <= 0:
            op = rng.choice(LEAF_NAMES)
            return cls(op, [], 0)

        roll = rng.random()

        # Probability of picking a leaf increases as max_depth shrinks
        p_leaf = 0.25 if max_depth >= 3 else (0.45 if max_depth == 2 else 0.65)

        if roll < p_leaf:
            op = rng.choice(LEAF_NAMES)
            return cls(op, [], 0)

        # Favour unary over binary to keep trees manageable
        if roll < p_leaf + 0.50:
            op    = rng.choice(UNARY_NAMES)
            child = cls.random_tree(max_depth - 1, rng)
            return cls(op, [child], child.depth + 1)

        op    = rng.choice(BINARY_NAMES)
        left  = cls.random_tree(max_depth - 1, rng)
        right = cls.random_tree(max_depth - 1, rng)
        return cls(op, [left, right], max(left.depth, right.depth) + 1)

    # ── Mutation ──────────────────────────────────────────────────────────────

    def mutate(self, rng: random.Random) -> "ExpressionNode":
        """Return a new tree with one randomly chosen node replaced by a new random subtree."""
        new_tree = copy.deepcopy(self)
        nodes    = _collect_nodes(new_tree)
        node, parent, child_idx = rng.choice(nodes)

        replacement = ExpressionNode.random_tree(max_depth=2, rng=rng)

        if parent is None:
            return replacement
        parent.children[child_idx] = replacement
        return new_tree

    # ── Crossover ─────────────────────────────────────────────────────────────

    @classmethod
    def crossover(
        cls,
        tree_a: "ExpressionNode",
        tree_b: "ExpressionNode",
        rng: random.Random,
    ) -> "ExpressionNode":
        """
        Return a new tree: tree_a with one random subtree replaced by
        a random subtree from tree_b.
        """
        new_a  = copy.deepcopy(tree_a)
        b_copy = copy.deepcopy(tree_b)

        b_nodes = _collect_nodes(b_copy)
        a_nodes = _collect_nodes(new_a)

        donor, _, _       = rng.choice(b_nodes)
        _, a_parent, a_idx = rng.choice(a_nodes)

        if a_parent is None:
            return copy.deepcopy(donor)
        a_parent.children[a_idx] = copy.deepcopy(donor)
        return new_a

    def __repr__(self) -> str:
        return f"ExpressionNode({self.to_string()!r})"
