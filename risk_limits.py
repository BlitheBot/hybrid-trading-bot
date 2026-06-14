"""
Risk Management Upgrade (Task 8) — pure decision functions.

The data gathering (open positions, account equity, signal_outcomes history) lives
in ``bot.py``; this module holds the deterministic, unit-tested rules so they can
be verified without Alpaca or a database:

  * sector concentration   — no more than ``max_sector_pct`` of total exposure in
    any single GICS sector (uses CorrelationGuard.SECTOR_MAP).
  * single-position cap     — no entry larger than ``max_position_pct`` of equity.
  * weekly loss limit       — when weekly P&L < ``threshold_pct``, shrink new
    positions by ``reduction`` for the rest of the week.
  * consecutive-loss pause  — after ``limit`` losers in a row, pause new entries.
"""
from __future__ import annotations

import math


def sector_exposure_ok(
    sector_map: dict,
    candidate_symbol: str,
    positions: list[tuple[str, float]],
    max_sector_pct: float,
) -> tuple[bool, str, float, str]:
    """Check whether a new entry would over-concentrate a sector.

    ``positions`` is a list of (symbol, abs_market_value). Returns
    (ok, sector, current_share_pct, reason). Unknown sector → always ok (we can't
    constrain what we can't classify). The check is pre-trade: it blocks when the
    candidate's sector ALREADY holds >= max_sector_pct of total exposure.
    """
    sector = sector_map.get(candidate_symbol)
    if sector is None:
        return True, "unknown", 0.0, "sector unknown — not constrained"
    total = sum(abs(mv) for _s, mv in positions)
    if total <= 0:
        return True, sector, 0.0, "no existing exposure"
    sector_exp = sum(abs(mv) for s, mv in positions if sector_map.get(s) == sector)
    share_pct = sector_exp / total * 100.0
    if share_pct >= max_sector_pct:
        return False, sector, share_pct, (
            f"sector {sector} already {share_pct:.1f}% of exposure "
            f"(cap {max_sector_pct:.0f}%)"
        )
    return True, sector, share_pct, "within sector cap"


def single_position_share_cap(equity: float, entry_price: float, max_position_pct: float) -> int:
    """Max whole shares so position notional <= max_position_pct of equity."""
    if equity <= 0 or entry_price <= 0:
        return 0
    return int(math.floor((equity * max_position_pct / 100.0) / entry_price))


def weekly_loss_reduction(weekly_pnl_pct: float, threshold_pct: float, reduction: float) -> float:
    """Return ``reduction`` (e.g. 0.5) when weekly P&L breached the limit, else 1.0."""
    if weekly_pnl_pct < threshold_pct:
        return reduction
    return 1.0


def consecutive_loss_tripped(consecutive_losses: int, limit: int) -> bool:
    """True when the consecutive-loss counter has reached the pause threshold."""
    return consecutive_losses >= limit


def count_leading_losses(closed_pnls_desc: list[float]) -> int:
    """Count consecutive losing trades from the most recent backwards (stop at a win)."""
    n = 0
    for p in closed_pnls_desc:
        if p <= 0:
            n += 1
        else:
            break
    return n
