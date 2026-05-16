import requests
import asyncio
import time
from config import Config
from datetime import datetime
import pytz

_bot_start_time = time.time()
SLACK_STARTUP_GRACE_SECONDS = 60


def _pagerduty_trigger(message: str) -> None:
    """Fire a PagerDuty phone alert. Silently skips if routing key not configured."""
    key = Config.PAGERDUTY_ROUTING_KEY
    if not key:
        return
    try:
        requests.post(
            "https://events.pagerduty.com/v2/enqueue",
            json={
                "routing_key": key,
                "event_action": "trigger",
                "payload": {
                    "summary": message,
                    "severity": "critical",
                    "source": "hybrid-trading-bot",
                },
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[PagerDuty] Failed to send alert: {e}")

_missing_webhook_logged: set[str] = set()

async def _post_to_slack(webhook_url, payload):
    """Post to a Slack webhook. Logs the HTTP status on every attempt."""
    if time.time() - _bot_start_time < SLACK_STARTUP_GRACE_SECONDS:
        print(f"[Slack] Startup grace period — suppressing message")
        return
    if not webhook_url:
        # Log once per unique missing URL to avoid log spam
        key = repr(webhook_url)
        if key not in _missing_webhook_logged:
            _missing_webhook_logged.add(key)
            print("[Slack] WARNING: webhook_url is not configured — Slack notification suppressed. "
                  "Set SLACK_*_WEBHOOK env vars in Railway / .env.")
        return

    masked = webhook_url[:35] + "..." if len(webhook_url) > 35 else webhook_url

    def _do_post():
        import time
        time.sleep(1)  # avoid Slack 429 rate limiting between consecutive webhook calls
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code == 200:
                print(f"[Slack] POST {masked} → HTTP {resp.status_code} OK")
            else:
                print(f"[Slack] POST {masked} → HTTP {resp.status_code} ERROR: {resp.text[:200]}")
        except Exception as e:
            print(f"[Slack] POST {masked} → EXCEPTION: {e}")

    await asyncio.to_thread(_do_post)

async def notify_trade_decision(symbol, strategy_name, signal_data, discovery_note=None):
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

    if discovery_note:
        payload["blocks"].append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":information_source: {discovery_note}"}]
        })

    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_trade_skipped(symbol, strategy_name, reason, critical=False):
    """Sends a skipped trade notification to the #trading-decisions channel.
    Set critical=True for VIX block, daily loss limit, or portfolio heat cap skips."""
    if not Config.SLACK_VERBOSE and not critical:
        return
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
    """Sends critical alerts to #trading-alerts; fires PagerDuty phone alert on CRITICAL."""
    if not Config.SLACK_VERBOSE and level.upper() == "INFO":
        return
    emoji = "🔥" if level.upper() == "CRITICAL" else "⚠️"
    payload = {
        "text": f"{emoji} *{level} ALERT* {emoji}\n{message}"
    }
    await _post_to_slack(Config.SLACK_ALERTS_WEBHOOK, payload)
    if level.upper() == "CRITICAL":
        await asyncio.to_thread(_pagerduty_trigger, message)

async def notify_daily_health(uptime_str, equity, buying_power, daily_pnl):
    """Sends the daily health report to the #trading-health channel.
    Always fires regardless of SLACK_VERBOSE — this is a critical operational signal."""
    pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
    print(f"[HealthReport] notify_daily_health firing — equity=${equity:,.0f} pnl={daily_pnl:+,.2f} uptime={uptime_str}")
    # Always send condensed one-liner; SLACK_VERBOSE does not suppress this report
    payload = {"text": f"🏥 Health: uptime {uptime_str} | equity ${equity:,.0f} | {pnl_emoji} P&L ${daily_pnl:+,.2f} | buying power ${buying_power:,.0f}"}
    await _post_to_slack(Config.SLACK_HEALTH_WEBHOOK, payload)
    if not Config.SLACK_VERBOSE:
        return
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

