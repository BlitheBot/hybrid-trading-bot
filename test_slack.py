"""Slack webhook diagnostic — run locally to confirm webhook config and connectivity."""
import os
import sys
import requests
from dotenv import load_dotenv
load_dotenv()

WEBHOOKS = {
    "SLACK_ALERTS_WEBHOOK":      os.getenv("SLACK_ALERTS_WEBHOOK"),
    "SLACK_DECISIONS_WEBHOOK":   os.getenv("SLACK_DECISIONS_WEBHOOK"),
    "SLACK_HEALTH_WEBHOOK":      os.getenv("SLACK_HEALTH_WEBHOOK"),
    "SLACK_PERFORMANCE_WEBHOOK": os.getenv("SLACK_PERFORMANCE_WEBHOOK"),
}

print("=== Slack Webhook Config Check ===")
all_set = True
for name, url in WEBHOOKS.items():
    if url:
        masked = url[:30] + "..." + url[-10:] if len(url) > 40 else url
        print(f"  {name}: SET — {masked}")
    else:
        print(f"  {name}: *** NOT SET ***")
        all_set = False

if not all_set:
    print("\nERROR: One or more Slack webhooks are not configured.")
    print("Set them in your .env file (local) or Railway environment variables (production).")
    sys.exit(1)

print("\n=== Sending test messages ===")
payload = {"text": "Bot diagnostic test — Slack connection confirmed"}
failed = []
for name, url in WEBHOOKS.items():
    try:
        resp = requests.post(url, json=payload, timeout=10)
        status = resp.status_code
        if status == 200:
            print(f"  {name}: OK (HTTP {status})")
        else:
            print(f"  {name}: FAILED (HTTP {status}) — {resp.text[:200]}")
            failed.append(name)
    except Exception as e:
        print(f"  {name}: EXCEPTION — {e}")
        failed.append(name)

if failed:
    print(f"\nFailed webhooks: {failed}")
    sys.exit(1)
else:
    print("\nAll webhooks succeeded — check your Slack channels for the test messages.")
