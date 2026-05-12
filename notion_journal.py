"""
Notion trade journal — posts a new page to the configured Notion database
after every successful trade execution.

Silently skips if NOTION_API_KEY or NOTION_DATABASE_ID is not set.

The target Notion database must have these properties (exact names):
  Symbol          — Title
  Strategy Type   — Text
  Entry Price     — Number
  Entry Time      — Date
  Stop Price      — Number
  Target Price    — Number
  Position Size   — Number  (risk % of equity used for sizing)
  Market Regime   — Text
  Signal Source   — Text
  Reasoning       — Text
"""
from datetime import datetime

from config import Config


async def post_trade_to_notion(trade: dict) -> None:
    if not Config.NOTION_API_KEY or not Config.NOTION_DATABASE_ID:
        return
    try:
        from notion_client import AsyncClient

        entry_time = trade.get("entry_time")
        if isinstance(entry_time, datetime):
            entry_iso = entry_time.isoformat()
        elif entry_time:
            entry_iso = str(entry_time)
        else:
            entry_iso = datetime.utcnow().isoformat()

        reasoning = str(trade.get("reasoning", ""))[:2000]

        async with AsyncClient(auth=Config.NOTION_API_KEY) as client:
            await client.pages.create(
                parent={"database_id": Config.NOTION_DATABASE_ID},
                properties={
                    "Symbol": {
                        "title": [{"text": {"content": str(trade.get("symbol", ""))}}]
                    },
                    "Strategy Type": {
                        "rich_text": [{"text": {"content": str(trade.get("signal_type", ""))}}]
                    },
                    "Entry Price": {
                        "number": float(trade.get("entry_price", 0))
                    },
                    "Entry Time": {
                        "date": {"start": entry_iso}
                    },
                    "Stop Price": {
                        "number": float(trade.get("stop_price", 0))
                    },
                    "Target Price": {
                        "number": float(trade.get("target_price", 0))
                    },
                    "Position Size": {
                        "number": round(float(trade.get("position_size", 0)), 4)
                    },
                    "Market Regime": {
                        "rich_text": [{"text": {"content": str(trade.get("market_regime", ""))}}]
                    },
                    "Signal Source": {
                        "rich_text": [{"text": {"content": str(trade.get("signal_source", ""))}}]
                    },
                    "Reasoning": {
                        "rich_text": [{"text": {"content": reasoning}}]
                    },
                },
            )
        print(f"[Notion] Trade journal entry posted for {trade.get('symbol')}")
    except Exception as e:
        print(f"[Notion] Journal post failed for {trade.get('symbol', '?')}: {e}")
