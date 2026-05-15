"""
Manual Slack notification trigger — run locally or on Railway to test webhooks.

Usage:
    python test_notify.py

Sends a test health report to #trading-health and a test alert to #trading-alerts.
Prints the HTTP status of every POST so you can confirm Slack is receiving messages.
"""
import asyncio
import datetime as _dt
import os
import sys

from dotenv import load_dotenv
load_dotenv()

import pytz
import requests

def _check_webhooks():
    names = [
        "SLACK_ALERTS_WEBHOOK",
        "SLACK_DECISIONS_WEBHOOK",
        "SLACK_HEALTH_WEBHOOK",
        "SLACK_PERFORMANCE_WEBHOOK",
    ]
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        print(f"ERROR: missing env vars: {missing}")
        print("Set them in .env (local) or Railway → Variables (production).")
        sys.exit(1)

def _post(label: str, url: str, payload: dict):
    try:
        resp = requests.post(url, json=payload, timeout=10)
        print(f"  {label}: HTTP {resp.status_code}" + ("" if resp.status_code == 200 else f" — {resp.text[:200]}"))
    except Exception as e:
        print(f"  {label}: EXCEPTION — {e}")

async def main():
    print("=== Slack Notification Manual Trigger ===\n")

    # Timezone diagnostic
    now_sys  = _dt.datetime.now()
    now_utc  = _dt.datetime.now(_dt.timezone.utc)
    now_est  = _dt.datetime.now(pytz.timezone("America/New_York"))
    print(f"System clock : {now_sys}")
    print(f"UTC          : {now_utc}")
    print(f"EST          : {now_est}")
    print()

    _check_webhooks()

    from config import Config

    print("Sending test health report to #trading-health...")
    _post(
        "SLACK_HEALTH_WEBHOOK",
        Config.SLACK_HEALTH_WEBHOOK,
        {"text": f"🏥 Health: uptime 0:01:00 | equity $25,000 | 🟢 P&L $0.00 | buying power $24,975 (MANUAL TEST {now_est.strftime('%H:%M:%S %Z')})"},
    )

    print("Sending test alert to #trading-alerts...")
    _post(
        "SLACK_ALERTS_WEBHOOK",
        Config.SLACK_ALERTS_WEBHOOK,
        {"text": f"⚠️ Bot diagnostic test — Slack confirmed {now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}"},
    )

    print("\nDone. Check your Slack channels — both messages should have arrived.")

asyncio.run(main())