async def notify_news_signal(ticker, headline, sentiment, score, action):
    """Sends a Benzinga news sentiment signal to #trading-decisions (alert only, no trade yet)."""
    if not Config.SLACK_VERBOSE:
        return
    payload = {
        "text": f"📰 *NEWS SIGNAL: {action.upper()} {ticker}* (Score: {score:.1f})",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📰 News Signal: {action.upper()} {ticker}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Sentiment:*\n{sentiment.capitalize()}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{score:.1f}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n{action.upper()}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Headline:*\n```{headline}```"}
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_news_trade(ticker, headline, direction, entry_price, position_size):
    """Sends a Benzinga news-triggered trade execution to #trading-alerts."""
    payload = {
        "text": f"📰🚀 *NEWS TRADE EXECUTED: {direction.upper()} {ticker}*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📰🚀 News Trade: {direction.upper()} {ticker}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Direction:*\n{direction.upper()}"},
                    {"type": "mrkdwn", "text": f"*Entry Price:*\n${entry_price:.2f}"},
                    {"type": "mrkdwn", "text": f"*Position Size:*\n{position_size} shares"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Trigger Headline:*\n```{headline}```"}
            }
        ]
    }
    await _post_to_slack(Config.SLACK_ALERTS_WEBHOOK, payload)

async def notify_edgar_signal(ticker, headline, sentiment, strength, action):
    """Sends a SEC EDGAR insider trade signal to #trading-decisions with 📋 emoji."""
    if not Config.SLACK_VERBOSE:
        return
    sentiment_emoji = "🟢" if sentiment == "bullish" else "🔴"
    payload = {
        "text": f"📋 *EDGAR INSIDER: {action.upper()} {ticker}* (Strength: {strength:.1f})",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📋 SEC EDGAR Insider: {action.upper()} {ticker}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Sentiment:*\n{sentiment_emoji} {sentiment.capitalize()}"},
                    {"type": "mrkdwn", "text": f"*Strength:*\n{strength:.1f}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n{action.upper()}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Filing:*\n```{headline}```"}
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_truth_social_signal(post_text, tickers, sentiment, score, action):
    """Sends a Truth Social sentiment signal to #trading-decisions (alert only, no trade yet)."""
    if not Config.SLACK_VERBOSE:
        return
    ticker_str = ", ".join(tickers) if tickers else "N/A"
    payload = {
        "text": f"🇺🇸 *TRUTH SOCIAL SIGNAL: {action.upper()} {ticker_str}* (Score: {score:.1f})",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🇺🇸 Truth Social Signal: {action.upper()}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Affected Tickers:*\n{ticker_str}"},
                    {"type": "mrkdwn", "text": f"*Sentiment:*\n{sentiment.capitalize()}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{score:.1f}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n{action.upper()}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Post:*\n```{post_text[:500]}```"}
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_macro_summary(snapshot: dict):
    """Sends the weekly FRED macro summary to #trading-health every Sunday 7 PM EST."""
    if not Config.SLACK_VERBOSE:
        return
    def _fmt(label: str, key: str, prev_key: str, unit: str = "%") -> str:
        val  = snapshot.get(key)
        prev = snapshot.get(prev_key)
        val_str = f"{val:.2f}{unit}" if val is not None else "N/A"
        if val is not None and prev is not None:
            delta = val - prev
            sign  = "+" if delta >= 0 else ""
            delta_str = f"({sign}{delta:.2f}{unit} WoW)"
        else:
            delta_str = "(N/A WoW)"
        return f"*{label}:*\n{val_str} {delta_str}"

    vix = snapshot.get("vix")
    regime_note = ""
    if vix is not None:
        if vix > 40:
            regime_note = "\n⚠️ *EXTREME FEAR* — VIX > 40. Auto-trade conviction at 0.7×."
        elif vix > 30:
            regime_note = "\n⚠️ *Elevated Fear* — VIX > 30. Auto-trade conviction at 0.7×."

    flags = []
    if snapshot.get("fed_rate_cut"):
        flags.append("🟢 Fed rate cut detected (bullish macro)")
    if snapshot.get("yield_rising_fast"):
        flags.append("🔴 10Y yield rising >0.2% in 30 days (growth concern)")
    if snapshot.get("vix_extreme_fear"):
        flags.append("🔴 VIX extreme fear (>40)")
    flags_text = "\n".join(flags) if flags else "None"

    payload = {
        "text": "📊 *Weekly Macro Summary — FRED Data*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📊 Weekly Macro Summary — FRED Data"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": _fmt("Fed Funds Rate", "fed_funds_rate", "prev_week_fed_funds")},
                    {"type": "mrkdwn", "text": _fmt("VIX", "vix", "prev_week_vix", "")},
                    {"type": "mrkdwn", "text": _fmt("10Y Treasury", "treasury_10y", "prev_week_treasury")},
                    {"type": "mrkdwn", "text": _fmt("Unemployment", "unemployment", "prev_week_unemployment")},
                    {"type": "mrkdwn", "text": _fmt("CPI YoY", "cpi_yoy", "prev_week_cpi_yoy")},
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Active Macro Flags:*\n{flags_text}{regime_note}"
                }
            }
        ]
    }
    await _post_to_slack(Config.SLACK_HEALTH_WEBHOOK, payload)

