import requests
import asyncio
from config import Config
from datetime import datetime
import pytz

async def _post_to_slack(webhook_url, payload):
    """Internal method to post asynchronously to a webhook without blocking the main loop."""
    if not webhook_url:
        return # Silently ignore if webhook isn't configured
        
    def _do_post():
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"Failed to send Slack notification: {e}")
            
    await asyncio.to_thread(_do_post)

async def notify_trade_decision(symbol, strategy_name, signal_data):
    """Sends a detailed trade decision to the #trading-decisions channel."""
    action = signal_data.get("signal", "unknown").upper()
    reasoning = signal_data.get("reasoning", "No specific reasoning provided.")
    
    payload = {
        "text": f"🚨 *TRADE SIGNAL: {action} {symbol}* 🚨",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📈 Trade Decision: {action} {symbol}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Strategy:*\n{strategy_name}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n{action}"}
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Reasoning:*\n```{reasoning}```"
                }
            }
        ]
    }
    
    # Include any extra details from the signal dictionary if present
    extra_fields = []
    if "stop_price" in signal_data:
        extra_fields.append({"type": "mrkdwn", "text": f"*Stop Loss:*\n${signal_data['stop_price']:.2f}"})
    if "target_price" in signal_data:
        extra_fields.append({"type": "mrkdwn", "text": f"*Take Profit:*\n${signal_data['target_price']:.2f}"})
    if "current_price" in signal_data:
        extra_fields.append({"type": "mrkdwn", "text": f"*Entry Price:*\n${signal_data['current_price']:.2f}"})
        
    if extra_fields:
        payload["blocks"].append({
            "type": "section",
            "fields": extra_fields
        })
        
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_trade_skipped(symbol, strategy_name, reason):
    """Sends a skipped trade notification to the #trading-decisions channel."""
    payload = {
        "text": f"⏭️ *TRADE SKIPPED: {symbol}*",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⏭️ *Skipped Trade:* {symbol}\n*Strategy:* {strategy_name}\n*Reason:* {reason}"
                }
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_alert(message, level="ERROR"):
    """Sends critical alerts and errors to the #trading-alerts channel."""
    emoji = "🔥" if level.upper() == "CRITICAL" else "⚠️"
    payload = {
        "text": f"{emoji} *{level} ALERT* {emoji}\n{message}"
    }
    await _post_to_slack(Config.SLACK_ALERTS_WEBHOOK, payload)

async def notify_daily_health(uptime_str, equity, buying_power, daily_pnl):
    """Sends the daily health report to the #trading-health channel."""
    pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
    payload = {
        "text": f"🏥 *Daily Bot Health Report*",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🏥 Daily Bot Health Report"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Uptime:*\n{uptime_str}"},
                    {"type": "mrkdwn", "text": f"*Equity:*\n${equity:,.2f}"},
                    {"type": "mrkdwn", "text": f"*Buying Power:*\n${buying_power:,.2f}"},
                    {"type": "mrkdwn", "text": f"*Daily PnL:*\n{pnl_emoji} ${daily_pnl:,.2f}"}
                ]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_HEALTH_WEBHOOK, payload)

async def notify_weekly_performance(equity, active_positions_count, weekly_pnl):
    """Sends the weekly performance report to the #trading-performance channel."""
    pnl_emoji = "🚀" if weekly_pnl >= 0 else "🔻"
    payload = {
        "text": f"📊 *Weekly Performance Report*",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📊 Weekly Performance Report"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Total Equity:*\n${equity:,.2f}"},
                    {"type": "mrkdwn", "text": f"*Active Positions:*\n{active_positions_count}"},
                    {"type": "mrkdwn", "text": f"*Weekly PnL:*\n{pnl_emoji} ${weekly_pnl:,.2f}"}
                ]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_PERFORMANCE_WEBHOOK, payload)
