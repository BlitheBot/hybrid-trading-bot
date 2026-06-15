#!/usr/bin/env python3
"""
Issue 1 live cleanup: find and cancel orphaned OCO orders.

An OCO order is orphaned if it is a BUY limit or BUY stop order and there is
no corresponding open SHORT position for that symbol (the short it was
protecting has already been closed or covered, but the OCO leg was never
cancelled).

Run from the repo root with the .env loaded:
    python cancel_orphaned_oco.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from config import Config

if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
    print("[Cleanup] ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set — aborting")
    sys.exit(1)

client = TradingClient(
    api_key=Config.ALPACA_API_KEY,
    secret_key=Config.ALPACA_SECRET_KEY,
    paper=Config.PAPER_TRADING,
    url_override=Config.ALPACA_BASE_URL,
)

# 1. Query open positions → symbols with an active short
positions = client.get_all_positions()
short_syms = {
    p.symbol
    for p in positions
    if str(getattr(p, "side", "")).lower() == "short"
}
long_syms = {
    p.symbol
    for p in positions
    if str(getattr(p, "side", "")).lower() == "long"
}
print(f"Open short positions ({len(short_syms)}): {sorted(short_syms) or 'none'}")
print(f"Open long  positions ({len(long_syms)}):  {sorted(long_syms)  or 'none'}")

# 2. Query all open orders
open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
print(f"\nTotal open orders: {len(open_orders)}")
for o in open_orders:
    otype = str(getattr(o, "order_type", "?")).lower()
    side  = str(getattr(o, "side",       "?")).lower()
    qty   = getattr(o, "qty", "?")
    lp    = getattr(o, "limit_price", None)
    sp    = getattr(o, "stop_price",  None)
    price = lp if lp else sp if sp else "?"
    cls   = str(getattr(o, "order_class", "?")).lower()
    print(f"  [{side.upper()} {otype}] {o.symbol} @ {price} qty={qty} class={cls} id={str(o.id)[:16]}")

# 3. Find orphaned: open BUY limit/stop orders with no matching SHORT position
# Alpaca returns enum objects; use .value for the string comparison.
def _side_str(o):
    s = getattr(o, "side", None)
    return getattr(s, "value", str(s)).lower()

def _type_str(o):
    t = getattr(o, "order_type", None)
    return getattr(t, "value", str(t)).lower()

orphaned = []
for o in open_orders:
    if _side_str(o) != "buy":
        continue
    otype = _type_str(o)
    if not any(k in otype for k in ("limit", "stop")):
        continue
    if o.symbol not in short_syms:
        orphaned.append(o)

print(f"\nOrphaned OCO orders (BUY protection with no matching short): {len(orphaned)}")
for o in orphaned:
    otype = str(getattr(o, "order_type", "?")).lower()
    qty   = getattr(o, "qty", "?")
    lp    = getattr(o, "limit_price", None)
    sp    = getattr(o, "stop_price",  None)
    price = lp if lp else sp if sp else "?"
    print(f"  >>> ORPHANED: {o.symbol} | {otype} @ {price} qty={qty} id={str(o.id)[:16]}")

# 4. Cancel all orphaned orders
if not orphaned:
    print("\nNothing to cancel.")
else:
    print(f"\nCancelling {len(orphaned)} orphaned order(s)...")
    for o in orphaned:
        otype = str(getattr(o, "order_type", "?")).lower()
        qty   = getattr(o, "qty", "?")
        lp    = getattr(o, "limit_price", None)
        sp    = getattr(o, "stop_price",  None)
        price = lp if lp else sp if sp else "?"
        try:
            client.cancel_order_by_id(o.id)
            print(f"[Cleanup] Cancelled orphaned OCO for {o.symbol} — {otype} @ {price} qty={qty}")
        except Exception as e:
            print(f"[Cleanup] Failed to cancel order for {o.symbol} (id={str(o.id)[:16]}): {e}")
    print("Done.")