async def notify_congressional_signal(ticker, headline, representative, party, chamber,
                                       amount_range, transaction_type, strength, action,
                                       informational=False):
    """Sends a congressional trade signal to #trading-decisions.
    Buys use emoji. Informational sells use warning emoji with explicit label.
    Suppressed when SLACK_VERBOSE=False."""
    if not Config.SLACK_VERBOSE:
        return
    if informational:
        emoji = "⚠️"
        header_text = f"Congressional SELL — Informational: {ticker}"
        action_label = "INFORMATIONAL SELL"
    else:
        emoji = "🏛️"
        header_text = f"Congressional BUY: {ticker}"
        action_label = action.upper()

    payload = {
        "text": f"{emoji} *CONGRESSIONAL TRADE: {action_label} {ticker}* (Strength: {strength:.1f})",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {header_text}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Representative:*\n{representative}"},
                    {"type": "mrkdwn", "text": f"*Party / Chamber:*\n{party} / {chamber}"},
                    {"type": "mrkdwn", "text": f"*Transaction:*\n{transaction_type}"},
                    {"type": "mrkdwn", "text": f"*Amount:*\n{amount_range}"},
                    {"type": "mrkdwn", "text": f"*Strength:*\n{strength:.1f}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Detail:*\n```{headline}```"}
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)

async def notify_market_open(equity: float, watchlist: str, regime: str):
    """Sends a morning briefing to #trading-alerts at 9:30 AM EST market open."""
    if not Config.SLACK_VERBOSE:
        return
    regime_emoji = "🐂" if regime == "bull" else ("🐻" if regime == "bear" else "⚖️")
    payload = {
        "text": "🔔 *Market Open — Morning Briefing*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🔔 Market Open — Morning Briefing"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Equity:*\n${equity:,.2f}"},
                    {"type": "mrkdwn", "text": f"*Market Regime:*\n{regime_emoji} {regime.capitalize()}"},
                    {"type": "mrkdwn", "text": f"*Swing Watchlist:*\n{watchlist}"},
                    {"type": "mrkdwn", "text": "*Next Event:*\nSwing evaluation at 10:30 AM EST"},
                ]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_ALERTS_WEBHOOK, payload)

async def notify_correlation_heatmap(image_url: str):
    """Sends the weekly signal correlation heatmap to #trading-health."""
    if not Config.SLACK_VERBOSE:
        return
    payload = {
        "text": "📊 *Weekly Signal Correlation Heatmap*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📊 Weekly Signal Correlation Heatmap"}
            },
            {
                "type": "image",
                "image_url": image_url,
                "alt_text": "Signal P&L% correlation across strategy types",
            },
        ]
    }
    await _post_to_slack(Config.SLACK_HEALTH_WEBHOOK, payload)


async def notify_discovery_progress(elapsed_min: int):
    """Sends an hourly progress ping while Discovery Engine v2 subprocess is running."""
    if not Config.SLACK_VERBOSE:
        return
    payload = {
        "text": f":mag: *Discovery Engine v2 running* — {elapsed_min}m elapsed. Full report on completion."
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)


async def notify_discovery_report(report_text: str):
    """Sends the Discovery Engine v2 completion brief to #trading-decisions."""
    payload = {"text": report_text}
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)


async def notify_reddit_signal(ticker: str, score: float, mention_count: int,
                               subreddits: list[str], sample_titles: list[str]):
    """Sends a Reddit momentum signal to #trading-decisions (alert-only, 🤖 emoji)."""
    if not Config.SLACK_VERBOSE:
        return
    subs_str = " + ".join(f"r/{s}" for s in subreddits)
    titles_block = "\n".join(f"• {t[:120]}" for t in sample_titles[:3])
    payload = {
        "text": f"🤖 *REDDIT SIGNAL: {ticker}* (Score: {score:.1f}, {mention_count} mentions)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🤖 Reddit Momentum: {ticker}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{score:.1f}"},
                    {"type": "mrkdwn", "text": f"*Mentions:*\n{mention_count}"},
                    {"type": "mrkdwn", "text": f"*Subreddits:*\n{subs_str}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Sample Posts:*\n{titles_block}"}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ":information_source: Alert-only — Reddit signals do not auto-trade"}]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)


async def notify_weekly_performance_brain(stats: dict):
    """Sends the weekly Performance Brain digest to #trading-performance every Sunday 6 PM EST."""
    total = stats.get("total_trades", 0)
    if total == 0:
        return

    best_type  = stats.get("best_signal_type")  or "—"
    best_ev    = stats.get("best_ev")
    worst_type = stats.get("worst_signal_type") or "—"
    worst_ev   = stats.get("worst_ev")
    best_day   = stats.get("best_day", "—")
    best_day_wr = stats.get("best_day_win_rate", 0.0)

    best_ev_str  = f"{best_ev:+.2f}%"  if best_ev  is not None else "—"
    worst_ev_str = f"{worst_ev:+.2f}%" if worst_ev is not None else "—"

    avg_win  = stats.get("overall_avg_win",  0.0)
    avg_loss = stats.get("overall_avg_loss", 0.0)
    ratio    = stats.get("overall_ratio")
    avg_win_str  = f"+{avg_win:.2f}%"  if avg_win  else "—"
    avg_loss_str = f"-{avg_loss:.2f}%" if avg_loss else "—"
    ratio_str    = f"{ratio:.2f}x"     if ratio is not None else "—"

    payload = {
        "text": "🧠 *Weekly Performance Brain Digest*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🧠 Weekly Performance Brain Digest"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Trades This Week:*\n{total}"},
                    {"type": "mrkdwn", "text": f"*Best Strategy:*\n{best_type} (EV {best_ev_str})"},
                    {"type": "mrkdwn", "text": f"*Worst Strategy:*\n{worst_type} (EV {worst_ev_str})"},
                    {"type": "mrkdwn", "text": f"*Best Entry Day:*\n{best_day} ({best_day_wr:.1f}% WR)"},
                    {"type": "mrkdwn", "text": f"*Avg Win | Avg Loss | Ratio:*\n{avg_win_str} | {avg_loss_str} | {ratio_str}"},
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ":information_source: Performance Brain uses last 20 trades per strategy to scale next week's position sizes: WR >60% → +20%, WR <40% → −30%, floor at 10% of normal size."}]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_PERFORMANCE_WEBHOOK, payload)


async def notify_market_close_digest(
    trades_today: int,
    daily_pnl_pct: float,
    signals_total: int,
    vix: float | None,
    regime: str,
    cooldown_symbols: list[str],
):
    """Sends the 4pm daily market close summary to #trading-health.
    Suppressed when SLACK_VERBOSE=False and no trades occurred today."""
    if not Config.SLACK_VERBOSE and trades_today == 0:
        return
    pnl_sign  = "+" if daily_pnl_pct >= 0 else ""
    pnl_emoji = "🟢" if daily_pnl_pct >= 0 else "🔴"
    vix_str   = f"{vix:.1f}" if vix is not None else "N/A"
    cooldown_str = ", ".join(cooldown_symbols) if cooldown_symbols else "None"
    regime_emoji = "🐂" if regime == "bull" else ("🐻" if regime == "bear" else "⚖️")
    payload = {
        "text": "📊 *4pm Market Close Summary*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📊 4pm Market Close Summary"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Trades Today:*\n{trades_today}"},
                    {"type": "mrkdwn", "text": f"*Daily P&L:*\n{pnl_emoji} {pnl_sign}{daily_pnl_pct:.2f}%"},
                    {"type": "mrkdwn", "text": f"*Signals Fired (total):*\n{signals_total}"},
                    {"type": "mrkdwn", "text": f"*VIX:*\n{vix_str}"},
                    {"type": "mrkdwn", "text": f"*Market Regime:*\n{regime_emoji} {regime.capitalize()}"},
                    {"type": "mrkdwn", "text": f"*Symbols on Cooldown:*\n{cooldown_str}"},
                ],
            },
        ],
    }
    await _post_to_slack(Config.SLACK_HEALTH_WEBHOOK, payload)


async def notify_grok_signal(coin: str, sentiment: str, score: int, confidence: int,
                              reasoning: str, theme: str):
    """Sends a Grok X/Twitter crypto sentiment alert to #trading-decisions (alert-only)."""
    if not Config.SLACK_VERBOSE:
        return
    sentiment_emoji = "🟢" if sentiment == "bullish" else ("🔴" if sentiment == "bearish" else "⚪")
    payload = {
        "text": f"🐦 *GROK X SENTIMENT: {sentiment.upper()} {coin}* (Score: {score}/10)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🐦 Grok X/Twitter: {sentiment.upper()} {coin}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Coin:*\n{coin}"},
                    {"type": "mrkdwn", "text": f"*Sentiment:*\n{sentiment_emoji} {sentiment.capitalize()}"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{score}/10"},
                    {"type": "mrkdwn", "text": f"*Confidence:*\n{confidence}/10"},
                    {"type": "mrkdwn", "text": f"*Dominant Theme:*\n{theme}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*X/Twitter Narrative:*\n```{reasoning}```"}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ":information_source: Alert-only — Grok signals do not auto-trade"}]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)


async def notify_webull_signal(ticker: str, rank: int, change_pct: float,
                                score: float, reasoning: str):
    """Sends a Webull contrarian retail-crowding alert to #trading-decisions (alert-only)."""
    if not Config.SLACK_VERBOSE:
        return
    payload = {
        "text": f"📉 *WEBULL CONTRARIAN: BEARISH {ticker}* (+{change_pct:.1f}% — retail crowding)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📉 Webull Contrarian Signal: {ticker}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Retail Rank:*\n#{rank}"},
                    {"type": "mrkdwn", "text": f"*Intraday Gain:*\n+{change_pct:.1f}%"},
                    {"type": "mrkdwn", "text": f"*Contrarian Score:*\n{score:.1f}/10"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Reasoning:*\n```{reasoning}```"}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ":information_source: Alert-only — Webull contrarian signals do not auto-trade"}]
            }
        ]
    }
    await _post_to_slack(Config.SLACK_DECISIONS_WEBHOOK, payload)


async def notify_truth_social_trade(ticker, post_text, direction, entry_price, position_size):
    """Sends a Truth Social-triggered trade execution to #trading-alerts."""
    payload = {
        "text": f"🇺🇸🚀 *TRUTH SOCIAL TRADE EXECUTED: {direction.upper()} {ticker}*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🇺🇸🚀 Truth Social Trade: {direction.upper()} {ticker}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticker:*\n{ticker}"},
                    {"type": "mrkdwn", "text": f"*Direction:*\n{direction.upper()}"},
                    {"type": "mrkdwn", "text": f"*Entry Price:*\n${entry_price:.2f}"},
                    {"type": "mrkdwn", "text": f"*Position Size:*\n{position_size} shares"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Trigger Post:*\n```{post_text[:500]}```"}
            }
        ]
    }
    await _post_to_slack(Config.SLACK_ALERTS_WEBHOOK, payload)


async def notify_weekly_macro_brief(brief_text: str, citations: list):
    """Sends the Friday AI-generated macro brief to #trading-health. Always fires (not SLACK_VERBOSE gated)."""
    source_lines = "\n".join(
        f"  • <{c['url']}|{c['title'] or c['url']}>" for c in citations[:4]
    )
    full_text = brief_text
    if source_lines:
        full_text += f"\n\n*Sources:*\n{source_lines}"
    payload = {
        "text": "🌐 *Friday Macro Brief (AI + Web Search)*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🌐 Friday Macro Brief"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": full_text[:2800]},
            },
        ],
    }
    await _post_to_slack(Config.SLACK_HEALTH_WEBHOOK, payload)
