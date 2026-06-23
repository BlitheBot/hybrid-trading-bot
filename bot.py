import os
import sys
import hashlib
import hmac
import time
import asyncio
import threading
from collections import deque
from datetime import datetime, timedelta
import pytz
import pandas as pd
from flask import Flask, jsonify, request
import notifications
import notion_journal
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

import requests as _requests
from llm_client import call_llm, call_llm_with_model, LLMError, MODEL_FLASH, log_model_config

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame

from sqlalchemy import create_engine, text as sql_text

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.smb_strategy import SMBStrategy
from strategies.crypto_momentum_strategy import CryptoMomentumStrategy
from strategies.swing_strategy import SwingStrategy
from strategies.bollinger_mean_reversion_strategy import BollingerMeanReversionStrategy
from strategies.news_strategy import NewsStrategy, _get_scan_sleep_seconds, get_sentiment_score
from strategies.truth_social_strategy import TruthSocialStrategy
from strategies.sec_edgar_strategy import SECEdgarStrategy
from strategies.congressional_trading_strategy import CongressionalTradingStrategy
from strategies.fred_strategy import FREDStrategy, get_conviction_multiplier, MACRO_SNAPSHOT
from strategies.correlation_guard import CorrelationGuard
from strategies.short_interest_signal import ShortInterestSignal
from strategies.grok_strategy import GrokStrategy
from strategies.webull_strategy import WebullStrategy
from discovery.regime_adapter import apply_to_swing_strategy
from discovery.regime_classifier import (
    classify_regime, BULL_TREND, BEAR_TREND, HIGH_VOL, CHOPPY,
    compute_cross_asset_signals, regime_confidence,
)
from discovery.decay_monitor import StrategyDecayMonitor
from discovery.grok_sentiment import refresh_grok_sentiment, get_grok_sentiment
from utils import get_historical_bars, get_finnhub_price
import signal_quality
import performance_brain
import risk_limits
try:
    from discovery.strategies.smc_strategy import detect_order_blocks, detect_fair_value_gaps
    _SMC_AVAILABLE = True
except Exception:
    _SMC_AVAILABLE = False

# ── Sentry error monitoring (optional — omit SENTRY_DSN to disable) ─────────
try:
    import sentry_sdk as _sentry_sdk
    if Config.SENTRY_DSN:
        _sentry_sdk.init(dsn=Config.SENTRY_DSN, traces_sample_rate=0.1)
        print("🔍 Sentry error monitoring initialized")
except ImportError:
    pass

# ── Flask Health Endpoint ────────────────────────────────────────────
_health_app = Flask(__name__)
_bot_start_time = datetime.now(pytz.utc)

# Updated by bot loops so the /health endpoint reflects live state
_health_state: dict = {
    "db_connected": False,
    "alpaca_connected": False,
    "last_news_scan_utc": None,
    "last_edgar_scan_utc": None,
    "crypto_polling_active": False,
    "equity_usd": 0.0,
    "open_positions": 0,
    "daily_pnl_pct": 0.0,
    "signals_fired_total": 0,
    "market_regime": "unknown",
    "last_health_report_utc": None,
    "claude_api_calls_today": 0,
}

# Set by /pause slash command; cleared by /resume. Checked by all trade-execution paths.
_bot_paused: bool = False

# Set by TradingBot.__init__; gives the Flask slash-command handlers access to trading_client.
_bot_instance = None

@_health_app.route("/health", methods=["GET"])
def health_check():
    uptime_seconds = (datetime.now(pytz.utc) - _bot_start_time).total_seconds()
    return jsonify({
        "status": "running",
        "uptime_seconds": round(uptime_seconds, 2),
        "started_at": _bot_start_time.isoformat(),
        "db_connected": _health_state["db_connected"],
        "alpaca_connected": _health_state["alpaca_connected"],
        "last_news_scan": _health_state["last_news_scan_utc"],
        "last_edgar_scan": _health_state["last_edgar_scan_utc"],
        "crypto_polling_active": _health_state["crypto_polling_active"],
    }), 200

@_health_app.route("/metrics", methods=["GET"])
def prometheus_metrics():
    from flask import Response, abort
    if not Config.PROMETHEUS_ENABLED:
        abort(404)
    uptime = round((datetime.now(pytz.utc) - _bot_start_time).total_seconds(), 2)
    vix    = MACRO_SNAPSHOT.get("vix") or 0.0
    polling = 1 if _health_state["crypto_polling_active"] else 0
    lines = [
        "# HELP bot_uptime_seconds Seconds since bot startup",
        "# TYPE bot_uptime_seconds gauge",
        f"bot_uptime_seconds {uptime}",
        "# HELP bot_equity_usd Current account equity in USD",
        "# TYPE bot_equity_usd gauge",
        f"bot_equity_usd {_health_state['equity_usd']:.2f}",
        "# HELP bot_open_positions Number of open positions",
        "# TYPE bot_open_positions gauge",
        f"bot_open_positions {_health_state['open_positions']}",
        "# HELP bot_daily_pnl_pct Daily P&L as percentage of start-of-day equity",
        "# TYPE bot_daily_pnl_pct gauge",
        f"bot_daily_pnl_pct {_health_state['daily_pnl_pct']:.4f}",
        "# HELP bot_vix_level VIX level from FRED",
        "# TYPE bot_vix_level gauge",
        f"bot_vix_level {float(vix):.2f}",
        "# HELP bot_crypto_polling_active 1 if crypto REST polling is active",
        "# TYPE bot_crypto_polling_active gauge",
        f"bot_crypto_polling_active {polling}",
        "# HELP bot_signals_fired_total Confirmed buy executions since startup",
        "# TYPE bot_signals_fired_total counter",
        f"bot_signals_fired_total {_health_state['signals_fired_total']}",
        "",
    ]
    return Response(
        "\n".join(lines),
        mimetype="text/plain; version=0.0.4; charset=utf-8",
    )


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Returns True if the request is legitimately from Slack, or if no signing secret is set."""
    secret = Config.SLACK_SIGNING_SECRET
    if not secret:
        return True  # development mode — skip verification
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False  # reject replays older than 5 minutes
    except (ValueError, TypeError):
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        secret.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@_health_app.route("/slack/commands", methods=["POST"])
def slack_commands():
    global _bot_paused  # must precede any read of _bot_paused in this function
    body         = request.get_data()
    ts           = request.headers.get("X-Slack-Request-Timestamp", "")
    sig          = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, ts, sig):
        return jsonify({"text": "Invalid request signature."}), 403

    command      = request.form.get("command", "")
    user         = request.form.get("user_name", "unknown")
    text         = request.form.get("text", "").strip()
    response_url = request.form.get("response_url", "")

    if command == "/status":
        pnl_sign = "+" if _health_state["daily_pnl_pct"] >= 0 else ""
        vix      = MACRO_SNAPSHOT.get("vix") or 0.0
        regime   = _health_state.get("market_regime", "unknown")
        paused   = "⏸ *PAUSED*" if _bot_paused else "▶ Running"
        status_text = (
            f"*Bot Status*\n"
            f"• State: {paused}\n"
            f"• Equity: ${_health_state['equity_usd']:,.2f}\n"
            f"• Open Positions: {_health_state['open_positions']}\n"
            f"• Daily P&L: {pnl_sign}{_health_state['daily_pnl_pct']:.2f}%\n"
            f"• VIX: {float(vix):.1f}\n"
            f"• Market Regime: {regime.capitalize()}"
        )
        return jsonify({"text": status_text})

    if command == "/pause":
        _bot_paused = True
        print(f"[SlashCmd] Bot PAUSED by @{user}")
        return jsonify({
            "response_type": "in_channel",
            "text": f"⏸ *Bot paused* by @{user} — no new trades will be placed until `/resume`.",
        })

    if command == "/resume":
        _bot_paused = False
        print(f"[SlashCmd] Bot RESUMED by @{user}")
        return jsonify({
            "response_type": "in_channel",
            "text": f"▶ *Bot resumed* by @{user} — normal trading has resumed.",
        })

    if command == "/buy":
        parts = text.split()
        if len(parts) != 2:
            return jsonify({"text": "Usage: `/buy SYMBOL SHARES` — e.g. `/buy COST 10`"}), 200
        symbol = parts[0].upper()
        try:
            shares = int(parts[1])
            if shares <= 0:
                raise ValueError
        except ValueError:
            return jsonify({"text": "SHARES must be a positive integer."}), 200

        if _bot_paused:
            return jsonify({"text": "⏸ Bot is paused — use `/resume` first."}), 200
        if _bot_instance is not None and _bot_instance.trading_halted_for_day:
            return jsonify({"text": "🛑 Trading halted for today (daily loss limit reached)."}), 200
        if _bot_instance is None:
            return jsonify({"text": "Bot not initialised yet — try again in a moment."}), 200

        def _submit_buy():
            try:
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce
                order = _bot_instance.trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol,
                    qty=shares,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                ))
                print(f"[SlashCmd] /buy {symbol} {shares} by @{user} → order {order.id}")
                if response_url:
                    _requests.post(response_url, json={
                        "response_type": "in_channel",
                        "text": (
                            f"✅ *BUY {symbol}* — {shares} shares @ market · "
                            f"order `{order.id}` by @{user}"
                        ),
                    }, timeout=5)
            except Exception as exc:
                print(f"[SlashCmd] /buy {symbol} error: {exc}")
                if response_url:
                    _requests.post(response_url, json={
                        "text": f"❌ Buy order failed for *{symbol}*: {exc}"
                    }, timeout=5)

        threading.Thread(target=_submit_buy, daemon=True).start()
        return jsonify({
            "text": f"Buying *{shares}* shares of *{symbol}* at market. Order submitted."
        }), 200

    if command == "/sell":
        symbol = text.split()[0].upper() if text else ""
        if not symbol:
            return jsonify({"text": "Usage: `/sell SYMBOL` — e.g. `/sell COST`"}), 200
        if _bot_instance is None:
            return jsonify({"text": "Bot not initialised yet — try again in a moment."}), 200

        # Check position synchronously — fast Alpaca call, well within 3-second timeout
        try:
            position = _bot_instance.trading_client.get_open_position(symbol)
            qty_shares = abs(float(position.qty))
            qty_str    = f"{qty_shares:.0f}"
        except Exception:
            return jsonify({"text": f"No open position for *{symbol}*."}), 200

        def _submit_sell():
            try:
                _bot_instance.trading_client.close_position(symbol)
                print(f"[SlashCmd] /sell {symbol} ({qty_str} shares) by @{user}")
                if response_url:
                    _requests.post(response_url, json={
                        "response_type": "in_channel",
                        "text": (
                            f"✅ *SELL {symbol}* — {qty_str} shares @ market · "
                            f"position closed by @{user}"
                        ),
                    }, timeout=5)
            except Exception as exc:
                print(f"[SlashCmd] /sell {symbol} error: {exc}")
                if response_url:
                    _requests.post(response_url, json={
                        "text": f"❌ Sell order failed for *{symbol}*: {exc}"
                    }, timeout=5)

        threading.Thread(target=_submit_sell, daemon=True).start()
        return jsonify({
            "text": f"Selling *{qty_str}* shares of *{symbol}* at market. Order submitted."
        }), 200

    if command == "/help":
        help_text = (
            "*Available Slash Commands*\n"
            "• `/status` — equity, positions, daily P&L, VIX, market regime\n"
            "• `/buy SYMBOL SHARES` — submit a market buy order\n"
            "• `/sell SYMBOL` — close your full open position\n"
            "• `/pause` — halt all new trade execution immediately\n"
            "• `/resume` — resume trading after a pause\n"
            "• `/help` — show this message"
        )
        return jsonify({"text": help_text})

    return jsonify({"text": f"Unknown command `{command}`. Try `/help`."}), 200


def start_health_server(port: int | None = None):
    """
    Run the Flask health/slash server in a daemon thread.
    Port defaults to HEALTH_PORT env var, then 8502.

    Railway setup: add a second public domain pointing to port 8502 (or HEALTH_PORT),
    then register that URL as the Slack slash-command Request URL in the Slack app config.
    """
    if port is None:
        port = int(os.environ.get("HEALTH_PORT", 8502))
    thread = threading.Thread(
        target=lambda: _health_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True
    )
    thread.start()
    print(f"🩺 Health/slash endpoint: http://0.0.0.0:{port}/health  (HEALTH_PORT={port})")
    print(f"   Slack slash commands:  POST :{port}/slack/commands")


# Hardcoded GICS sector mapping for current SWING_SYMBOLS.
# TODO Phase 4: extend to full S&P 500 mapping and wire into news/EDGAR auto-trade loops.
_SECTOR_MAP: dict[str, str] = {
    "JPM":   "Financials",
    "BRK.B": "Financials",
    "V":     "Financials",
    "COST":  "Consumer Staples",
    "PG":    "Consumer Staples",
    "SPY":   "Market",
}


class TradingBot:
    def __init__(self):
        global _bot_instance
        _bot_instance = self
        print("DEBUG: Initializing TradingBot...")
        
        # Determine Base URL — env-driven so Railway can override without a code deploy
        base_url = Config.ALPACA_BASE_URL
        _key = Config.ALPACA_API_KEY or ""
        _key_prefix = _key[:6] if len(_key) >= 6 else repr(_key)
        _mode = "PAPER" if Config.PAPER_TRADING else "LIVE"
        print(f"DEBUG: Alpaca {_mode} | key prefix={_key_prefix} | url={base_url}")
        if not _key.startswith("PK") and Config.PAPER_TRADING:
            print("WARNING: PAPER_TRADING=True but key does not start with 'PK' — orders may route incorrectly")
        if not _key.startswith("AK") and not Config.PAPER_TRADING:
            print("WARNING: PAPER_TRADING=False but key does not start with 'AK' — you may be using a paper key on live")

        self.trading_client = TradingClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
            paper=Config.PAPER_TRADING,
            url_override=base_url
        )
        self.stock_data_client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY
        )
        self.crypto_data_client = CryptoHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY
        )
        
        self._crypto_poll_cooldowns: dict[str, datetime] = {}
        self.scalp_strategies = []
        self.swing_strategies = []
        self.swing_symbol_strategies: dict[str, SwingStrategy] = {}
        self._open_trade_ids: dict = {}       # symbol → (row_id, entry_price, entry_time)
        self._trade_ids_lock = asyncio.Lock() # guards all _open_trade_ids mutations
        self._db_engine = self._init_db_engine()
        self._regime_cache = None             # (regime_str, timestamp)
        self._regime_class_cache = None       # 4-regime taxonomy: (regime_str, timestamp)
        self._current_regime_class = CHOPPY   # latest 4-regime label for entry logging
        self._regime_confidence = 50          # cross-asset confirmation confidence 0-100 (Task 3)
        self._regime_cross_asset = {}         # latest cross-asset signal detail (Task 3)
        self._decay_monitor = StrategyDecayMonitor(engine=self._db_engine)
        self._decay_status_map: dict = {}     # (signal_type, symbol) → {disabled, position_multiplier, status}
        self.daily_pnl = 0.0
        self.start_of_day_equity = 0.0
        self.last_pnl_reset_date = datetime.now(pytz.timezone('America/New_York')).date()
        self.trading_halted_for_day = False
        self.risk_multiplier = 1.0
        self.active_signals = {}
        self.last_loss_times = {}
        # Risk Management Upgrade (Task 8) state
        self._risk_state_cache = None          # (timestamp, dict) — consecutive losses + weekly P&L
        self._entries_paused_until = None       # datetime — consecutive-loss pause expiry
        self._last_consec_loss_alert = None     # de-dup the Slack pause alert
        # Issue 2: only count losses from the current bot session (and reset after each
        # 2-hour consecutive-loss pause) so prior-session losses never trigger a pause.
        self._consec_loss_baseline_time = datetime.now(pytz.utc)
        self._alerted_negative_ev: set[str] = set()
        self._last_ev_check_date = None

        self._recent_signals: deque = deque(maxlen=50)
        self._sector_alert_cooldown: dict[str, datetime] = {}
        self._adx_regime_cache = None  # (regime_str, timestamp)
        self._adv_cache: dict[str, tuple[float, datetime]] = {}  # symbol → (adv_shares, cached_at)
        self._signal_stack: dict[str, list] = {}  # ticker → [{source, strength, timestamp}]
        self._daily_signals: dict[str, set] = {}  # ticker → set of source names that fired today
        self._confluence_alerted: set[str] = set()  # tickers already alerted today (dedup)
        self._last_daily_signals_date = datetime.now(pytz.timezone('America/New_York')).date()
        self._correlation_guard = CorrelationGuard(
            price_lookback_days=60,
            max_portfolio_correlation=0.7,
            max_correlated_positions=2,
            correlation_threshold=0.75,
        )
        self._si_signal = ShortInterestSignal(
            quiver_api_key=Config.QUIVER_API_KEY,
            high_short_interest_threshold=0.65,
            squeeze_price_change_threshold=0.02,
            cache_ttl_hours=12.0,
        )
        # Per-symbol 4-hour cooldown for the 5-min swing screener.
        # Updated when a signal actually fires to prevent re-entering the same daily candle.
        self._swing_signal_times: dict[str, datetime] = {}

    def add_scalp_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.scalp_strategies.append(strategy)

    def add_swing_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.swing_strategies.append(strategy)

    async def _check_account_status(self):
        print("DEBUG: Fetching account details from Alpaca...")
        try:
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                acct_num = getattr(account, 'account_number', 'unknown')
                print(f"Account ID={acct_num} Status={account.status} Equity=${float(account.equity):,.2f} BuyingPower=${float(account.buying_power):,.2f}")
                
                current_date = datetime.now(pytz.timezone('America/New_York')).date()
                if current_date != self.last_pnl_reset_date:
                    self.daily_pnl = 0.0
                    self.start_of_day_equity = float(account.equity)
                    self.last_pnl_reset_date = current_date
                    self.trading_halted_for_day = False
                    print(f"DEBUG: Daily PnL reset for {current_date}. Starting equity: ${self.start_of_day_equity:,.2f}")
                
                if self.start_of_day_equity == 0.0:
                    self.start_of_day_equity = float(account.equity)

                current_daily_pnl = float(account.equity) - self.start_of_day_equity
                self.risk_multiplier = 1.0
                if current_daily_pnl < 0:
                    current_daily_loss_percent = (abs(current_daily_pnl) / self.start_of_day_equity) * 100
                    if current_daily_loss_percent >= Config.MAX_DAILY_LOSS_PERCENT:
                        if not self.trading_halted_for_day:
                            self.trading_halted_for_day = True
                            msg = f"CRITICAL: Max daily loss of {Config.MAX_DAILY_LOSS_PERCENT}% hit! Trading halted for the day."
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg, level="CRITICAL"))
                    elif current_daily_loss_percent >= Config.DAILY_LOSS_REDUCTION_2_PERCENT:
                        self.risk_multiplier = 0.50
                    elif current_daily_loss_percent >= Config.DAILY_LOSS_REDUCTION_1_PERCENT:
                        self.risk_multiplier = 0.75
                
                self.daily_pnl = current_daily_pnl
                _health_state["alpaca_connected"] = True
                _health_state["equity_usd"] = float(account.equity)

                # Keep KellySizer base_capital in sync as equity changes daily
                _current_equity = float(account.equity)
                for _s in (
                    getattr(self, 'scalp_strategies', [])
                    + list(getattr(self, 'swing_symbol_strategies', {}).values())
                ):
                    _k = getattr(_s, '_kelly', None)
                    if _k:
                        _k.update_capital(_current_equity)
                if self.start_of_day_equity > 0:
                    _health_state["daily_pnl_pct"] = round(
                        (self.daily_pnl / self.start_of_day_equity) * 100, 4
                    )
                return True
            return False
        except Exception as e:
            _health_state["alpaca_connected"] = False
            msg = f"Error checking account status: {e}"
            print(msg)
            asyncio.create_task(notifications.notify_alert(msg))
            return False

    async def _update_loss_cache(self):
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=50,
                after=datetime.now(pytz.utc) - timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES)
            )
            orders = await asyncio.to_thread(self.trading_client.get_orders, req)
            for order in orders:
                if order.status.value == "filled" and (order.order_type.value == "stop" or order.order_type.value == "trailing_stop"):
                    self.last_loss_times[order.symbol] = order.filled_at
        except Exception as e:
            print(f"Failed to update loss cache: {e}")

    # ── Database helpers (SQLAlchemy) ─────────────────────────────────────────

    def _init_db_engine(self):
        url = Config.DATABASE_URL
        if not url:
            return None
        try:
            engine = create_engine(url, pool_pre_ping=True)
            return engine
        except Exception as e:
            print(f"[DB] Engine creation failed: {e}")
            return None

    def _ensure_signal_outcomes_table(self):
        if not self._db_engine:
            return
        try:
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    CREATE TABLE IF NOT EXISTS signal_outcomes (
                        id            SERIAL PRIMARY KEY,
                        symbol        VARCHAR(10),
                        signal_type   VARCHAR(20),
                        entry_time    TIMESTAMP,
                        exit_time     TIMESTAMP,
                        entry_price   FLOAT,
                        exit_price    FLOAT,
                        pnl_pct       FLOAT,
                        hold_bars     INTEGER,
                        ema_short     INTEGER,
                        ema_long      INTEGER,
                        rsi_at_entry  FLOAT,
                        macd_at_entry FLOAT,
                        market_regime VARCHAR(20),
                        exit_reason   VARCHAR(30),
                        discovered_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                conn.execute(sql_text("""
                    CREATE TABLE IF NOT EXISTS strategy_circuit_breakers (
                        strategy_name  TEXT PRIMARY KEY,
                        tripped_at     TIMESTAMP DEFAULT NOW(),
                        reason         TEXT
                    )
                """))
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
                conn.execute(sql_text("""
                    CREATE TABLE IF NOT EXISTS discovery_results (
                        id              SERIAL PRIMARY KEY,
                        symbol          VARCHAR(10),
                        strategy_type   VARCHAR(50),
                        parameters      JSONB,
                        train_sharpe    FLOAT,
                        test_sharpe     FLOAT,
                        degradation     FLOAT,
                        p_value         FLOAT,
                        total_trades    INTEGER,
                        win_rate        FLOAT,
                        bull_sharpe     FLOAT,
                        bear_sharpe     FLOAT,
                        high_vol_sharpe FLOAT,
                        best_regime     VARCHAR(20),
                        status          VARCHAR(20) DEFAULT 'pending_approval',
                        discovered_at   TIMESTAMP DEFAULT NOW(),
                        UNIQUE (symbol, strategy_type, parameters)
                    )
                """))
                # Regime-aware tracking column (4-regime taxonomy at signal time).
                conn.execute(sql_text(
                    "ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS regime_class VARCHAR(20)"
                ))
                # Decay multiplier applied to this trade (audit trail for decay monitor).
                conn.execute(sql_text(
                    "ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS decay_multiplier FLOAT DEFAULT 1.0"
                ))
                # Composite signal-quality score at entry (Task 5).
                conn.execute(sql_text(
                    "ALTER TABLE signal_outcomes ADD COLUMN IF NOT EXISTS composite_score FLOAT"
                ))
                count = conn.execute(sql_text("SELECT COUNT(*) FROM signal_outcomes")).scalar()
            _health_state["db_connected"] = True
            print(f"[DB] signal_outcomes table verified — {count} existing rows")
        except Exception as e:
            _health_state["db_connected"] = False
            print(f"[DB] Table setup failed: {e}")

    def _ensure_signal_cooldowns_table(self) -> None:
        if not self._db_engine:
            return
        try:
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    CREATE TABLE IF NOT EXISTS signal_cooldowns (
                        symbol           VARCHAR(20) PRIMARY KEY,
                        last_signal_time TIMESTAMP WITH TIME ZONE,
                        updated_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """))
            print("[Cooldown] signal_cooldowns table verified")
        except Exception as e:
            print(f"[Cooldown] Table setup failed (non-fatal): {e}")

    def _load_signal_cooldowns(self) -> None:
        """Restore in-memory _swing_signal_times from DB after a Railway redeploy.

        Only loads cooldowns that are still within the 4-hour window so stale rows
        don't block a symbol indefinitely.
        """
        if not self._db_engine:
            return
        try:
            with self._db_engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT symbol, last_signal_time
                    FROM signal_cooldowns
                    WHERE last_signal_time > NOW() - INTERVAL '4 hours'
                """)).mappings().fetchall()
            loaded = 0
            for row in rows:
                ts = row["last_signal_time"]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=pytz.utc)
                self._swing_signal_times[row["symbol"]] = ts
                loaded += 1
            if loaded:
                print(
                    f"[Cooldown] Loaded {loaded} active symbol cooldowns from DB "
                    f"— re-entry protection survives this restart"
                )
        except Exception as e:
            print(f"[Cooldown] Failed to load cooldowns from DB (non-fatal): {e}")

    def _persist_signal_cooldown(self, symbol: str, ts: datetime) -> None:
        """Write a cooldown timestamp to signal_cooldowns so it survives the next restart."""
        if not self._db_engine:
            return
        try:
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    INSERT INTO signal_cooldowns (symbol, last_signal_time, updated_at)
                    VALUES (:symbol, :ts, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        last_signal_time = EXCLUDED.last_signal_time,
                        updated_at = NOW()
                """), {"symbol": symbol, "ts": ts})
        except Exception as e:
            print(f"[Cooldown] Failed to persist cooldown for {symbol} (non-fatal): {e}")

    def _log_trade_entry(self, symbol: str, signal_type: str, entry_price: float,
                          ema_short: int, ema_long: int, rsi_at_entry: float,
                          macd_at_entry: float, regime: str, entry_time,
                          regime_class: str | None = None,
                          decay_multiplier: float = 1.0,
                          composite_score: float | None = None) -> int | None:
        if not self._db_engine:
            return None
        try:
            with self._db_engine.begin() as conn:
                result = conn.execute(sql_text("""
                    INSERT INTO signal_outcomes
                        (symbol, signal_type, entry_time, entry_price, ema_short, ema_long,
                         rsi_at_entry, macd_at_entry, market_regime, regime_class, decay_multiplier,
                         composite_score)
                    VALUES (:symbol, :signal_type, :entry_time, :entry_price, :ema_short, :ema_long,
                            :rsi_at_entry, :macd_at_entry, :market_regime, :regime_class, :decay_multiplier,
                            :composite_score)
                    RETURNING id
                """), {
                    "symbol": symbol, "signal_type": signal_type, "entry_time": entry_time,
                    "entry_price": float(entry_price), "ema_short": int(ema_short),
                    "ema_long": int(ema_long), "rsi_at_entry": float(rsi_at_entry),
                    "macd_at_entry": float(macd_at_entry), "market_regime": regime,
                    "regime_class": regime_class, "decay_multiplier": float(decay_multiplier),
                    "composite_score": None if composite_score is None else float(composite_score),
                })
                row_id = result.fetchone()[0]
            print(f"[DB] Logged {signal_type} entry for {symbol} (row={row_id})")
            return row_id
        except Exception as e:
            print(f"[DB] Entry log failed for {symbol}: {e}")
            return None

    def _update_trade_exit(self, row_id: int, exit_price: float, exit_reason: str,
                            exit_time, hold_bars: int, pnl_pct: float):
        if not self._db_engine:
            return
        try:
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE signal_outcomes
                    SET exit_time=:exit_time, exit_price=:exit_price, pnl_pct=:pnl_pct,
                        hold_bars=:hold_bars, exit_reason=:exit_reason
                    WHERE id=:id
                """), {
                    "exit_time": exit_time, "exit_price": float(exit_price),
                    "pnl_pct": float(pnl_pct), "hold_bars": int(hold_bars),
                    "exit_reason": exit_reason, "id": row_id,
                })
            print(f"[DB] Exit logged row={row_id}: {exit_reason} @ {exit_price:.2f} ({pnl_pct:+.2f}%)")
        except Exception as e:
            print(f"[DB] Exit update failed row={row_id}: {e}")

    def _check_strategy_circuit_breaker(
        self,
        strategy_name: str,
        signal_type: str,
        threshold_pct: float,
        window_days: int,
    ) -> tuple[bool, str, bool]:
        """
        Returns (is_paused, reason, is_newly_tripped). Sync — call via asyncio.to_thread.

        Queries rolling net pnl_pct over window_days for signal_type. If net loss
        exceeds threshold_pct the strategy is paused (CB record inserted). When
        the window recovers above the threshold the record is deleted and trading resumes.
        No fixed resume timestamp — re-evaluated on every buy signal.
        """
        if not self._db_engine:
            return False, "", False
        try:
            with self._db_engine.connect() as conn:
                pnl_row = conn.execute(sql_text("""
                    SELECT COALESCE(SUM(pnl_pct), 0) AS net_pnl,
                           COUNT(*)                   AS trade_count
                    FROM signal_outcomes
                    WHERE signal_type = :st
                      AND exit_time >= NOW() - (:days * INTERVAL '1 day')
                      AND exit_time IS NOT NULL
                      AND pnl_pct   IS NOT NULL
                """), {"st": signal_type, "days": window_days}).mappings().fetchone()
                cb_row = conn.execute(sql_text(
                    "SELECT strategy_name FROM strategy_circuit_breakers WHERE strategy_name = :name"
                ), {"name": strategy_name}).mappings().fetchone()

            net_pnl         = float(pnl_row["net_pnl"])   if pnl_row else 0.0
            trade_count     = int(pnl_row["trade_count"]) if pnl_row else 0
            currently_paused = cb_row is not None
            threshold_neg   = -abs(threshold_pct)

            if trade_count > 0 and net_pnl <= threshold_neg:
                reason = (
                    f"net_pnl={net_pnl:+.1f}% over last {window_days}d "
                    f"({trade_count} closed trades) ≤ −{abs(threshold_pct):.0f}%"
                )
                if not currently_paused:
                    with self._db_engine.begin() as conn:
                        conn.execute(sql_text("""
                            INSERT INTO strategy_circuit_breakers (strategy_name, reason)
                            VALUES (:name, :reason)
                            ON CONFLICT (strategy_name) DO UPDATE
                                SET tripped_at = NOW(), reason = EXCLUDED.reason
                        """), {"name": strategy_name, "reason": reason})
                    print(f"[CB] {strategy_name} TRIPPED — {reason}")
                    return True, reason, True
                return True, reason, False
            else:
                if currently_paused:
                    with self._db_engine.begin() as conn:
                        conn.execute(sql_text(
                            "DELETE FROM strategy_circuit_breakers WHERE strategy_name = :name"
                        ), {"name": strategy_name})
                    print(f"[CB] {strategy_name} RESUMED — net_pnl recovered to {net_pnl:+.1f}%")
                return False, "", False

        except Exception as e:
            print(f"[CircuitBreaker] DB check failed for {strategy_name}: {e}")
            return False, "", False

    # ── Market regime (Task 1) ────────────────────────────────────────────────

    async def _get_market_regime(self) -> str:
        if self._regime_cache is not None:
            regime, ts = self._regime_cache
            if time.time() - ts < Config.MARKET_REGIME_CACHE_SECONDS:
                return regime
        try:
            bars = await asyncio.to_thread(
                get_historical_bars, "SPY", TimeFrame.Day, 210, self.stock_data_client, False
            )
            if bars is not None and len(bars) >= 200:
                spy_close  = float(bars['close'].iloc[-1])
                spy_ema200 = float(bars['close'].ewm(span=200, adjust=False).mean().iloc[-1])
                regime = 'bull' if spy_close > spy_ema200 else 'bear'
            else:
                regime = 'neutral'
        except Exception as e:
            print(f"[MarketRegime] Failed: {e}")
            regime = 'neutral'
        self._regime_cache = (regime, time.time())
        _health_state["market_regime"] = regime
        return regime

    def _get_market_regime_adx(self, spy_bars) -> str:
        """
        Classifies the current SPY regime using ADX(14).
        Returns 'trending', 'choppy', or 'neutral'. Caches result for 4 hours.
        Sync — safe to call from swing_loop without to_thread.
        """
        if self._adx_regime_cache is not None:
            cached_regime, cached_ts = self._adx_regime_cache
            if time.time() - cached_ts < 14400:
                return cached_regime

        regime = 'neutral'
        try:
            if spy_bars is not None and not spy_bars.empty and len(spy_bars) >= 20:
                import pandas_ta as _ta
                adx_df = _ta.adx(spy_bars['high'], spy_bars['low'], spy_bars['close'], length=14)
                if adx_df is not None and not adx_df.empty:
                    adx_cols = [c for c in adx_df.columns if c.startswith('ADX_')]
                    if adx_cols:
                        adx_val = adx_df[adx_cols[0]].iloc[-1]
                        if not pd.isna(adx_val):
                            if adx_val > 25:
                                regime = 'trending'
                            elif adx_val < 20:
                                regime = 'choppy'
                            print(f"[ADX] SPY ADX={adx_val:.1f} → regime={regime}")
        except Exception as e:
            print(f"[ADX] Regime detection failed: {e}")

        self._adx_regime_cache = (regime, time.time())
        return regime

    # ── 4-regime classifier + live regime gating ──────────────────────────────

    async def _get_current_regime_class(self, spy_bars=None) -> str:
        """
        Current market regime in the 4-regime taxonomy (BULL_TREND / BEAR_TREND /
        HIGH_VOL / CHOPPY). Cached for Config.REGIME_CACHE_SECONDS (4h). Uses the
        live FRED-sourced VIX from MACRO_SNAPSHOT; falls back to SPY-only when VIX
        is unavailable.
        """
        if self._regime_class_cache is not None:
            regime, ts = self._regime_class_cache
            if time.time() - ts < Config.REGIME_CACHE_SECONDS:
                self._current_regime_class = regime
                return regime

        regime = CHOPPY
        ema50 = ema200 = float("nan")
        vix = MACRO_SNAPSHOT.get("vix")
        try:
            bars = spy_bars
            if bars is None:
                bars = await asyncio.to_thread(
                    get_historical_bars, "SPY", TimeFrame.Day, 260, self.stock_data_client, False
                )
            if bars is not None and len(bars) >= 200:
                tagged = classify_regime(bars, vix)
                regime = str(tagged["regime"].iloc[-1])
                ema50 = float(bars["close"].ewm(span=50, adjust=False).mean().iloc[-1])
                ema200 = float(bars["close"].ewm(span=200, adjust=False).mean().iloc[-1])
        except Exception as e:
            print(f"[Regime] _get_current_regime_class failed — defaulting to CHOPPY: {e}")

        vix_disp = vix if vix is not None else float("nan")
        print(
            f"[Regime] Current market regime: {regime} | "
            f"SPY EMA50={ema50:.2f} EMA200={ema200:.2f} VIX={vix_disp:.1f}"
        )

        # Cross-asset confirmation (Task 3): supplementary signals adjust a confidence
        # score (they never override the primary regime). Fully fail-open.
        try:
            signals = await asyncio.to_thread(
                compute_cross_asset_signals, self.stock_data_client, vix
            )
            conf = regime_confidence(regime, signals)
            print(
                f"[Regime] {regime} confidence={conf}% | "
                f"VIX_structure={signals['vix_structure']} credit={signals['credit']} "
                f"rotation={signals['rotation']} dollar={signals['dollar']}"
            )
            self._regime_confidence = conf
            self._regime_cross_asset = signals
        except Exception as e:
            print(f"[Regime] cross-asset confirmation failed (non-fatal): {e}")

        self._regime_class_cache = (regime, time.time())
        self._current_regime_class = regime
        return regime

    def _regime_gate_ok(self, symbol: str, current_regime: str) -> tuple[bool, list[str], bool]:
        """
        Look up the symbol's regime-validated flags in validated_strategies.
        Returns (proceed, valid_regimes, has_data). Fail-open: if there is no DB,
        no validation row, or any error, returns proceed=True with has_data=False.
        Sync — call via asyncio.to_thread.
        """
        if not self._db_engine:
            return True, [], False
        _col = {
            BULL_TREND: "valid_bull_trend",
            BEAR_TREND: "valid_bear_trend",
            HIGH_VOL:   "valid_high_vol",
            CHOPPY:     "valid_choppy",
        }
        try:
            with self._db_engine.connect() as conn:
                row = conn.execute(sql_text("""
                    SELECT valid_bull_trend, valid_bear_trend, valid_high_vol, valid_choppy
                    FROM validated_strategies
                    WHERE symbol = :sym
                    ORDER BY validated_at DESC NULLS LAST
                    LIMIT 1
                """), {"sym": symbol}).mappings().fetchone()
            if row is None:
                return True, [], False  # no regime data yet → fail-open
            valid_regimes = [
                reg for reg, col in _col.items() if bool(row.get(col))
            ]
            proceed = bool(row.get(_col.get(current_regime, "valid_choppy")))
            return proceed, valid_regimes, True
        except Exception as e:
            print(f"[Regime] {symbol}: validated_strategies lookup failed — fail-open: {e}")
            return True, [], False

    def _load_portfolio_symbols(self) -> tuple[set, bool]:
        """Return (symbols_in_current_optimal_portfolio, has_data).

        Reads the most recent ``strategy_portfolio`` build (Task 4). Fail-open: no
        DB / no table / empty / error → (empty set, False) so the caller proceeds.
        Sync — call via asyncio.to_thread.
        """
        if not self._db_engine:
            return set(), False
        try:
            with self._db_engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT symbol FROM strategy_portfolio
                    WHERE build_id = (SELECT build_id FROM strategy_portfolio
                                      ORDER BY selected_at DESC LIMIT 1)
                """)).mappings().fetchall()
            symbols = {r["symbol"] for r in rows}
            return symbols, bool(symbols)
        except Exception as e:
            print(f"[Portfolio] strategy_portfolio lookup failed — fail-open: {e}")
            return set(), False

    # ── Strategy decay gating (Loop 22 support) ───────────────────────────────

    def _decay_key_for_strategy(self, strategy) -> str:
        """Map a live strategy object to its signal_type — the decay-status key."""
        disc_type = getattr(strategy, 'discovery_strategy_type', None)
        if isinstance(strategy, (SwingStrategy, BollingerMeanReversionStrategy)) and disc_type:
            return f"discovery_{disc_type}"
        if isinstance(strategy, SwingStrategy):
            return 'swing_long'
        if isinstance(strategy, BollingerMeanReversionStrategy):
            return 'swing_bb'
        return 'scalp_long'

    def _decay_adjustment(self, strategy, symbol: str) -> tuple[bool, float, str | None]:
        """Return (disabled, position_multiplier, status) from the cached decay map."""
        if not Config.DECAY_MONITOR_ENABLED:
            return False, 1.0, None
        info = self._decay_status_map.get((self._decay_key_for_strategy(strategy), symbol))
        if not info:
            return False, 1.0, None
        return info["disabled"], float(info.get("position_multiplier", 1.0)), info.get("status")

    # ── Fundamentals check (Task 3) ───────────────────────────────────────────

    async def _check_fundamentals(self, symbol: str) -> tuple[bool, str | None]:
        try:
            api_key = Config.FINNHUB_API_KEY
            if not api_key:
                return True, None

            base = "https://finnhub.io/api/v1"
            today = datetime.now(pytz.timezone("America/New_York")).date()

            def _fetch_metrics():
                return _requests.get(
                    f"{base}/stock/metric",
                    params={"symbol": symbol, "metric": "all", "token": api_key},
                    timeout=10,
                ).json()

            metrics = await asyncio.to_thread(_fetch_metrics)
            m = metrics.get("metric", {})

            pe = m.get("peBasicExclExtraTTM")
            if pe is not None and float(pe) < 0:
                return False, f"Negative P/E ({float(pe):.1f}) — company not profitable"

            eps_list = metrics.get("series", {}).get("annual", {}).get("eps", [])
            if len(eps_list) >= 2:
                recent = eps_list[-1].get("v") or 0
                prior  = eps_list[-2].get("v") or 1
                if prior != 0:
                    growth_pct = (recent - prior) / abs(prior) * 100
                    if growth_pct < -20:
                        return False, f"EPS declined {growth_pct:.1f}% YoY"

            def _fetch_calendar():
                return _requests.get(
                    f"{base}/calendar/earnings",
                    params={
                        "from":   str(today),
                        "to":     str(today + timedelta(days=2)),
                        "symbol": symbol,
                        "token":  api_key,
                    },
                    timeout=10,
                ).json()

            cal    = await asyncio.to_thread(_fetch_calendar)
            events = cal.get("earningsCalendar", [])
            # When EARNINGS_FILTER_ENABLED the earnings_filter layer handles this — don't double-block
            if events and not Config.EARNINGS_FILTER_ENABLED:
                report_date = events[0].get("date", "within 48h")
                return False, f"Earnings report {report_date} — avoid pre-earnings volatility"

            return True, None

        except Exception as e:
            print(f"[Fundamentals] {symbol} check failed ({e}) — proceeding without")
            return True, None

    # ── Earnings calendar helper ──────────────────────────────────────────────

    async def _check_upcoming_earnings(
        self, symbol: str, days_ahead: int = 2
    ) -> tuple[bool, str | None]:
        """
        Returns (has_earnings, report_date_str) if symbol has a scheduled earnings
        event within the next days_ahead calendar days (inclusive), else (False, None).
        Silently returns (False, None) if FINNHUB_API_KEY is unset or the request fails.
        """
        api_key = Config.FINNHUB_API_KEY
        if not api_key:
            return False, None
        est   = pytz.timezone("America/New_York")
        today = datetime.now(est).date()
        try:
            def _fetch():
                return _requests.get(
                    "https://finnhub.io/api/v1/calendar/earnings",
                    params={
                        "from":   str(today),
                        "to":     str(today + timedelta(days=days_ahead)),
                        "symbol": symbol,
                        "token":  api_key,
                    },
                    timeout=10,
                ).json()
            cal    = await asyncio.to_thread(_fetch)
            events = cal.get("earningsCalendar", [])
            if events:
                return True, events[0].get("date", "unknown")
            return False, None
        except Exception as e:
            print(f"[Earnings] Calendar check failed for {symbol}: {e}")
            return False, None

    # ── Bull/Bear debate (Task 2) ─────────────────────────────────────────────

    async def _debate_trade(self, symbol: str, signal: dict, strategy) -> tuple[bool, str]:
        import json as _json
        try:
            is_short = signal.get('signal', '') == 'sell'
            ema_short = getattr(strategy, 'ema_short', 50)
            ema_long  = getattr(strategy, 'ema_long', 200)
            crossover_desc = (
                f"EMA{ema_short}/EMA{ema_long} bearish configuration."
                if is_short else
                f"EMA{ema_short} crossed above EMA{ema_long}."
            )
            shared_data = (
                f"Signal direction: {'SHORT SALE' if is_short else 'LONG BUY'} | "
                f"Symbol: {symbol}  Price: ${signal.get('entry_price', 0):.2f}  "
                f"RSI({getattr(strategy, 'rsi_period', 14)}): {signal.get('rsi_at_entry', 'N/A')}  "
                f"MACD: {signal.get('macd_at_entry', 'N/A')}  "
                f"{crossover_desc}  "
                f"Signal detail: {signal.get('reasoning', '')}"
            )
            web_plugin = [{"id": "web", "max_results": 1}]

            if is_short:
                # SHORT path: bear defends the signal; bull must raise 4+ concrete fundamental/macro
                # reasons to override it. No synthesis call — outcome is determined by counting.
                _bear_prompt = (
                    f"A technical SELL signal has fired for {symbol}. "
                    f"You are a bearish analyst defending this signal. "
                    f"Make the strongest 2-sentence case that this bearish signal is correct "
                    f"and the stock should be shorted now. Data: {shared_data}"
                )
                _bull_prompt = (
                    f"A technical SELL signal has fired for {symbol} with the following bearish "
                    f"evidence: {shared_data}. You are a bullish analyst. Make a compelling case "
                    f"for why this bearish signal should be IGNORED and the stock will rise. "
                    f"Provide only concrete fundamental or macro reasons — vague optimism does not count.\n"
                    'Return JSON only: {"reasons":["<concrete fundamental/macro reason 1>","<reason 2>",...],'
                    '"summary":"<one sentence>"}'
                )
                for _attempt in range(2):
                    try:
                        bear_resp, bull_resp = await asyncio.gather(
                            call_llm_with_model(
                                MODEL_FLASH, _bear_prompt,
                                max_tokens=200, plugins=web_plugin,
                            ),
                            call_llm_with_model(
                                MODEL_FLASH, _bull_prompt,
                                response_format={"type": "json_object"},
                                max_tokens=300, plugins=web_plugin,
                            ),
                        )
                        break
                    except LLMError as _e:
                        if _attempt == 0 and "null content" in str(_e):
                            print(f"[Debate] {symbol} None response from LLM — retrying once")
                        else:
                            raise

                bull_reasons: list[str] = []
                bull_summary = "No override provided"
                try:
                    parsed_b = _json.loads(bull_resp.text)
                    bull_reasons = [str(r) for r in parsed_b.get("reasons", []) if r]
                    bull_summary = parsed_b.get("summary", bull_resp.text[:200])
                except Exception:
                    bull_reasons = []
                    bull_summary = bull_resp.text[:200]

                n_bull = len(bull_reasons)
                proceed = n_bull < 4  # short proceeds unless bull raises 4+ concrete reasons

                if proceed:
                    print(f"[Debate] SHORT {symbol} — bull override FAILED ({n_bull} reason(s)) — short proceeds")
                else:
                    print(f"[Debate] SHORT {symbol} — bull override SUCCEEDED ({n_bull} reason(s)) — short blocked")

                all_citations = bear_resp.citations + bull_resp.citations
                source_lines = "\n".join(
                    f"  • <{c['url']}|{c['title'] or c['url']}>" for c in all_citations[:4]
                )
                reason_lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(bull_reasons))
                verdict_label = (
                    "✅ SHORT PROCEEDS (bull override failed)"
                    if proceed else
                    "🚫 SHORT BLOCKED (bull raised 4+ concrete override reasons)"
                )
                summary = (
                    f"*Bear (defends signal):* {bear_resp.text}\n"
                    f"*Bull override ({n_bull} reason(s)):* {bull_summary}"
                )
                if not proceed and reason_lines:
                    summary += f"\n*Override reasons:*\n{reason_lines}"
                summary += f"\n*Verdict:* {verdict_label}"
                if source_lines:
                    summary += f"\n*Sources:* {source_lines}"
                return proceed, summary

            else:
                # LONG path: neutral bull vs bear; synthesis call makes final verdict.
                _bull_long_prompt = (
                    f"You are a bullish stock analyst. Search for the latest news on {symbol} "
                    f"and make the strongest 2-sentence case FOR buying it now. Data: {shared_data}"
                )
                _bear_long_prompt = (
                    f"You are a bearish stock analyst. Search for the latest news on {symbol} "
                    f"and identify the strongest risks AGAINST buying it now. Data: {shared_data}\n"
                    'Return JSON only: {"objections":["<risk 1>","<risk 2>",...],"summary":"<one sentence>"}'
                )
                for _attempt in range(2):
                    try:
                        bull_resp, bear_resp = await asyncio.gather(
                            call_llm_with_model(
                                MODEL_FLASH, _bull_long_prompt,
                                max_tokens=200, plugins=web_plugin,
                            ),
                            call_llm_with_model(
                                MODEL_FLASH, _bear_long_prompt,
                                response_format={"type": "json_object"},
                                max_tokens=300, plugins=web_plugin,
                            ),
                        )
                        break
                    except LLMError as _e:
                        if _attempt == 0 and "null content" in str(_e):
                            print(f"[Debate] {symbol} None response from LLM — retrying once")
                        else:
                            raise

                # Parse bear JSON — retry bear call alone once if parse fails
                _bear_objections: list[str] = []
                _bear_summary = ""
                _bear_ok = False
                try:
                    _parsed_bear = _json.loads(bear_resp.text)
                    _bear_objections = [str(o) for o in _parsed_bear.get("objections", []) if o]
                    _bear_summary = _parsed_bear.get("summary", bear_resp.text[:300])
                    _bear_ok = True
                    print(f"[Debate] LONG {symbol} — bear raised {len(_bear_objections)} objection(s)")
                except Exception as _be:
                    _preview = bear_resp.text[:200] if bear_resp else "(no response)"
                    print(
                        f"[Debate] {symbol} bear parse failed: {_be}"
                        f" | raw[:200]={_preview!r} — retrying bear alone"
                    )
                    try:
                        bear_resp = await call_llm_with_model(
                            MODEL_FLASH, _bear_long_prompt,
                            response_format={"type": "json_object"},
                            max_tokens=300, plugins=web_plugin,
                        )
                        _parsed_bear = _json.loads(bear_resp.text)
                        _bear_objections = [str(o) for o in _parsed_bear.get("objections", []) if o]
                        _bear_summary = _parsed_bear.get("summary", bear_resp.text[:300])
                        _bear_ok = True
                        print(f"[Debate] LONG {symbol} — bear raised {len(_bear_objections)} objection(s) (retry succeeded)")
                    except (LLMError, Exception) as _be2:
                        print(
                            f"[Debate] LONG {symbol} — bear AI failed after retry ({_be2}),"
                            f" proceeding with caution"
                        )
                        strategy.debate_size_multiplier = min(
                            getattr(strategy, 'debate_size_multiplier', 1.0), 0.75
                        )
                        _bear_summary = "(bear analysis unavailable)"

                synthesis_prompt = (
                    f"Bull case: {bull_resp.text}\nBear case: {_bear_summary}\n\n"
                    f"Should we buy {symbol} right now?\n"
                    'Return JSON only: {"verdict":"proceed"|"skip"|"reduce_size",'
                    '"conviction":0.0-1.0,"reasoning":"one sentence"}'
                )
                for _attempt in range(2):
                    try:
                        verdict_resp = await call_llm_with_model(
                            MODEL_FLASH,
                            synthesis_prompt,
                            response_format={"type": "json_object"},
                            max_tokens=150,
                        )
                        break
                    except LLMError as _e:
                        if _attempt == 0 and "null content" in str(_e):
                            print(f"[Debate] {symbol} None response from LLM — retrying once")
                        else:
                            raise

                try:
                    parsed = _json.loads(verdict_resp.text)
                    verdict = parsed.get("verdict", "proceed").lower()
                    conviction = float(parsed.get("conviction", 0.7))
                    reasoning = parsed.get("reasoning", verdict_resp.text)
                except Exception:
                    verdict = "proceed" if verdict_resp.text.upper().startswith("P") else "skip"
                    conviction = 0.5
                    reasoning = verdict_resp.text

                all_citations = bull_resp.citations + bear_resp.citations
                source_lines = "\n".join(
                    f"  • <{c['url']}|{c['title'] or c['url']}>" for c in all_citations[:4]
                )
                proceed = verdict in ("proceed", "reduce_size")
                if verdict == "reduce_size":
                    strategy.debate_size_multiplier = 0.5
                    print(f"[Debate] {symbol} reduce_size verdict — setting 50% position size")

                _n_bear = len(_bear_objections)
                _bear_label = (
                    f"{_n_bear} objection(s): {_bear_summary}"
                    if _bear_ok else
                    f"unavailable — 0.75x size penalty applied"
                )
                summary = (
                    f"*Bull:* {bull_resp.text}\n*Bear ({_n_bear} objection(s)):* {_bear_summary}\n"
                    f"*Verdict:* {verdict.upper()} (conviction {conviction:.0%}) — {reasoning}"
                )
                if source_lines:
                    summary += f"\n*Sources:* {source_lines}"
                return proceed, summary

        except LLMError as e:
            print(f"[Debate] {symbol} LLMError — proceeding without debate: {e}")
            return True, "debate unavailable (LLM error)"
        except Exception as e:
            import traceback as _tb
            print(f"[Debate] {symbol} failed: {e}\n{_tb.format_exc()}")
            return True, "debate unavailable"

    # ── Pre-trade hook: fundamentals → debate (Tasks 2 & 3) ──────────────────

    async def _swing_pre_trade_hook(self, symbol: str, signal: dict, strategy) -> tuple[bool, str]:
        # Earnings protection — 3-day window: reduce to 25%; today/tomorrow: block
        if Config.EARNINGS_PROTECTION_ENABLED:
            _ep_has, _ep_date = await self._check_upcoming_earnings(symbol, days_ahead=3)
            if _ep_has and _ep_date:
                _ep_est = pytz.timezone("America/New_York")
                _ep_today = datetime.now(_ep_est).date()
                try:
                    from datetime import date as _date_cls
                    _ep_rpt = datetime.strptime(_ep_date, "%Y-%m-%d").date()
                    _ep_days = (_ep_rpt - _ep_today).days
                except Exception:
                    _ep_days = 99  # Unknown date format — reduce size, don't block
                if _ep_days <= 1:
                    _when = "today" if _ep_days <= 0 else "tomorrow"
                    print(f"[Earnings] {symbol} blocked — earnings {_when} ({_ep_date}), too risky")
                    asyncio.create_task(notifications.notify_trade_skipped(
                        symbol, "EarningsProtection",
                        f"Earnings {_when} ({_ep_date}) — blocked to avoid pre-report volatility",
                    ))
                    return False, f"Earnings: blocked — report {_when} ({_ep_date})"
                else:
                    strategy.earnings_override_multiplier = 0.25
                    _ep_note = f"⚠️ {symbol} has earnings in {_ep_days} days ({_ep_date}) — size reduced to 25%"
                    print(f"[Earnings] {_ep_note}")
                    asyncio.create_task(notifications.notify_alert(_ep_note, level="WARNING"))
        elif Config.EARNINGS_FILTER_ENABLED:
            # Legacy 48h check
            _ef_has, _ef_date = await self._check_upcoming_earnings(symbol, days_ahead=2)
            if _ef_has:
                strategy.earnings_override_multiplier = 0.25
                print(f"[Earnings] {symbol}: earnings within 48h ({_ef_date}) — size reduced to 25%")

        # Fundamentals check
        proceed, reason = await self._check_fundamentals(symbol)
        if not proceed:
            print(f"[Fundamentals] Blocking {symbol}: {reason}")
            asyncio.create_task(notifications.notify_trade_skipped(symbol, "Fundamentals", reason))
            return False, f"Fundamentals: {reason}"

        # Bull/Bear debate (can be disabled via config to save API credits)
        if not Config.BULL_BEAR_DEBATE_ENABLED:
            print(f"[Debate] Disabled via config — proceeding without debate.")
            return True, "Debate disabled"

        # SwingStrategy: Gemini 2.5 Flash sequential debate in swing_strategy.py
        # Other strategies (BollingerMeanReversion etc.): existing DeepSeek debate
        if isinstance(strategy, SwingStrategy):
            proceed, debate_summary = await strategy.run_debate(symbol, signal)
            debate_label = "Gemini Swing Debate"
        else:
            proceed, debate_summary = await self._debate_trade(symbol, signal, strategy)
            debate_label = "Bull/Bear Debate"

        action_label = "BUY" if proceed else "SKIP"
        asyncio.create_task(notifications.notify_trade_decision(
            symbol, debate_label,
            {"signal": "buy" if proceed else "hold",
             "reasoning": f"[{action_label}] {debate_summary}",
             "confidence": 0.0},
        ))

        if not proceed:
            return False, f"Debate SKIP — {debate_summary}"

        return True, debate_summary

    async def _process_symbol(self, symbol, strategies, is_crypto, risk_percent, stop_loss_percent,
                              current_price=None, pre_execute_hook=None):
        if self.trading_halted_for_day or _bot_paused:
            return

        await self._update_loss_cache()
        if symbol in self.last_loss_times:
            if datetime.now(pytz.utc) - self.last_loss_times[symbol] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                return # Blocked by cooldown

        client = self.crypto_data_client if is_crypto else self.stock_data_client
        if is_crypto:
            # 1 day of 1-minute bars (~1,440 bars) — enough for SMB (30-bar min) and
            # CryptoMomentum (21-bar min). The original value of 390 was intended as a
            # bar count but was interpreted as days_back=390 DAYS (~561k bars), which
            # caused the Alpaca SDK to paginate hundreds of pages and block the event loop.
            data = get_historical_bars(symbol, TimeFrame.Minute, 1, client, is_crypto=True)
            if data is not None and len(data) > 0:
                _latest_ts = data.index[-1].strftime('%H:%M UTC')
                print(f"[Scalp] {symbol}: fetched {len(data)} 1-min bars (latest: {_latest_ts})")
            else:
                print(f"[Scalp] {symbol}: no 1-min bars returned")
        else:
            data = get_historical_bars(symbol, TimeFrame.Day, 365, client, is_crypto=False)
        
        if data is None:
            return

        # Ensure 'symbol' column exists in the DataFrame
        if 'symbol' not in data.columns:
            data['symbol'] = symbol

        if current_price is not None:
            current_bar = pd.DataFrame([{
                'open': current_price,
                'high': current_price,
                'low': current_price,
                'close': current_price,
                'volume': 0,
                'vwap': current_price,
                'symbol': symbol,
            }], index=pd.DatetimeIndex([datetime.now(pytz.utc)]))
            data = pd.concat([data, current_bar])
            data.sort_index(inplace=True)

        # Best-signal priority (Task 6): when multiple crypto strategies run on the
        # same tick (SMB + Crypto Momentum), evaluate each and execute only the
        # highest-confidence signal so they never double up on one symbol. The
        # winning signal is reused below (not regenerated) so per-strategy throttle
        # state set during evaluation doesn't suppress it on a second call.
        iter_strategies = strategies
        pre_signals: dict = {}
        if is_crypto and len(strategies) > 1:
            scored = []
            for s in strategies:
                try:
                    _sig = (s.generate_signals(data, self.stock_data_client)
                            if isinstance(s, SMBStrategy) else s.generate_signals(data))
                except Exception as _ge:
                    print(f"[Scalp] {symbol}: {s.name} signal eval failed — {_ge}")
                    _sig = None
                if _sig and _sig.get("signal") in ("buy", "sell"):
                    scored.append((float(_sig.get("confidence", 0.0)), s, _sig))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                _conf, winner, winner_sig = scored[0]
                if len(scored) > 1:
                    print(f"[Scalp] {symbol}: {len(scored)} crypto signals — running "
                          f"highest-confidence {winner.name} (conf={_conf:.2f})")
                iter_strategies = [winner]
                pre_signals[winner.name] = winner_sig
            else:
                iter_strategies = []

        for strategy in iter_strategies:
            # Decay gate: a strategy disabled by the decay monitor is skipped entirely.
            _decay_disabled, _decay_mult, _decay_status = self._decay_adjustment(strategy, symbol)
            if _decay_disabled:
                print(f"[Decay] {symbol} {strategy.name} DISABLED — skipping")
                continue

            print(f"Running strategy: {strategy.name} for {symbol}")
            signal = pre_signals.get(strategy.name)
            if signal is None:
                if isinstance(strategy, SMBStrategy):
                    signal = strategy.generate_signals(data, self.stock_data_client)
                else:
                    signal = strategy.generate_signals(data)
            
            if signal:
                if self.trading_halted_for_day:
                    asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Daily loss limit hit", critical=True))
                    continue
                    
                if signal['signal'] == "hold":
                    asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Signal was hold (insufficient RR ratio or bear case stronger)"))
                    continue

                # Portfolio heat cap: applies to ALL new positions (buys and shorts)
                _all_pos = []  # populated below; reused by correlation guard
                _equity_ref = _health_state.get("equity_usd") or self.start_of_day_equity
                if _equity_ref > 0:
                    try:
                        _all_pos = await asyncio.to_thread(self.trading_client.get_all_positions)
                        _heat = sum(
                            abs(float(p.market_value)) * (stop_loss_percent / 100.0)
                            for p in _all_pos
                        ) / _equity_ref
                        if _heat >= Config.PORTFOLIO_HEAT_CAP:
                            _heat_msg = (
                                f"Portfolio heat {_heat:.1%} ≥ cap "
                                f"{Config.PORTFOLIO_HEAT_CAP:.0%} — trade skipped"
                            )
                            print(f"[HeatCap] {symbol}: {_heat_msg}")
                            asyncio.create_task(notifications.notify_trade_skipped(
                                symbol, strategy.name, _heat_msg, critical=True
                            ))
                            continue
                    except Exception as _heat_err:
                        print(f"[HeatCap] Position check failed for {symbol}: {_heat_err}")

                # ── Risk Management Upgrade (Task 8): account-level gates ───────
                _risk_state = await asyncio.to_thread(self._get_risk_state)
                _now_utc = datetime.now(pytz.utc)
                # Consecutive-loss pause — block all new entries during the cooldown.
                if self._entries_paused_until and _now_utc < self._entries_paused_until:
                    print(f"[Risk] {symbol}: new entries paused until "
                          f"{self._entries_paused_until:%H:%M UTC} (consecutive losses) — SKIP")
                    continue
                # Pause just expired → reset the baseline so the counter starts from zero.
                # Without this, the same historical losses would immediately re-trigger the pause.
                if self._entries_paused_until and _now_utc >= self._entries_paused_until:
                    self._consec_loss_baseline_time = _now_utc
                    self._risk_state_cache = None
                    self._entries_paused_until = None
                    _risk_state["consecutive_losses"] = 0
                    print(f"[Risk] {symbol}: consecutive-loss pause expired — loss counter reset to 0")
                if risk_limits.consecutive_loss_tripped(
                    _risk_state["consecutive_losses"], Config.CONSECUTIVE_LOSS_LIMIT
                ):
                    self._entries_paused_until = _now_utc + timedelta(
                        hours=Config.CONSECUTIVE_LOSS_PAUSE_HOURS)
                    _cl_msg = (f"{_risk_state['consecutive_losses']} consecutive losing trades "
                               f"— pausing new entries for {Config.CONSECUTIVE_LOSS_PAUSE_HOURS:g}h")
                    print(f"[Risk] {symbol}: {_cl_msg} — SKIP (FAIL)")
                    if (self._last_consec_loss_alert is None
                            or (_now_utc - self._last_consec_loss_alert).total_seconds() > 3600):
                        asyncio.create_task(notifications.notify_alert(
                            f"⛔ Risk: {_cl_msg}", level="CRITICAL"))
                        self._last_consec_loss_alert = _now_utc
                    continue
                print(f"[Risk] {symbol}: consecutive-loss check PASS "
                      f"({_risk_state['consecutive_losses']}/{Config.CONSECUTIVE_LOSS_LIMIT})")
                # Weekly loss limit — reduce new-position sizing for the rest of the week.
                weekly_loss_mult = risk_limits.weekly_loss_reduction(
                    _risk_state["weekly_pnl_pct"], Config.WEEKLY_LOSS_LIMIT_PCT,
                    Config.WEEKLY_LOSS_SIZE_REDUCTION,
                )
                if weekly_loss_mult < 1.0:
                    print(f"[Risk] {symbol}: weekly P&L {_risk_state['weekly_pnl_pct']:.2f}% < "
                          f"{Config.WEEKLY_LOSS_LIMIT_PCT}% — new sizes ×{weekly_loss_mult} (FAIL)")
                else:
                    print(f"[Risk] {symbol}: weekly P&L {_risk_state['weekly_pnl_pct']:.2f}% "
                          f"within limit (PASS)")

                # ── SMC confirmation gate (Task 9 / optional) ─────────────────────
                # When enabled, swing signals require price to be inside an active
                # order block AND have an unfilled FVG target in the signal direction.
                if (Config.SMC_CONFIRMATION_ENABLED and _SMC_AVAILABLE
                        and isinstance(strategy, SwingStrategy) and not is_crypto):
                    _smc_direction = "bullish" if signal["signal"] == "buy" else "bearish"
                    try:
                        _smc_obs = detect_order_blocks(data, lookback=20)
                        _smc_fvgs = detect_fair_value_gaps(data)
                        _smc_obs_dir = [ob for ob in _smc_obs if ob["direction"] == _smc_direction]
                        _smc_fvgs_dir = [fvg for fvg in _smc_fvgs if fvg["direction"] == _smc_direction]
                        _curr_price = float(data["close"].iloc[-1])
                        _n_ob = len(_smc_obs_dir)
                        _n_fvg = len(_smc_fvgs_dir)
                        print(
                            f"[SMC] {symbol} — {_n_ob} active order blocks | "
                            f"{_n_fvg} unfilled FVGs | signal={_smc_direction}"
                        )
                        _in_ob = any(ob["low"] <= _curr_price <= ob["high"] for ob in _smc_obs_dir)
                        if _smc_direction == "bullish":
                            # Unfilled bullish FVG target: gap lower edge is above current price
                            _fvg_target = any(fvg["lower"] > _curr_price for fvg in _smc_fvgs_dir)
                        else:
                            # Unfilled bearish FVG target: gap upper edge is below current price
                            _fvg_target = any(fvg["upper"] < _curr_price for fvg in _smc_fvgs_dir)
                        if not _in_ob or not _fvg_target:
                            _smc_msg = (
                                f"SMC gate: "
                                + ("not in order block" if not _in_ob else "no FVG target")
                                + f" ({_n_ob} OBs, {_n_fvg} FVGs)"
                            )
                            print(f"[SMC] {symbol}: {_smc_msg} — SKIP")
                            asyncio.create_task(notifications.notify_trade_skipped(
                                symbol, strategy.name, _smc_msg
                            ))
                            continue
                        print(f"[SMC] {symbol}: in OB + FVG target confirmed — PASS")
                    except Exception as _smc_err:
                        print(f"[SMC] {symbol}: gate error (fail-open) — {_smc_err}")

                if signal['signal'] == "buy":
                    try:
                        await asyncio.to_thread(self.trading_client.get_open_position, symbol)
                        asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "One position per symbol limit"))
                        continue
                    except Exception as e:
                        err = str(e).lower()
                        if "position" not in err and "not found" not in err and "404" not in err:
                            print(f"[ProcessSymbol] Unexpected error checking position for {symbol}: {e}")
                            continue  # Don't trade on unexpected API errors

                    # Correlation guard: block if new position would concentrate the portfolio
                    _open_symbols = [p.symbol for p in _all_pos]
                    _corr_result = await asyncio.to_thread(
                        self._correlation_guard.check,
                        symbol,
                        _open_symbols,
                        lambda sym: get_historical_bars(
                            sym, TimeFrame.Day, 60, self.stock_data_client
                        ),
                    )
                    if not _corr_result["allowed"]:
                        _corr_msg = _corr_result["reason"]
                        print(
                            f"[CORRELATION] Trade blocked: {_corr_msg} | "
                            f"correlation_map={_corr_result['correlation_map']}"
                        )
                        asyncio.create_task(notifications.notify_trade_skipped(
                            symbol, strategy.name, _corr_msg
                        ))
                        continue
                    elif _corr_result["avg_correlation"] > 0.5 and _open_symbols:
                        print(
                            f"[CORRELATION] Proceeding with elevated correlation "
                            f"{_corr_result['avg_correlation']:.2f} to open positions"
                        )

                    # ── Sector concentration cap (Task 8) ──────────────────────
                    _positions_mv = [
                        (p.symbol, abs(float(getattr(p, "market_value", 0) or 0)))
                        for p in _all_pos
                    ]
                    _sec_ok, _sec, _sec_share, _sec_reason = risk_limits.sector_exposure_ok(
                        CorrelationGuard.SECTOR_MAP, symbol, _positions_mv,
                        Config.MAX_SECTOR_CONCENTRATION_PCT,
                    )
                    if not _sec_ok:
                        print(f"[Risk] {symbol}: sector concentration FAIL — {_sec_reason}")
                        asyncio.create_task(notifications.notify_trade_skipped(
                            symbol, strategy.name, f"Sector cap: {_sec_reason}"))
                        continue
                    print(f"[Risk] {symbol}: sector concentration PASS "
                          f"({_sec} {_sec_share:.1f}% ≤ {Config.MAX_SECTOR_CONCENTRATION_PCT:.0f}%)")

                    # ── Single-position size cap (Task 8): ≤ 5% equity at entry ─
                    _eq_ref = _health_state.get("equity_usd") or self.start_of_day_equity or 0.0
                    _entry_px = float(signal.get("entry_price") or 0)
                    _pos_cap = risk_limits.single_position_share_cap(
                        _eq_ref, _entry_px, Config.MAX_SINGLE_POSITION_PCT)
                    if _pos_cap > 0:
                        _existing_cap = signal.get("adv_cap_shares")
                        signal["adv_cap_shares"] = (
                            min(_existing_cap, _pos_cap) if _existing_cap else _pos_cap)
                        print(f"[Risk] {symbol}: single-position cap "
                              f"{_pos_cap} shares (≤{Config.MAX_SINGLE_POSITION_PCT:.0f}% equity) "
                              f"→ effective share cap {signal['adv_cap_shares']}")

                    # Short interest signal: enrichment for swing buy signals only
                    if isinstance(strategy, SwingStrategy):
                        _si = await asyncio.to_thread(
                            self._si_signal.get,
                            symbol,
                            float(signal.get("entry_price", 0)),
                            float(signal.get("prev_close", signal.get("entry_price", 0))),
                        )
                        print(
                            f"[SI] {symbol}: short_vol_ratio={_si['short_interest_pct']:.1%} "
                            f"squeeze_score={_si['squeeze_score']:.2f} signal={_si['signal']:+d}"
                        )
                        if _si["signal"] == -1:
                            _si_msg = f"Short interest veto: {_si['note']}"
                            asyncio.create_task(notifications.notify_trade_skipped(
                                symbol, strategy.name, _si_msg
                            ))
                            continue
                        elif _si["signal"] == 1:
                            signal["si_boost_note"] = _si["note"]

                        # Week-over-week short-interest confirmation bonus (Task 4 of
                        # overnight build). Distinct from the FINRA veto above: rewards
                        # size when the WoW change in short-volume ratio confirms the
                        # thesis. SHORT (sell): SI rising > +10% → +0.2x. LONG (buy):
                        # SI falling > -15% (squeeze) → +0.3x. Fail-open: no data → skip.
                        if Config.SHORT_INTEREST_CONFIRM_ENABLED:
                            try:
                                from discovery.data_feeds.finra_historical import (
                                    get_recent_wow_change, short_interest_size_adjustment,
                                )
                                _wow = await asyncio.to_thread(get_recent_wow_change, symbol)
                                if _wow is not None:
                                    _adj = short_interest_size_adjustment(
                                        signal.get('signal'), _wow,
                                        Config.SHORT_INTEREST_RISING_THRESHOLD,
                                        Config.SHORT_INTEREST_FALLING_THRESHOLD,
                                        Config.SHORT_INTEREST_SHORT_BONUS,
                                        Config.SHORT_INTEREST_LONG_BONUS,
                                    )
                                    if _adj > 0:
                                        signal['short_interest_size_mult'] = 1.0 + _adj
                                        signal['si_wow_note'] = (
                                            f"🩳 Short interest WoW {_wow:+.1%} → size +{_adj:.1f}×"
                                        )
                                    print(f"[ShortInt] {symbol} WoW change={_wow:+.1%} → size adjustment={_adj:+.1f}x")
                                else:
                                    print(f"[ShortInt] {symbol} WoW change=n/a → size adjustment=+0.0x (no data)")
                            except Exception as _sie:
                                import traceback as _tb
                                print(f"[ShortInt] {symbol} WoW confirmation failed (non-fatal): {_sie}\n{_tb.format_exc()}")

                    # Pre-execute hook: fundamentals check + bull/bear debate (swing only)
                    if pre_execute_hook:
                        # Set 4-hour cooldown the moment a signal enters the protection stack,
                        # so repeated debate calls never fire for the same signal in one session.
                        _now_utc = datetime.now(pytz.utc)
                        _next_eligible = _now_utc + timedelta(hours=4)
                        self._swing_signal_times[symbol] = _now_utc
                        asyncio.create_task(
                            asyncio.to_thread(self._persist_signal_cooldown, symbol, _now_utc)
                        )
                        print(
                            f"[Swing] {symbol} cooldown set — "
                            f"next eligible {_next_eligible.strftime('%Y-%m-%d %H:%M UTC')}"
                        )
                        hook_proceed, hook_reason = await pre_execute_hook(symbol, signal, strategy)
                        if not hook_proceed:
                            asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, hook_reason))
                            continue

                    # Confluence tracking: register swing buy after hook passes
                    if isinstance(strategy, SwingStrategy):
                        asyncio.create_task(self._record_daily_signal(symbol, 'Swing technical'))

                signal_key = f"{symbol}-{strategy.name}"

                # Check if signal is active and within cooldown period (1 hour, per symbol+strategy)
                if signal_key in self.active_signals:
                    last_signal_time = self.active_signals[signal_key]
                    if datetime.now(pytz.utc) - last_signal_time < timedelta(hours=1):
                        asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Symbol on cooldown"))
                        continue
                    else:
                        # Cooldown expired, remove from active signals
                        del self.active_signals[signal_key]

                # VIX spike protection
                vix = MACRO_SNAPSHOT.get("vix") or 0
                if vix > Config.VIX_EXTREME_THRESHOLD:
                    block_msg = (
                        f"VIX {vix:.1f} exceeds extreme threshold "
                        f"({Config.VIX_EXTREME_THRESHOLD}) — trade blocked"
                    )
                    print(f"[VIX] {symbol}: {block_msg}")
                    asyncio.create_task(notifications.notify_alert(
                        f"VIX spike: {symbol} {strategy.name} trade BLOCKED — "
                        f"VIX={vix:.1f} > {Config.VIX_EXTREME_THRESHOLD}. "
                        f"All new trades suppressed until VIX normalises.",
                        level="CRITICAL",
                    ))
                    asyncio.create_task(notifications.notify_trade_skipped(
                        symbol, strategy.name, block_msg, critical=True
                    ))
                    continue

                vix_risk_mult = 1.0
                vix_note = None
                if vix > Config.VIX_SPIKE_THRESHOLD:
                    vix_risk_mult = 0.25
                    vix_note = (
                        f"⚠️ VIX spike ({vix:.1f} > {Config.VIX_SPIKE_THRESHOLD}) "
                        f"— position size reduced to 25%"
                    )
                    print(f"[VIX] {symbol}: {vix_note}")

                print(f"Signal generated: {signal}")
                # Sector sentiment tracking (buy signals only)
                # TODO Phase 4: extend to news/EDGAR auto-trade loops with full S&P 500 sector mapping.
                if signal.get('signal') == 'buy':
                    self._recent_signals.append((symbol, datetime.now(pytz.utc)))
                    asyncio.create_task(self._check_sector_alert(symbol))

                # Determine signal_type early for Performance Brain + DB logging
                signal_type = None
                if signal['signal'] == 'buy':
                    disc_type = getattr(strategy, 'discovery_strategy_type', None)
                    if isinstance(strategy, (SwingStrategy, BollingerMeanReversionStrategy)) and disc_type:
                        signal_type = f"discovery_{disc_type}"
                    elif isinstance(strategy, SwingStrategy):
                        signal_type = 'swing_long'
                    elif isinstance(strategy, BollingerMeanReversionStrategy):
                        signal_type = 'swing_bb'
                    else:
                        signal_type = 'scalp_long'

                # Circuit breaker: block new entries if strategy has recent drawdown
                if signal.get('signal') == 'buy' and signal_type:
                    cb_threshold = getattr(strategy, 'drawdown_threshold_pct', 10.0)
                    cb_window    = getattr(strategy, 'drawdown_window_days', 14)
                    cb_paused, cb_reason, cb_new_trip = await asyncio.to_thread(
                        self._check_strategy_circuit_breaker,
                        strategy.name, signal_type, cb_threshold, cb_window,
                    )
                    if cb_paused:
                        cb_msg = f"Circuit breaker active — {cb_reason}"
                        print(f"[CB] {symbol}/{strategy.name}: trade blocked — {cb_reason}")
                        asyncio.create_task(notifications.notify_trade_skipped(
                            symbol, strategy.name, cb_msg, critical=cb_new_trip
                        ))
                        continue

                # Performance Brain: adjust size based on last 20-trade win rate
                perf_mult = 1.0
                perf_note = None
                if signal_type and Config.PERFORMANCE_SCALING_ENABLED:
                    perf_mult = await asyncio.to_thread(
                        self._get_performance_multiplier, signal_type, symbol,
                        self._current_regime_class,
                    )
                    if perf_mult > 1.0:
                        perf_note = (
                            f"🧠 Performance Brain: {signal_type} momentum/regime/time "
                            f"→ size +{int((perf_mult - 1) * 100)}%"
                        )
                    elif perf_mult < 1.0:
                        perf_note = (
                            f"🧠 Performance Brain: {signal_type} momentum/regime/time "
                            f"→ size {int((perf_mult - 1) * 100)}%"
                        )

                kelly_note = None
                if signal.get('signal') == 'buy':
                    _kelly = getattr(strategy, '_kelly', None)
                    if _kelly and _kelly.engine and _kelly.base_capital > 0 and signal_type:
                        _ep = float(signal.get('entry_price') or 0)
                        if _ep > 0:
                            _kr = _kelly.get_position_size(signal_type, _ep)
                            if _kr['shares'] > 0:
                                signal['kelly_qty'] = _kr['shares']
                                signal['half_kelly_f'] = _kr['half_kelly_f']
                                kelly_note = (
                                    f"Kelly ({_kr['half_kelly_f']:.1%} of capital): {_kr['note']}"
                                )
                                print(
                                    f"[Kelly] {symbol} {signal_type}: "
                                    f"{_kr['shares']} shares | {_kr['note']}"
                                )

                # Sentiment gate: adjust position size based on news sentiment_scores table
                sentiment_mult = 1.0
                sentiment_note = None
                if signal.get('signal') == 'buy':
                    _sent = await asyncio.to_thread(
                        get_sentiment_score, self._db_engine, symbol
                    )
                    if _sent:
                        _sdir = _sent.get('direction', 'neutral')
                        _sscr = int(_sent.get('score', 0))
                        _scnt = int(_sent.get('headline_count', 0))
                        if _sdir == 'bullish' and _sscr >= 7:
                            sentiment_mult = 1.2
                            sentiment_note = (
                                f"📰 News sentiment bullish (score {_sscr}/10, "
                                f"{_scnt} headline{'s' if _scnt != 1 else ''}) → size +20%"
                            )
                            print(f"[Sentiment] {symbol}: bullish {_sscr}/10 → 1.2× size boost")
                        elif _sdir == 'bearish' and _sscr >= 7:
                            sentiment_mult = 0.5
                            sentiment_note = (
                                f"📰 News sentiment bearish (score {_sscr}/10, "
                                f"{_scnt} headline{'s' if _scnt != 1 else ''}) → size −50%"
                            )
                            print(f"[Sentiment] {symbol}: bearish {_sscr}/10 → 0.5× reduction")

                # Grok X/Twitter sentiment gate — same weighting as news sentiment
                grok_mult = 1.0
                grok_note = None
                if signal.get('signal') == 'buy':
                    _grok = await asyncio.to_thread(
                        get_grok_sentiment, self._db_engine, symbol
                    )
                    if _grok:
                        _gdir = _grok.get('direction', 'neutral')
                        _gscr = int(_grok.get('score', 0))
                        if _gdir == 'bullish' and _gscr >= 7:
                            grok_mult = 1.2
                            grok_note = f"🐦 Grok X/Twitter bullish (score {_gscr}/10) → size +20%"
                            print(f"[GrokSentiment] {symbol}: bullish {_gscr}/10 → 1.2× boost")
                        elif _gdir == 'bearish' and _gscr >= 7:
                            grok_mult = 0.5
                            grok_note = f"🐦 Grok X/Twitter bearish (score {_gscr}/10) → size −50%"
                            print(f"[GrokSentiment] {symbol}: bearish {_gscr}/10 → 0.5× reduction")

                # ── Enhanced signal quality scoring (Task 5) ────────────────────
                # Composite 0-10 score from technical/sentiment/regime/insider/volume.
                # Gates buys below SIGNAL_QUALITY_MIN_SCORE and scales size 0.5x-1.5x.
                quality_mult = 1.0
                composite_val = None
                quality_note = None
                if Config.SIGNAL_QUALITY_ENABLED and signal.get('signal') == 'buy':
                    try:
                        import pandas_ta as _ta
                        _es_p = int(getattr(strategy, 'ema_short', 50) or 50)
                        _el_p = int(getattr(strategy, 'ema_long', 200) or 200)
                        _closes = data['close']
                        _es_series = _ta.ema(_closes, length=_es_p)
                        _el_series = _ta.ema(_closes, length=_el_p)
                        _es_val = float(_es_series.iloc[-1]) if _es_series is not None and len(_es_series) and pd.notna(_es_series.iloc[-1]) else None
                        _el_val = float(_el_series.iloc[-1]) if _el_series is not None and len(_el_series) and pd.notna(_el_series.iloc[-1]) else None
                        _gscore = float(_grok.get('score')) if (_grok and _grok.get('score') is not None) else None
                        # Regime alignment from validated_strategies (fail-open -> neutral).
                        _rg_ok, _rg_valid, _rg_has = await asyncio.to_thread(
                            self._regime_gate_ok, symbol, self._current_regime_class
                        )
                        _regime_aligned = _rg_ok if _rg_has else None
                        try:
                            _cur_vol = float(data['volume'].iloc[-1])
                            _adv_val = float(data['volume'].tail(20).mean())
                        except (KeyError, IndexError, ValueError, TypeError):
                            _cur_vol = _adv_val = None
                        # Evolved GP indicator (Task 7) — extra composite feature when a
                        # graduated indicator exists for this symbol (fail-open -> None).
                        _evolved = None
                        try:
                            from discovery.evolved_features import get_evolved_score
                            _evolved = await asyncio.to_thread(
                                get_evolved_score, symbol, data, 'long', self._db_engine,
                                (self._current_regime_class or 'any'),
                            )
                        except Exception as _eve:
                            print(f"[Signal] {symbol}: evolved feature failed (fail-open): {_eve}")
                        sq = signal_quality.evaluate(
                            rsi=signal.get('rsi_at_entry'),
                            macd_histogram=signal.get('macd_histogram_at_entry'),
                            ema_short_val=_es_val, ema_long_val=_el_val,
                            grok_score=_gscore,
                            validated_for_regime=_regime_aligned,
                            insider_aligned=None,   # no historical Form 4 feed wired yet
                            current_volume=_cur_vol, adv=_adv_val,
                            evolved_score=_evolved,
                            direction='long',
                            min_score=Config.SIGNAL_QUALITY_MIN_SCORE,
                        )
                        composite_val = round(sq.composite, 2)
                        _c = sq.components
                        print(
                            f"[Signal] {symbol} composite score={sq.composite:.1f}/10 | "
                            f"tech={_c['technical']} sent={_c['sentiment']} regime={_c['regime']} "
                            f"insider={_c['insider']} vol={_c['volume']}"
                            + (f" evolved={_c['evolved']}" if 'evolved' in _c else "")
                        )
                        if Config.SIGNAL_QUALITY_GATING_ENABLED:
                            if not sq.passes:
                                _sq_msg = (f"Signal quality {sq.composite:.1f} < "
                                           f"{Config.SIGNAL_QUALITY_MIN_SCORE} — skip")
                                print(f"[Signal] {symbol}: {_sq_msg}")
                                asyncio.create_task(notifications.notify_trade_skipped(
                                    symbol, strategy.name, _sq_msg))
                                continue
                            quality_mult = sq.size_multiplier
                            quality_note = f"🎯 Signal quality {sq.composite:.1f}/10 → size {quality_mult:.2f}×"
                    except Exception as _sqe:
                        import traceback as _tb
                        print(f"[Signal] {symbol}: quality scoring failed (fail-open): {_sqe}\n{_tb.format_exc()}")

                _notes = [n for n in [
                    getattr(strategy, 'discovery_size_note', None),
                    getattr(strategy, 'earnings_size_note', None),
                    getattr(strategy, 'bear_market_note', None),
                    signal.get('si_boost_note'),
                    signal.get('si_wow_note'),
                    vix_note,
                    perf_note,
                    kelly_note,
                    sentiment_note,
                    grok_note,
                    quality_note,
                ] if n]

                # Short selling: when a sell signal fires with no open long position,
                # execute a short sale instead of silently skipping.
                _skip_execute_trade = False
                if signal['signal'] == 'sell':
                    _has_long = False
                    try:
                        _pos = await asyncio.to_thread(
                            self.trading_client.get_open_position, symbol
                        )
                        _pos_side = str(getattr(_pos, 'side', '')).lower()
                        if _pos_side == 'short':
                            # Already short — block duplicate entry.
                            # PositionSide is a str-enum; 'short' == 'long' is always False,
                            # so without this check _has_long=False would trigger _execute_short
                            # again even when a short already exists.
                            print(
                                f"[Swing] {symbol}: already in SHORT position — skipping re-entry"
                            )
                            continue
                        _has_long = (_pos_side == 'long')
                    except Exception:
                        _has_long = False

                    if not _has_long:
                        _skip_execute_trade = True
                        if not Config.SHORT_SELLING_ENABLED:
                            print(
                                f"[Swing] SHORT skipped for {symbol} "
                                "— SHORT_SELLING_ENABLED=False"
                            )
                        else:
                            _shortable = False
                            try:
                                _asset = await asyncio.to_thread(
                                    self.trading_client.get_asset, symbol
                                )
                                _shortable = (
                                    bool(getattr(_asset, "shortable", False))
                                    and bool(getattr(_asset, "easy_to_borrow", False))
                                )
                            except Exception as _ae:
                                print(f"[Swing] {symbol}: asset shortability check failed — {_ae}")
                            if not _shortable:
                                print(
                                    f"[Swing] SHORT {symbol} skipped "
                                    "— not shortable/hard to borrow"
                                )
                            else:
                                try:
                                    await self._execute_short(
                                        symbol, signal, strategy,
                                        risk_percent, stop_loss_percent, data,
                                    )
                                except Exception as _se:
                                    import traceback as _tb
                                    print(
                                        f"[Swing] {symbol}: _execute_short raised "
                                        f"— {_se}\n{_tb.format_exc()}"
                                    )

                # Liquidity filter + ADV cap (stocks only; skipped for crypto)
                if not _skip_execute_trade and not is_crypto:
                    _adv = await self._get_adv(symbol)
                    _ep  = float(signal.get('entry_price') or 0)
                    _dollar_vol = _adv * _ep if _adv > 0 and _ep > 0 else 0.0
                    if 0 < _dollar_vol < Config.MIN_DOLLAR_VOLUME:
                        _liq_msg = (
                            f"[Liquidity] {symbol} skipped — "
                            f"avg daily dollar volume ${_dollar_vol / 1e6:.1f}M below $10M minimum"
                        )
                        print(_liq_msg)
                        asyncio.create_task(notifications.notify_trade_skipped(
                            symbol, strategy.name, _liq_msg
                        ))
                        continue
                    if _adv > 0 and _ep > 0:
                        _adv_cap = max(1, int(_adv * 0.01))
                        # Keep the most restrictive of the ADV cap and any single-
                        # position cap (Task 8) already set on the signal.
                        _prior_cap = signal.get('adv_cap_shares')
                        signal['adv_cap_shares'] = min(_prior_cap, _adv_cap) if _prior_cap else _adv_cap

                entry_time = datetime.now(pytz.utc)
                if not _skip_execute_trade:
                    try:
                        earnings_mult   = getattr(strategy, 'earnings_override_multiplier', 1.0)
                        debate_mult     = getattr(strategy, 'debate_size_multiplier', 1.0)
                        confidence_mult = signal.get('confidence_multiplier', 1.0)
                        # Decay multiplier stacks with the rest, floored at 0.1x.
                        decay_mult = max(_decay_mult, Config.DECAY_MULTIPLIER_FLOOR) if _decay_mult < 1.0 else 1.0
                        if decay_mult < 1.0:
                            print(f"[Decay] {symbol} applying decay multiplier {decay_mult}x (status={_decay_status})")
                        short_int_mult = signal.get('short_interest_size_mult', 1.0)
                        scaled_risk_percent = (
                            risk_percent * self.risk_multiplier
                            * earnings_mult * vix_risk_mult * confidence_mult
                            * perf_mult * debate_mult * sentiment_mult * grok_mult
                            * decay_mult * quality_mult * weekly_loss_mult * short_int_mult
                        )
                        # Floor: no trade can go below 10% of normal size regardless of stacked multipliers
                        scaled_risk_percent = max(
                            scaled_risk_percent,
                            Config.SWING_EQUITY_RISK_PERCENT * Config.POSITION_SIZE_FLOOR,
                        )
                        executed_qty = await asyncio.to_thread(
                            strategy.execute_trade,
                            signal,
                            self.trading_client,
                            scaled_risk_percent,
                            stop_loss_percent,
                            Config.TAKE_PROFIT_PERCENT,
                            Config.MAX_BUYING_POWER_UTILIZATION_PERCENT,
                        )

                        # Prometheus counter: confirmed buy execution
                        if signal['signal'] == 'buy':
                            _health_state["signals_fired_total"] += 1

                        # Execution confirmation — fires only after Alpaca accepts the order
                        if executed_qty:
                            _gates = (
                                "debate: passed | fundamentals: passed"
                                if pre_execute_hook else "gates: passed"
                            )
                            print(f"[Slack] Attempting trade notification for {symbol}")
                            asyncio.create_task(notifications.notify_trade_executed(
                                symbol, "LONG",
                                float(signal.get('entry_price', 0)),
                                float(signal.get('stop_price', 0)),
                                float(signal.get('target_price', 0)),
                                executed_qty,
                                _gates,
                            ))

                        # Log entry to signal_outcomes only after a confirmed fill
                        if executed_qty and signal['signal'] == 'buy' and signal_type:
                            regime = await self._get_market_regime()
                            row_id = await asyncio.to_thread(
                                self._log_trade_entry,
                                symbol, signal_type, float(signal.get('entry_price', 0)),
                                getattr(strategy, 'ema_short', 50), getattr(strategy, 'ema_long', 200),
                                float(signal.get('rsi_at_entry', 0)), float(signal.get('macd_at_entry', 0)),
                                regime, entry_time, self._current_regime_class, decay_mult,
                                composite_val,
                            )
                            if row_id:
                                async with self._trade_ids_lock:
                                    self._open_trade_ids[symbol] = (row_id, float(signal.get('entry_price', 0)), entry_time)
                                asyncio.create_task(notion_journal.post_trade_to_notion({
                                    "symbol":        symbol,
                                    "signal_type":   signal_type,
                                    "entry_price":   float(signal.get('entry_price', 0)),
                                    "entry_time":    entry_time,
                                    "stop_price":    float(signal.get('stop_price', 0)),
                                    "target_price":  float(signal.get('target_price', 0)),
                                    "position_size": round(scaled_risk_percent, 4),
                                    "market_regime": regime,
                                    "signal_source": strategy.name,
                                    "reasoning":     signal.get('reasoning', ''),
                                }))

                    except Exception as e:
                        msg = f"Error executing trade for {symbol}: {e}"
                        print(msg)
                        asyncio.create_task(notifications.notify_alert(msg))

                # Record the time the signal was generated
                self.active_signals[signal_key] = datetime.now(pytz.utc)

    async def _get_stronger_momentum_crypto(self):
        now = datetime.now(pytz.utc)
        if hasattr(self, '_momentum_winner_cache') and hasattr(self, '_momentum_winner_time') and (now - self._momentum_winner_time).total_seconds() < 300:
            return self._momentum_winner_cache
            
        import pandas_ta as ta
        from utils import get_historical_bars
        from alpaca.data.timeframe import TimeFrame
        
        rsi_scores = {}
        for sym in Config.SCALP_SYMBOLS:
            df = get_historical_bars(sym, TimeFrame.Hour, 7, self.crypto_data_client, is_crypto=True)
            if df is not None and len(df) > 14:
                df['RSI'] = ta.rsi(df['close'], length=14)
                rsi_scores[sym] = df['RSI'].iloc[-1]
                
        if len(rsi_scores) == 2:
            winner = max(rsi_scores, key=rsi_scores.get)
            self._momentum_winner_cache = winner
            self._momentum_winner_time = now
            return winner
        return None

    async def scalp_loop(self):
        if not Config.SCALP_ENABLED:
            print("[Scalp] Disabled via config — skipping crypto scalp loop.")
            return
        print(f"[Scalp] Starting crypto REST poll loop for {', '.join(Config.SCALP_SYMBOLS)} (60s interval, 24/7)...")
        _health_state["crypto_polling_active"] = True

        while True:
            try:
                # Fetch latest 1-min bar for each symbol to get current price for logging
                btc_bars = get_historical_bars("BTC/USD", TimeFrame.Minute, 1, self.crypto_data_client, is_crypto=True)
                eth_bars = get_historical_bars("ETH/USD", TimeFrame.Minute, 1, self.crypto_data_client, is_crypto=True)

                btc_price = float(btc_bars['close'].iloc[-1]) if btc_bars is not None and len(btc_bars) > 0 else None
                eth_price = float(eth_bars['close'].iloc[-1]) if eth_bars is not None and len(eth_bars) > 0 else None

                btc_str = f"BTC/USD={btc_price:.2f}" if btc_price else "BTC/USD=N/A"
                eth_str = f"ETH/USD={eth_price:.2f}" if eth_price else "ETH/USD=N/A"

                winner = await self._get_stronger_momentum_crypto()
                if winner is None:
                    winner = "BTC/USD"

                print(f"[Scalp] REST poll — {btc_str} | {eth_str} | evaluating {winner}")

                _now = datetime.now(pytz.utc)
                last_trade = self._crypto_poll_cooldowns.get(winner)
                if last_trade and (_now - last_trade).total_seconds() < 15 * 60:
                    remaining = int(15 * 60 - (_now - last_trade).total_seconds())
                    print(f"[Scalp] {winner}: cooldown active — {remaining}s remaining")
                else:
                    self._crypto_poll_cooldowns[winner] = _now
                    current_price = btc_price if winner == "BTC/USD" else eth_price
                    await self._process_symbol(
                        winner,
                        self.scalp_strategies,
                        is_crypto=True,
                        risk_percent=Config.EQUITY_RISK_PER_TRADE_PERCENT,
                        stop_loss_percent=Config.CRYPTO_SCALP_STOP_LOSS_PERCENT,
                        current_price=current_price,
                    )
            except Exception as e:
                print(f"[Scalp] REST poll error: {e}")

            await asyncio.sleep(60)

    async def trailing_stop_monitor_loop(self):
        print("🛡️ Starting Trailing Stop Monitor Loop...")
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        while True:
            await asyncio.sleep(Config.TRAILING_STOP_MONITOR_INTERVAL)
            try:
                positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                _health_state["open_positions"] = len(positions)
                for pos in positions:
                    unrealized_pct = float(pos.unrealized_plpc)
                    if unrealized_pct >= Config.TRAILING_STOP_ACTIVATION_PCT:
                        req = GetOrdersRequest(
                            status=QueryOrderStatus.OPEN,
                            symbols=[pos.symbol]
                        )
                        orders = await asyncio.to_thread(self.trading_client.get_orders, req)
                        for order in orders:
                            if order.order_type.value in ("stop", "stop_limit"):
                                msg = f"Activating Trailing Stop for {pos.symbol} at {unrealized_pct*100:.2f}% profit!"
                                print(msg)
                                asyncio.create_task(notifications.notify_alert(msg, level="INFO"))
                                await asyncio.to_thread(self.trading_client.cancel_order_by_id, order.id)
                                new_sl = TrailingStopOrderRequest(
                                    symbol=pos.symbol,
                                    qty=abs(float(pos.qty)),
                                    side=OrderSide.SELL if pos.side == "long" else OrderSide.BUY,
                                    time_in_force=TimeInForce.GTC,
                                    trail_percent=Config.TRAILING_STOP_TRAIL_PCT * 100
                                )
                                await asyncio.to_thread(self.trading_client.submit_order, new_sl)
            except Exception as e:
                print(f"[TrailingStop] Error: {e}")

    # ── Task 1 — exit monitor: updates signal_outcomes when positions close ───

    async def _check_unprotected_positions(self) -> None:
        """Scan all open positions for missing stop-loss protection.

        Any position with no stop order (and no OCO stop leg) gets an emergency
        ATR-based OCO placed immediately. Sends a PagerDuty CRITICAL alert per
        symbol. Fail-open: errors log and trading continues.
        """
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        import numpy as np

        def _val(attr):
            return getattr(attr, 'value', str(attr)).lower() if attr is not None else ''

        try:
            all_positions = await asyncio.to_thread(self.trading_client.get_all_positions)
            if not all_positions:
                return

            open_orders = await asyncio.to_thread(
                self.trading_client.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True),
            )

            # Index live positions by symbol — only symbols present here are truly open.
            pos_by_sym: dict[str, object] = {p.symbol: p for p in all_positions}

            # Map symbol → whether a stop order exists (including OCO child legs)
            stop_protected: dict[str, bool] = {}
            for o in open_orders:
                sym = o.symbol
                has_stop = 'stop' in _val(getattr(o, 'order_type', None))
                if not has_stop:
                    for leg in (getattr(o, 'legs', None) or []):
                        if 'stop' in _val(getattr(leg, 'order_type', None)):
                            has_stop = True
                            break
                if has_stop:
                    stop_protected[sym] = True
                elif sym not in stop_protected:
                    stop_protected[sym] = False

            unprotected = []
            for p in all_positions:
                sym = p.symbol
                if stop_protected.get(sym, False):
                    continue
                # Skip if all shares are already committed to another order
                # (e.g. a pending market liquidation) — abs(qty_available)==0 means
                # Alpaca would reject any new order anyway. Short positions report
                # qty_available as negative, so use abs() for the zero-check.
                _qty_avail_raw = getattr(p, 'qty_available', None)
                qty_avail = abs(float(_qty_avail_raw)) if _qty_avail_raw is not None else abs(float(p.qty))
                if qty_avail == 0:
                    print(f"[Safety] {sym}: no stop-loss but qty_available=0 (liquidation in progress?) — skipping")
                    continue
                unprotected.append(p)

            if not unprotected:
                return

            print(f"[Safety] Found {len(unprotected)} position(s) with no stop-loss — placing emergency OCOs")

            dc = StockHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
            end_dt = datetime.now(pytz.utc)
            start_dt = end_dt - timedelta(days=45)

            for p in unprotected:
                sym = p.symbol
                # Guard: re-verify position still exists (race between check and action)
                if sym not in pos_by_sym:
                    print(f"[Safety] {sym}: position disappeared before OCO placement — skipping")
                    continue
                entry  = float(p.avg_entry_price)
                current = float(p.current_price)
                qty    = qty_avail   # abs value already computed above
                is_short = _val(getattr(p, 'side', None)) == 'short'

                # ATR(14) from daily bars; fallback to 2% of price
                atr = current * 0.02
                try:
                    bars = await asyncio.to_thread(
                        dc.get_stock_bars,
                        StockBarsRequest(
                            symbol_or_symbols=sym,
                            timeframe=TimeFrame.Day,
                            start=start_dt,
                            end=end_dt,
                        ),
                    )
                    df = bars.df
                    if hasattr(df.index, 'levels'):
                        df = df.xs(sym, level=0)
                    if len(df) >= 15:
                        h, l, c = df['high'].values, df['low'].values, df['close'].values
                        tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(df))]
                        atr = float(np.mean(tr[-14:]))
                except Exception:
                    pass

                if is_short:
                    raw_stop = entry + 2.0 * atr
                    stop_price   = round(max(raw_stop, current * 1.005), 2)
                    target_price = round(max(entry - 5.0 * atr, 0.01), 2)
                    order_side   = OrderSide.BUY
                else:
                    raw_stop = entry - 2.0 * atr
                    stop_price   = round(min(raw_stop, current * 0.995), 2)
                    target_price = round(entry + 5.0 * atr, 2)
                    order_side   = OrderSide.SELL

                try:
                    oco = await asyncio.to_thread(
                        self.trading_client.submit_order,
                        LimitOrderRequest(
                            symbol=sym,
                            qty=qty,
                            side=order_side,
                            time_in_force=TimeInForce.GTC,
                            order_class=OrderClass.OCO,
                            limit_price=target_price,
                            take_profit=TakeProfitRequest(limit_price=target_price),
                            stop_loss=StopLossRequest(stop_price=stop_price),
                        ),
                    )
                    msg = (
                        f"\U0001f6a8 EMERGENCY — {sym} had no stop-loss protection, "
                        f"emergency OCO placed | stop={stop_price} target={target_price} | "
                        f"id={str(oco.id)[:12]}"
                    )
                    print(f"[Safety] {msg}")
                    asyncio.create_task(notifications.notify_alert(msg, level="CRITICAL"))
                except Exception as _oco_e:
                    err_msg = f"[Safety] Emergency OCO for {sym} FAILED: {_oco_e}"
                    print(err_msg)
                    asyncio.create_task(notifications.notify_alert(err_msg, level="CRITICAL"))

        except Exception as _e:
            print(f"[Safety] Unprotected position check failed (non-fatal): {_e}")

    async def _cancel_orphaned_oco_orders(self) -> None:
        """Cancel BUY limit/stop OCO orders whose short position no longer exists.

        A short's OCO legs are BUY orders (buy-to-cover). If the short is closed
        (covered at market, hit stop, or expired) without the OCO being cancelled,
        the BUY orders stay open indefinitely and could incorrectly open a long.
        Called every 10 minutes from _exit_monitor_loop.
        """
        def _val(attr):
            return getattr(attr, 'value', str(attr)).lower() if attr is not None else ''

        try:
            all_positions = await asyncio.to_thread(self.trading_client.get_all_positions)
            short_syms = {
                p.symbol for p in all_positions
                if _val(getattr(p, 'side', None)) == 'short'
            }
            open_orders = await asyncio.to_thread(
                self.trading_client.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN),
            )
            orphaned = [
                o for o in open_orders
                if _val(getattr(o, 'side', None)) == 'buy'
                and any(k in _val(getattr(o, 'order_type', None)) for k in ('limit', 'stop'))
                and o.symbol not in short_syms
            ]
            if orphaned:
                print(f"[Cleanup] Found {len(orphaned)} orphaned OCO order(s) — cancelling")
                for o in orphaned:
                    otype = _val(getattr(o, 'order_type', None))
                    qty   = getattr(o, 'qty', '?')
                    price = getattr(o, 'limit_price', None) or getattr(o, 'stop_price', None) or '?'
                    try:
                        await asyncio.to_thread(self.trading_client.cancel_order_by_id, o.id)
                        print(f"[Cleanup] Cancelled orphaned OCO for {o.symbol} — {otype} @ {price} qty={qty}")
                    except Exception as _ce:
                        print(f"[Cleanup] Could not cancel OCO for {o.symbol} — {_ce}")
        except Exception as _e:
            print(f"[Cleanup] Orphaned OCO check failed (non-fatal): {_e}")

    async def _exit_monitor_loop(self):
        print("[DB] Exit monitor loop started (10-min polling)")
        while True:
            await asyncio.sleep(600)
            async with self._trade_ids_lock:
                open_ids_snapshot = dict(self._open_trade_ids)

            # Daily EV check — runs regardless of open positions.
            # Alert set is reset each day so a strategy that recovers then turns
            # negative again will fire a new alert.
            today = datetime.now(pytz.timezone('America/New_York')).date()
            if today != self._last_ev_check_date:
                self._alerted_negative_ev.clear()
                self._last_ev_check_date = today
                await self._calculate_strategy_ev()

            # Health watchdog — if daily health report hasn't fired in 25+ hours,
            # fire a CRITICAL PagerDuty alert (covers Railway crash/restart scenarios).
            last_hr = _health_state.get("last_health_report_utc")
            if last_hr is not None:
                last_hr_dt = datetime.fromisoformat(last_hr)
                if (datetime.now(pytz.utc) - last_hr_dt).total_seconds() > 25 * 3600:
                    asyncio.create_task(notifications.notify_alert(
                        "Health report has not fired in 25+ hours — bot may have crashed or restarted.",
                        level="CRITICAL",
                    ))
                    _health_state["last_health_report_utc"] = datetime.now(pytz.utc).isoformat()

            # Orphaned OCO cleanup — runs every cycle regardless of open trade IDs.
            await self._cancel_orphaned_oco_orders()

            # Safety net — emergency OCO for any position missing stop protection.
            await self._check_unprotected_positions()

            if not open_ids_snapshot:
                continue
            try:
                req = GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED,
                    limit=200,
                    after=datetime.now(pytz.utc) - timedelta(days=7),
                )
                orders = await asyncio.to_thread(self.trading_client.get_orders, req)
                for order in orders:
                    sym = order.symbol
                    if sym not in open_ids_snapshot:
                        continue
                    if order.status.value != 'filled':
                        continue
                    if not hasattr(order, 'side') or order.side.value != 'sell':
                        continue

                    async with self._trade_ids_lock:
                        if sym not in self._open_trade_ids:
                            continue  # Already processed by a concurrent iteration
                        row_id, entry_price, entry_time = self._open_trade_ids.pop(sym)

                    if row_id is None:
                        continue

                    exit_price = float(order.filled_avg_price) if order.filled_avg_price else 0.0
                    exit_time  = order.filled_at or datetime.now(pytz.utc)
                    pnl_pct    = (exit_price - entry_price) / entry_price * 100 if entry_price else 0.0

                    order_type = order.order_type.value if hasattr(order, 'order_type') else 'unknown'
                    if order_type in ('stop', 'trailing_stop'):
                        exit_reason = 'stop'
                    elif order_type == 'limit':
                        exit_reason = 'target'
                    else:
                        exit_reason = 'manual'

                    hold_days = int((exit_time - entry_time).total_seconds() / 86400) if entry_time else 0
                    await asyncio.to_thread(
                        self._update_trade_exit,
                        row_id, exit_price, exit_reason, exit_time, hold_days, pnl_pct,
                    )
            except Exception as e:
                print(f"[DB] Exit monitor error: {e}")

    async def _get_discovery_risk_multiplier(
        self,
        symbol: str,
        strategy_type: str,
        backtest_win_rate: float | None,
    ) -> tuple[float, str]:
        """
        Returns (position_size_multiplier, log_reason) for a discovery strategy.
        Ramps from 25% → 50% → 100% of SWING_EQUITY_RISK_PERCENT based on
        live trade count and win-rate parity with the backtest result.
        """
        if not self._db_engine:
            return 0.25, "no DB — safe default 25%"

        signal_type = f"discovery_{strategy_type}"
        try:
            with self._db_engine.connect() as conn:
                row = conn.execute(sql_text("""
                    SELECT COUNT(*) AS total,
                           COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::float
                               / NULLIF(COUNT(*), 0) AS live_win_rate
                    FROM signal_outcomes
                    WHERE signal_type = :st
                      AND exit_time IS NOT NULL
                      AND pnl_pct IS NOT NULL
                """), {"st": signal_type}).mappings().fetchone()

            trade_count   = int(row["total"]) if row else 0
            live_win_rate = float(row["live_win_rate"]) if (row and row["live_win_rate"] is not None) else None

            if trade_count < 50:
                return 0.25, f"{trade_count}/50 trades — using 25% position size"

            if trade_count < 100:
                return 0.50, f"{trade_count}/100 trades — using 50% position size"

            # ≥100 trades: full size only if win rate is within 15% of backtest
            if backtest_win_rate is not None and live_win_rate is not None:
                diff = abs(live_win_rate - backtest_win_rate)
                if diff <= 0.15:
                    return 1.00, (
                        f"{trade_count} trades — live win rate {live_win_rate:.1%} "
                        f"within 15% of backtest {backtest_win_rate:.1%} — using 100% position size"
                    )
                else:
                    return 0.50, (
                        f"{trade_count} trades — live win rate {live_win_rate:.1%} "
                        f"diverged from backtest {backtest_win_rate:.1%} — staying at 50% position size"
                    )

            return 1.00, f"{trade_count} trades — using 100% position size (no backtest win rate to compare)"

        except Exception as e:
            print(f"[Swing] Discovery risk multiplier query failed: {e}")
            return 0.25, f"DB error — safe default 25%"

    async def _calculate_strategy_ev(self) -> dict[str, float]:
        """
        Computes EV = (win_rate × avg_win_pct) − (loss_rate × avg_loss_pct)
        per signal_type for strategies with ≥ 20 closed trades.
        Fires a #trading-alerts warning if EV turns negative (once per day per strategy).
        Resets alert set daily so recovery → negative cycles re-alert.
        """
        if not self._db_engine:
            return {}
        try:
            with self._db_engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT signal_type,
                           COUNT(*) AS total,
                           AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct       ELSE NULL END) AS avg_win,
                           AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct)  ELSE NULL END) AS avg_loss,
                           COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::float
                               / COUNT(*) AS win_rate
                    FROM signal_outcomes
                    WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    GROUP BY signal_type
                    HAVING COUNT(*) >= 20
                """)).mappings().fetchall()

            ev_map: dict[str, float] = {}
            for row in rows:
                wr       = float(row["win_rate"]  or 0)
                avg_win  = float(row["avg_win"]   or 0)
                avg_loss = float(row["avg_loss"]  or 0)
                ev       = round((wr * avg_win) - ((1.0 - wr) * avg_loss), 4)
                ev_map[row["signal_type"]] = ev

                if ev < 0 and row["signal_type"] not in self._alerted_negative_ev:
                    self._alerted_negative_ev.add(row["signal_type"])
                    asyncio.create_task(notifications.notify_alert(
                        f"Strategy EV negative: {row['signal_type']} "
                        f"EV = {ev:.2f}% over {row['total']} trades. Consider disabling.",
                        level="WARNING",
                    ))
                    print(
                        f"[EV] ALERT — {row['signal_type']}: EV={ev:.2f}% "
                        f"(wr={wr:.1%} avg_win={avg_win:.2f}% avg_loss={avg_loss:.2f}%)"
                    )
                else:
                    print(
                        f"[EV] {row['signal_type']}: EV={ev:.2f}% "
                        f"(wr={wr:.1%} avg_win={avg_win:.2f}% avg_loss={avg_loss:.2f}% "
                        f"n={row['total']})"
                    )

            return ev_map

        except Exception as e:
            print(f"[EV] Calculation failed: {e}")
            return {}

    async def _check_sector_alert(self, symbol: str):
        """
        Fires a #trading-alerts message when ≥3 distinct symbols from the same GICS sector
        generate buy signals within a 30-minute window. Cooldown: once per sector per 30 min.
        """
        sector = _SECTOR_MAP.get(symbol)
        if not sector or sector == "Market":
            return

        now    = datetime.now(pytz.utc)
        window = timedelta(minutes=30)

        recent_syms = {
            sym for sym, ts in self._recent_signals
            if now - ts <= window and _SECTOR_MAP.get(sym) == sector
        }

        if len(recent_syms) < 4:
            return

        last_alert = self._sector_alert_cooldown.get(sector)
        if last_alert and now - last_alert < window:
            return

        self._sector_alert_cooldown[sector] = now
        syms_str = ", ".join(sorted(recent_syms))
        await notifications.notify_alert(
            f"🔥 Sector hot: {sector} — {len(recent_syms)} signals in 30min "
            f"({syms_str}). Possible sector rotation.",
            level="WARNING",
        )

    async def _push_signal_stack(
        self, ticker: str, source: str, strength: float
    ) -> tuple[bool, float]:
        """
        Registers an auto-trade signal for ticker from source.
        Returns (is_stacked, size_multiplier).

        Fires a #trading-alerts stack notification when ≥2 distinct sources have
        filed auto-trade signals on the same ticker within 30 minutes.
        size_multiplier is 1.3 when stacked, else 1.0.
        """
        now    = datetime.now(pytz.utc)
        cutoff = now - timedelta(minutes=30)

        existing = self._signal_stack.get(ticker, [])
        fresh    = [e for e in existing if e["timestamp"] >= cutoff]

        # Don't double-count the same source (e.g., two news headlines for COST)
        if not any(e["source"] == source for e in fresh):
            fresh.append({"source": source, "strength": strength, "timestamp": now})
        self._signal_stack[ticker] = fresh

        distinct_sources = {e["source"] for e in fresh}
        if len(distinct_sources) < 2:
            return False, 1.0

        # ≥2 distinct auto-trade sources — fire stacked conviction alert
        combined = round(sum(e["strength"] for e in fresh), 1)
        parts    = " + ".join(
            f"{e['source'].upper()} (strength {e['strength']:.1f})" for e in fresh
        )
        msg = (
            f"🎯 Signal stack: {ticker} — {parts} = combined conviction {combined}. "
            f"High confidence entry."
        )
        print(f"[SignalStack] {msg}")
        asyncio.create_task(notifications.notify_alert(msg, level="WARNING"))
        return True, 1.3

    async def _record_daily_signal(self, symbol: str, source: str) -> None:
        """Track day-level confluence. Fires a #trading-alerts alert when ≥2 distinct
        sources (Swing technical, News, EDGAR) fire on the same ticker the same day."""
        today = datetime.now(pytz.timezone('America/New_York')).date()
        if today != self._last_daily_signals_date:
            self._daily_signals.clear()
            self._confluence_alerted.clear()
            self._last_daily_signals_date = today
        self._daily_signals.setdefault(symbol, set()).add(source)
        sources = self._daily_signals[symbol]
        if len(sources) >= 2 and symbol not in self._confluence_alerted:
            self._confluence_alerted.add(symbol)
            sources_str = " + ".join(sorted(sources))
            print(f"[Confluence] {symbol}: {sources_str}")
            asyncio.create_task(notifications.notify_alert(
                f"🎯 Confluence: {symbol} — {sources_str} both fired today. High conviction setup.",
                level="WARNING",
            ))

    def _upload_image_to_public(self, file_path: str) -> str | None:
        """POSTs an image file to 0x0.st and returns the public URL, or None on failure."""
        try:
            with open(file_path, "rb") as f:
                resp = _requests.post("https://0x0.st", files={"file": f}, timeout=30)
            resp.raise_for_status()
            url = resp.text.strip()
            return url if url.startswith("http") else None
        except Exception as e:
            print(f"[Heatmap] Upload to 0x0.st failed: {e}")
            return None

    def _generate_correlation_heatmap_sync(self) -> str | None:
        """
        Queries signal_outcomes for daily avg pnl_pct per signal_type,
        builds a correlation matrix, generates a seaborn heatmap in dark theme,
        saves to discovery/data/charts/ and returns the file path.
        Requires ≥2 signal types each with ≥10 closed trades.
        """
        if not self._db_engine:
            return None
        try:
            import seaborn as sns
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from pathlib import Path

            with self._db_engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT DATE(entry_time) AS trade_date,
                           signal_type,
                           AVG(pnl_pct) AS avg_pnl
                    FROM signal_outcomes
                    WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    GROUP BY DATE(entry_time), signal_type
                """)).fetchall()

            if not rows:
                print("[Heatmap] No closed trades in signal_outcomes — skipping heatmap")
                return None

            df = pd.DataFrame(rows, columns=["trade_date", "signal_type", "avg_pnl"])
            counts = df.groupby("signal_type")["avg_pnl"].count()
            valid_types = counts[counts >= 10].index.tolist()
            if len(valid_types) < 2:
                print("[Heatmap] Not enough signal types with ≥10 closed trades for correlation matrix")
                return None

            df = df[df["signal_type"].isin(valid_types)]
            pivot = df.pivot(index="trade_date", columns="signal_type", values="avg_pnl")
            corr = pivot.corr()

            n = len(valid_types)
            fig_w = max(6, n * 1.5)
            fig_h = max(5, n * 1.2)
            fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor="#0d1117")
            ax.set_facecolor("#0d1117")

            sns.heatmap(
                corr,
                ax=ax,
                cmap="RdYlGn",
                annot=True,
                fmt=".2f",
                vmin=-1,
                vmax=1,
                linewidths=0.5,
                linecolor="#30363d",
                annot_kws={"size": 9, "color": "#e6edf3"},
                cbar_kws={"shrink": 0.8},
            )
            ax.tick_params(colors="#e6edf3", labelsize=9)
            ax.set_title("Signal P&L Correlation Matrix", color="#e6edf3", fontsize=12, pad=12)
            ax.set_xlabel("")
            ax.set_ylabel("")
            cbar = ax.collections[0].colorbar
            cbar.ax.tick_params(colors="#8b949e")
            plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
            plt.setp(ax.get_yticklabels(), rotation=0)

            plt.tight_layout(pad=1.5)

            charts_dir = Path("discovery/data/charts")
            charts_dir.mkdir(parents=True, exist_ok=True)
            fpath = str(charts_dir / f"correlation_heatmap_{datetime.now().strftime('%Y%m%d')}.png")
            plt.savefig(fpath, dpi=100, bbox_inches="tight", facecolor="#0d1117")
            plt.close(fig)
            print(f"[Heatmap] Saved {fpath}")
            return fpath
        except Exception as e:
            print(f"[Heatmap] Generation failed: {e}")
            return None

    async def _generate_correlation_heatmap(self):
        """Async wrapper: generate heatmap PNG, upload to 0x0.st, post to #trading-health."""
        try:
            file_path = await asyncio.to_thread(self._generate_correlation_heatmap_sync)
            if file_path is None:
                return
            url = await asyncio.to_thread(self._upload_image_to_public, file_path)
            if url:
                await notifications.notify_correlation_heatmap(url)
                print(f"[Heatmap] Posted to #trading-health: {url}")
            else:
                print("[Heatmap] Upload failed — Slack notification skipped")
        except Exception as e:
            print(f"[Heatmap] Unexpected error: {e}")

    def _get_risk_state(self) -> dict:
        """Account-level risk state (Task 8): consecutive losses + weekly P&L proxy.

        Cached for RISK_STATE_CACHE_SECONDS. Weekly P&L is approximated as the sum
        of closed-trade pnl_pct over the last 7 days (equal-weight book proxy — no
        per-trade dollar P&L is stored). Sync — call via asyncio.to_thread.
        """
        now = datetime.now(pytz.utc)
        if self._risk_state_cache:
            ts, cached = self._risk_state_cache
            if (now - ts).total_seconds() < Config.RISK_STATE_CACHE_SECONDS:
                return cached
        state = {"consecutive_losses": 0, "weekly_pnl_pct": 0.0}
        if self._db_engine:
            try:
                with self._db_engine.connect() as conn:
                    recent = conn.execute(sql_text("""
                        SELECT pnl_pct FROM signal_outcomes
                        WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                          AND exit_time >= :baseline
                        ORDER BY exit_time DESC LIMIT 50
                    """), {"baseline": self._consec_loss_baseline_time}).mappings().fetchall()
                    week_ago = now - timedelta(days=7)
                    wk = conn.execute(sql_text("""
                        SELECT COALESCE(SUM(pnl_pct), 0) AS wk FROM signal_outcomes
                        WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                          AND exit_time >= :wa
                    """), {"wa": week_ago}).mappings().fetchone()
                state["consecutive_losses"] = risk_limits.count_leading_losses(
                    [float(r["pnl_pct"]) for r in recent]
                )
                state["weekly_pnl_pct"] = float(wk["wk"]) if wk and wk["wk"] is not None else 0.0
            except Exception as e:
                print(f"[Risk] risk-state query failed (fail-open): {e}")
        self._risk_state_cache = (now, state)
        return state

    def _get_performance_multiplier(self, signal_type: str, symbol: str | None = None,
                                    current_regime: str | None = None) -> float:
        """
        Enhanced Performance Brain (Task 7). Combines three signals into one
        position-size multiplier, clamped to [0.5, 1.5]:

          * momentum (base): wins 3+ of last 5 closed signals → 1.2x (hot),
            loses 3+ of last 5 → 0.7x (cold), otherwise 1.0x (neutral).
          * regime bonus: +0.1x if the current regime has been net-profitable
            historically for this strategy.
          * time-of-day bonus: +0.1x if the current session window (morning
            9:30–11:30 vs afternoon 13:30–16:00) is this strategy's stronger one,
            −0.1x if it's the weaker one.

        Sync — called via asyncio.to_thread from _process_symbol. Fail-open: any
        error or missing data leaves the corresponding term neutral.
        """
        if not Config.PERFORMANCE_SCALING_ENABLED or not self._db_engine:
            return 1.0

        try:
            with self._db_engine.connect() as conn:
                # ── Momentum: last 5 closed signals for this strategy ──────────
                recent = conn.execute(sql_text("""
                    SELECT pnl_pct FROM signal_outcomes
                    WHERE signal_type = :st AND exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    ORDER BY exit_time DESC LIMIT 5
                """), {"st": signal_type}).mappings().fetchall()
                recent_pnls = [float(r["pnl_pct"]) for r in recent]

                # ── Regime stats: is the current regime net-profitable here? ───
                reg_avg, reg_n = None, 0
                if current_regime:
                    rrow = conn.execute(sql_text("""
                        SELECT AVG(pnl_pct) AS avg_pnl, COUNT(*) AS n
                        FROM signal_outcomes
                        WHERE signal_type = :st AND regime_class = :rc
                          AND exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    """), {"st": signal_type, "rc": current_regime}).mappings().fetchone()
                    if rrow:
                        reg_n = int(rrow["n"] or 0)
                        reg_avg = float(rrow["avg_pnl"]) if rrow["avg_pnl"] is not None else None

                # ── Time-of-day stats: morning vs afternoon avg P&L ────────────
                trow = conn.execute(sql_text("""
                    SELECT
                      AVG(CASE WHEN m BETWEEN 570 AND 690 THEN pnl_pct END) AS morning_avg,
                      COUNT(CASE WHEN m BETWEEN 570 AND 690 THEN 1 END)     AS morning_n,
                      AVG(CASE WHEN m BETWEEN 810 AND 960 THEN pnl_pct END) AS afternoon_avg,
                      COUNT(CASE WHEN m BETWEEN 810 AND 960 THEN 1 END)     AS afternoon_n
                    FROM (
                      SELECT pnl_pct,
                             (EXTRACT(HOUR FROM entry_time AT TIME ZONE 'America/New_York') * 60
                              + EXTRACT(MINUTE FROM entry_time AT TIME ZONE 'America/New_York'))::int AS m
                      FROM signal_outcomes
                      WHERE signal_type = :st AND exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    ) t
                """), {"st": signal_type}).mappings().fetchone()

            now_et = datetime.now(pytz.timezone("America/New_York"))
            cur_min = now_et.hour * 60 + now_et.minute
            momentum = performance_brain.momentum_multiplier(recent_pnls)
            reg_bonus = performance_brain.regime_bonus(reg_avg, reg_n)
            t_bonus = performance_brain.time_of_day_bonus(
                float(trow["morning_avg"]) if trow and trow["morning_avg"] is not None else None,
                int(trow["morning_n"] or 0) if trow else 0,
                float(trow["afternoon_avg"]) if trow and trow["afternoon_avg"] is not None else None,
                int(trow["afternoon_n"] or 0) if trow else 0,
                cur_min,
            )
            mult = performance_brain.combine(momentum, reg_bonus, t_bonus)
            print(
                f"[PerfBrain] {symbol or signal_type} multiplier={mult:.2f} | "
                f"momentum={momentum:.1f} regime_bonus={reg_bonus:.1f} time_bonus={t_bonus:.1f}"
            )
            return mult
        except Exception as e:
            print(f"[PerfBrain] Multiplier query failed for {signal_type}: {e}")
            return 1.0

    def _fetch_weekly_brain_stats(self) -> dict:
        """Queries last 7 days of signal_outcomes for the Sunday Performance Brain digest."""
        if not self._db_engine:
            return {}
        try:
            week_ago = datetime.now(pytz.utc) - timedelta(days=7)
            with self._db_engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT signal_type,
                           COUNT(*) AS trades,
                           COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::float
                               / NULLIF(COUNT(*), 0) AS win_rate,
                           AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct      ELSE NULL END) AS avg_win,
                           AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct) ELSE NULL END) AS avg_loss
                    FROM signal_outcomes
                    WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                      AND entry_time >= :week_ago
                    GROUP BY signal_type
                    HAVING COUNT(*) >= 2
                """), {"week_ago": week_ago}).mappings().fetchall()

            best_ev, worst_ev = float('-inf'), float('inf')
            best_type = worst_type = None
            total_trades = 0

            for row in rows:
                wr       = float(row["win_rate"]  or 0)
                avg_win  = float(row["avg_win"]   or 0)
                avg_loss = float(row["avg_loss"]  or 0)
                ev       = round((wr * avg_win) - ((1.0 - wr) * avg_loss), 3)
                count    = int(row["trades"])
                total_trades += count
                if ev > best_ev:
                    best_ev, best_type = ev, row["signal_type"]
                if ev < worst_ev:
                    worst_ev, worst_type = ev, row["signal_type"]

            _DAY_NAMES = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday"}
            with self._db_engine.connect() as conn:
                day_row = conn.execute(sql_text("""
                    SELECT EXTRACT(DOW FROM entry_time AT TIME ZONE 'America/New_York')::int AS dow,
                           ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric
                               / NULLIF(COUNT(*), 0), 1) AS win_rate
                    FROM signal_outcomes
                    WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                      AND EXTRACT(DOW FROM entry_time AT TIME ZONE 'America/New_York') BETWEEN 1 AND 5
                    GROUP BY 1
                    HAVING COUNT(*) >= 3
                    ORDER BY win_rate DESC NULLS LAST
                    LIMIT 1
                """)).mappings().fetchone()

            with self._db_engine.connect() as conn:
                overall_row = conn.execute(sql_text("""
                    SELECT AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct      ELSE NULL END) AS avg_win,
                           AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct) ELSE NULL END) AS avg_loss
                    FROM signal_outcomes
                    WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                      AND entry_time >= :week_ago
                """), {"week_ago": week_ago}).mappings().fetchone()

            overall_avg_win  = float(overall_row["avg_win"]  or 0) if overall_row else 0.0
            overall_avg_loss = float(overall_row["avg_loss"] or 0) if overall_row else 0.0
            overall_ratio = (
                round(overall_avg_win / overall_avg_loss, 2)
                if overall_avg_loss > 0 else None
            )

            # Regime performance breakdown — closes the loop on backtested vs live
            # performance per market regime, to detect regime-specific decay.
            regime_breakdown = []
            try:
                with self._db_engine.connect() as conn:
                    regime_rows = conn.execute(sql_text("""
                        SELECT regime_class,
                               COUNT(*) AS trades,
                               ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric
                                   / NULLIF(COUNT(*), 0), 1) AS win_rate,
                               ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl
                        FROM signal_outcomes
                        WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                          AND regime_class IS NOT NULL
                          AND entry_time >= :week_ago
                        GROUP BY regime_class
                        ORDER BY trades DESC
                    """), {"week_ago": week_ago}).mappings().fetchall()
                regime_breakdown = [
                    {
                        "regime":   r["regime_class"],
                        "trades":   int(r["trades"]),
                        "win_rate": float(r["win_rate"] or 0),
                        "avg_pnl":  float(r["avg_pnl"] or 0),
                    }
                    for r in regime_rows
                ]
            except Exception as _re:
                print(f"[PerfBrain] Regime breakdown query failed: {_re}")

            # Strategy decay summary — tier counts + re-validation queue depth.
            decay_summary = {}
            try:
                from discovery.decay_monitor import summarize_decay_status
                decay_summary = summarize_decay_status(self._db_engine)
            except Exception as _de:
                print(f"[PerfBrain] Decay summary query failed: {_de}")

            return {
                "regime_breakdown":  regime_breakdown,
                "decay_summary":     decay_summary,
                "total_trades":      total_trades,
                "best_signal_type":  best_type,
                "best_ev":           round(best_ev, 3)  if best_type  else None,
                "worst_signal_type": worst_type,
                "worst_ev":          round(worst_ev, 3) if worst_type else None,
                "best_day":          _DAY_NAMES.get(int(day_row["dow"]), "—") if day_row else "—",
                "best_day_win_rate": float(day_row["win_rate"] or 0) if day_row else 0.0,
                "overall_avg_win":   round(overall_avg_win, 2),
                "overall_avg_loss":  round(overall_avg_loss, 2),
                "overall_ratio":     overall_ratio,
            }
        except Exception as e:
            print(f"[PerfBrain] Weekly stats query failed: {e}")
            return {}

    # ── Swing screener helpers ────────────────────────────────────────────────

    def _get_swing_symbols(self) -> list[str]:
        """Pull top 250 from active_tickers by volume, always including the 6 priority symbols."""
        from discovery.ticker_prioritizer import get_active_tickers
        _PRIORITY = list(Config.SWING_SYMBOLS)
        try:
            db_tickers = get_active_tickers(self._db_engine)[:250]
        except Exception as _e:
            import traceback as _tb
            print(f"[Swing] active_tickers fetch failed — using priority symbols only\n{_tb.format_exc()}")
            db_tickers = []
        # Append any priority symbols that didn't appear in the top-250 volume ranking
        ticker_set = set(db_tickers)
        extras = [s for s in _PRIORITY if s not in ticker_set]
        return db_tickers + extras

    async def _get_adv(self, symbol: str) -> float:
        """20-day average daily volume for symbol, cached per symbol for 24 hours."""
        now = datetime.now(pytz.utc)
        cached = self._adv_cache.get(symbol)
        if cached:
            adv_val, cached_at = cached
            if (now - cached_at).total_seconds() < 86400:
                return adv_val
        try:
            bars = await asyncio.to_thread(
                get_historical_bars, symbol, TimeFrame.Day, 20, self.stock_data_client
            )
            adv_val = float(bars["volume"].mean()) if bars is not None and not bars.empty else 0.0
        except Exception as _adv_e:
            print(f"[ADV] {symbol}: fetch failed (non-fatal): {_adv_e}")
            adv_val = 0.0
        self._adv_cache[symbol] = (adv_val, now)
        return adv_val

    async def _execute_short(
        self,
        symbol: str,
        signal: dict,
        strategy,
        risk_percent: float,
        stop_loss_percent: float,
        data,
    ) -> None:
        """Execute a short sale with the full protection stack."""
        import traceback as _tb
        import pandas_ta as _ta

        print(f"[Swing] SHORT debug — signal keys: {signal.keys()} values: {signal}")
        entry_price = float(signal.get("entry_price", 0))
        if entry_price <= 0:
            print(f"[Swing] {symbol}: SHORT skipped — no valid entry price in signal")
            return

        # Defense-in-depth: abort if any position or pending order already exists.
        # This catches duplicates that slip past the cycle-start _short_positions dict
        # (e.g., transient get_all_positions() API failure) and the Layer 1 position check.
        _already_in = await asyncio.to_thread(
            strategy.is_already_in_position, symbol, self.trading_client
        )
        if _already_in:
            print(
                f"[Swing] {symbol}: position or pending order already exists — aborting SHORT"
            )
            return

        # Fundamentals gate
        fund_proceed, fund_reason = await self._check_fundamentals(symbol)
        if not fund_proceed:
            print(f"[Swing] {symbol}: SHORT blocked by fundamentals — {fund_reason}")
            asyncio.create_task(notifications.notify_trade_skipped(
                symbol, strategy.name, f"SHORT fundamentals: {fund_reason}"
            ))
            return

        # Set 4-hour cooldown before entering debate — prevents repeated debate calls
        # for the same signal if the short is blocked by debate or subsequent gates.
        _now_utc = datetime.now(pytz.utc)
        _next_eligible = _now_utc + timedelta(hours=4)
        self._swing_signal_times[symbol] = _now_utc
        asyncio.create_task(
            asyncio.to_thread(self._persist_signal_cooldown, symbol, _now_utc)
        )
        print(
            f"[Swing] {symbol} cooldown set — "
            f"next eligible {_next_eligible.strftime('%Y-%m-%d %H:%M UTC')}"
        )

        # Bull/bear debate gate
        debate_proceed, debate_summary = await self._debate_trade(symbol, signal, strategy)
        if not debate_proceed:
            print(f"[Swing] {symbol}: SHORT blocked by debate — {debate_summary}")
            asyncio.create_task(notifications.notify_trade_skipped(
                symbol, strategy.name, f"SHORT debate SKIP — {debate_summary}"
            ))
            return
        print(f"[Swing] {symbol}: SHORT debate passed — proceeding to sentiment/ATR/sizing")

        # Sentiment gate (bearish boosts short conviction, bullish reduces it)
        sentiment_mult = 1.0
        try:
            _sent = await asyncio.to_thread(get_sentiment_score, self._db_engine, symbol)
            if _sent:
                _sdir = _sent.get("direction", "neutral")
                _sscr = int(_sent.get("score", 0))
                if _sdir == "bearish" and _sscr >= 7:
                    sentiment_mult = 1.2
                    print(f"[Swing] {symbol}: bearish sentiment (score={_sscr}) → short size +20% (mult=1.2)")
                elif _sdir == "bullish" and _sscr >= 7:
                    sentiment_mult = 0.5
                    print(f"[Swing] {symbol}: bullish sentiment (score={_sscr}) → short size −50% (mult=0.5)")
                else:
                    print(f"[Swing] {symbol}: sentiment neutral (dir={_sdir} score={_sscr}) → no size adjustment")
            else:
                print(f"[Swing] {symbol}: no recent sentiment data — size mult=1.0")
        except Exception as _se:
            print(f"[Swing] {symbol}: sentiment check failed (non-fatal): {_se}\n{_tb.format_exc()}")
        print(f"[Swing] {symbol}: sentiment_mult={sentiment_mult}")

        # ATR-based stop (above entry) and target (below entry)
        _atr: float | None = None
        try:
            _atr_s = _ta.atr(data["high"], data["low"], data["close"], length=14)
            if _atr_s is not None and not _atr_s.empty and not pd.isna(_atr_s.iloc[-1]):
                _atr = float(_atr_s.iloc[-1])
                print(f"[Swing] {symbol}: ATR(14)={_atr:.4f}")
            else:
                print(f"[Swing] {symbol}: ATR(14) unavailable — will use pct-based stop/target")
        except Exception as _ae:
            print(f"[Swing] {symbol}: ATR calculation failed (non-fatal): {_ae}\n{_tb.format_exc()}")

        if _atr and _atr > 0:
            short_stop   = round(entry_price + 2.0 * _atr, 2)
            short_target = round(entry_price - 5.0 * _atr, 2)
            print(
                f"[Swing] {symbol}: ATR stop/target — "
                f"entry={entry_price:.2f} stop={short_stop:.2f} (+2×ATR) "
                f"target={short_target:.2f} (−5×ATR)"
            )
        else:
            short_stop   = round(entry_price * (1 + stop_loss_percent / 100), 2)
            short_target = round(entry_price * (1 - Config.TAKE_PROFIT_PERCENT / 100), 2)
            print(
                f"[Swing] {symbol}: pct stop/target (ATR unavailable) — "
                f"entry={entry_price:.2f} stop={short_stop:.2f} (+{stop_loss_percent}%) "
                f"target={short_target:.2f} (−{Config.TAKE_PROFIT_PERCENT}%)"
            )

        # Minimum 1:2 R/R check
        short_risk   = short_stop - entry_price
        short_reward = entry_price - short_target
        rr = round(short_reward / max(short_risk, 1e-9), 2)
        print(
            f"[Swing] {symbol}: R/R check — risk={short_risk:.4f} reward={short_reward:.4f} "
            f"ratio={rr:.2f} (min={Config.SWING_MIN_RR_RATIO})"
        )
        if short_risk <= 0 or rr < Config.SWING_MIN_RR_RATIO:
            print(f"[Swing] {symbol}: SHORT skipped — R/R {rr:.2f} < {Config.SWING_MIN_RR_RATIO}")
            return

        # Slippage-adjusted R/R: assume 0.2% worse fill on both entry and exit
        _SLIP = 0.002
        _eff_entry  = entry_price * (1 - _SLIP)
        _eff_cover  = short_target * (1 + _SLIP)
        _eff_risk   = short_stop - _eff_entry
        _eff_reward = _eff_entry - _eff_cover
        rr_slippage = round(_eff_reward / max(_eff_risk, 1e-9), 2)
        print(f"[Sizing] {symbol} R/R after slippage: {rr_slippage} (pre-slippage: {rr})")
        if _eff_risk <= 0 or rr_slippage < Config.SWING_MIN_RR_RATIO:
            print(
                f"[Swing] {symbol}: SHORT skipped — "
                f"R/R after slippage {rr_slippage:.2f} < {Config.SWING_MIN_RR_RATIO}"
            )
            return
        print(f"[Swing] {symbol}: R/R passed ({rr_slippage:.2f} w/ slippage) — proceeding to liquidity + sizing")

        # Minimum liquidity filter: skip if avg daily dollar volume < $10M
        _adv = await self._get_adv(symbol)
        _adv_dollar_vol = _adv * entry_price if _adv > 0 else 0.0
        if 0 < _adv_dollar_vol < Config.MIN_DOLLAR_VOLUME:
            print(
                f"[Liquidity] {symbol} skipped — "
                f"avg daily dollar volume ${_adv_dollar_vol / 1e6:.1f}M below $10M minimum"
            )
            return
        _adv_cap = max(1, int(_adv * 0.01)) if _adv > 0 else None

        # Position sizing
        try:
            account      = await asyncio.to_thread(self.trading_client.get_account)
            equity       = float(account.equity)
            # Week-over-week short-interest confirmation bonus (Task 4): rising SI
            # confirms a short → larger size. Set on the signal in _process_symbol.
            short_int_mult = float(signal.get('short_interest_size_mult', 1.0))
            risk_dollars = equity * (risk_percent * self.risk_multiplier * sentiment_mult * short_int_mult / 100.0)
            shares_raw   = max(1, int(risk_dollars / short_risk))
            max_dollars  = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
            shares_bp    = max(1, int(max_dollars / entry_price))
            shares       = min(shares_raw, shares_bp)
            print(
                f"[Swing] {symbol}: sizing — equity={equity:.0f} risk%={risk_percent:.3f} "
                f"risk_mult={self.risk_multiplier:.2f} sent_mult={sentiment_mult:.2f} "
                f"short_int_mult={short_int_mult:.2f} "
                f"risk_$={risk_dollars:.2f} shares_risk={shares_raw} "
                f"bp_cap={shares_bp} final_shares={shares}"
            )
            if shares < shares_raw:
                print(f"[Swing] {symbol}: shares capped by buying power ({shares_raw}→{shares})")
            if _adv_cap is not None and shares > _adv_cap:
                print(
                    f"[Sizing] {symbol} ADV cap applied — requested {shares} shares, "
                    f"capped to {_adv_cap} (1% of ADV={_adv:.0f})"
                )
                shares = _adv_cap
        except Exception as _acct_e:
            print(f"[Swing] {symbol}: SHORT skipped — account fetch failed: {_acct_e}\n{_tb.format_exc()}")
            return

        # Step 1: plain market SELL — Alpaca does not support BRACKET on market orders
        print(
            f"[Swing] {symbol}: submitting SHORT market order — "
            f"qty={shares} entry={entry_price:.2f}"
        )
        try:
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopLossRequest, TakeProfitRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
            _entry_order = await asyncio.to_thread(
                self.trading_client.submit_order,
                MarketOrderRequest(
                    symbol=symbol,
                    qty=shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                ),
            )
            print(f"[Swing] {symbol}: SHORT market order submitted — id={str(_entry_order.id)[:12]} status={_entry_order.status}")

            # Step 2: poll for fill (max 30 s) — isolated so a transient API error
            # here does not abort the OCO placement or the Slack notification.
            _fill_price = None
            try:
                for _attempt in range(30):
                    await asyncio.sleep(1)
                    _checked = await asyncio.to_thread(self.trading_client.get_order_by_id, _entry_order.id)
                    if getattr(_checked, 'status', None) and _checked.status.value == 'filled':
                        _fill_price = float(_checked.filled_avg_price or entry_price)
                        break
            except Exception as _poll_e:
                print(f"[Swing] {symbol}: fill poll failed (non-fatal) — {_poll_e}")
            if _fill_price is None:
                print(f"[Swing] {symbol}: fill not confirmed in 30s — using calculated entry price for OCO")
                _fill_price = entry_price

            # Recompute stop/target from actual fill price
            if _atr and _atr > 0:
                _actual_stop   = round(_fill_price + 2.0 * _atr, 2)
                _actual_target = round(_fill_price - 5.0 * _atr, 2)
            else:
                _actual_stop   = short_stop
                _actual_target = short_target

            print(
                f"[Swing] SHORT entered for {symbol} at {_fill_price:.2f} | "
                f"stop={_actual_stop:.2f} target={_actual_target:.2f} | size={shares}"
            )

            # Step 3: OCO buy-to-cover — BUY limit at target + BUY stop at stop
            try:
                _oco = await asyncio.to_thread(
                    self.trading_client.submit_order,
                    LimitOrderRequest(
                        symbol=symbol,
                        qty=shares,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.GTC,
                        order_class=OrderClass.OCO,
                        limit_price=_actual_target,
                        take_profit=TakeProfitRequest(limit_price=_actual_target),
                        stop_loss=StopLossRequest(stop_price=_actual_stop),
                    ),
                )
                print(
                    f"[Swing] {symbol}: OCO protection placed — "
                    f"id={str(_oco.id)[:12]} target={_actual_target} stop={_actual_stop}"
                )
            except Exception as _oco_e:
                print(f"[Swing] {symbol}: OCO placement FAILED — {_oco_e}\n{_tb.format_exc()}")

            print(f"[Slack] Attempting trade notification for {symbol}")
            asyncio.create_task(notifications.notify_trade_executed(
                symbol, "SHORT", _fill_price, _actual_stop, _actual_target, shares,
                "debate: passed | fundamentals: passed",
            ))
            _health_state["signals_fired_total"] += 1
        except Exception as _ord_e:
            print(f"[Swing] {symbol}: SHORT order failed — {_ord_e}\n{_tb.format_exc()}")

    async def swing_loop(self):
        print("📈 Starting Stock Swing Screener — 5-min cadence, 250-symbol universe, market hours only...")
        _no_edge = {"JPM", "PG"}
        _est     = pytz.timezone('America/New_York')
        _cycle   = 0

        while True:
            now     = datetime.now(_est)
            tod_min = now.hour * 60 + now.minute
            # Market hours: Mon–Fri 9:30 AM – 4:00 PM EDT (570–960 minutes)
            is_market = now.weekday() < 5 and 570 <= tod_min < 960

            if not is_market:
                # Sleep until 9:30 AM on the next trading weekday
                next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                if tod_min >= 960 or now.weekday() >= 5:
                    next_open += timedelta(days=1)
                while next_open.weekday() >= 5:
                    next_open += timedelta(days=1)
                sleep_s = max((next_open - now).total_seconds(), 1)
                print(
                    f"[Swing] Market closed — next open "
                    f"{next_open.strftime('%Y-%m-%d %H:%M %Z')} ({sleep_s/3600:.1f}h)"
                )
                await asyncio.sleep(sleep_s)
                continue

            _cycle += 1

            # --- Symbol universe: top 250 by volume + 6 priority symbols ---
            symbols = await asyncio.to_thread(self._get_swing_symbols)
            top3    = ", ".join(symbols[:3]) if symbols else "—"
            print(f"[Swing] Cycle {_cycle} | {now.strftime('%H:%M')} EDT | screening {len(symbols)} symbols")
            print(f"[Swing] Screening {len(symbols)} symbols — top 3 by volume: {top3}")

            await self._check_account_status()
            spy_bars = None
            try:
                spy_bars = get_historical_bars("SPY", TimeFrame.Day, 365, self.stock_data_client)
            except Exception as _spy_e:
                print(f"[Swing] SPY bars fetch failed (non-fatal): {_spy_e}")

            swing_regime = await self._get_market_regime()

            # 4-regime classification for live regime gating (cached 4h).
            current_regime_class = await self._get_current_regime_class(spy_bars)

            # Correlation-aware portfolio gate (Task 4): load the current optimal
            # portfolio's symbol set once per cycle. Fail-open when no portfolio exists.
            portfolio_symbols, portfolio_has_data = set(), False
            if Config.PORTFOLIO_GATING_ENABLED:
                portfolio_symbols, portfolio_has_data = await asyncio.to_thread(
                    self._load_portfolio_symbols
                )
                if portfolio_has_data:
                    print(f"[Portfolio] Gating swing screener to {len(portfolio_symbols)} portfolio symbols")
                else:
                    print("[Portfolio] No optimal portfolio yet — screening all symbols (fail-open)")

            adx_regime = self._get_market_regime_adx(spy_bars)
            preferred_strategy_type = {
                "trending": "ema_trend",
                "choppy":   "bb_mean_reversion",
            }.get(adx_regime)
            if preferred_strategy_type:
                print(f"[ADX] Market is {adx_regime} — preferring {preferred_strategy_type} strategies")

            # 5-window time-of-day multiplier
            if 570 <= tod_min < 630:
                tod_mult = 1.2   # 9:30–10:30 — open momentum
            elif 630 <= tod_min < 720:
                tod_mult = 1.0   # 10:30–12:00 — prime window
            elif 720 <= tod_min < 840:
                tod_mult = 0.7   # 12:00–14:00 — midday chop
            elif 840 <= tod_min < 960:
                tod_mult = 1.0   # 14:00–16:00 — afternoon trend
            else:
                tod_mult = 0.5
            print(f"[Swing] Time-of-day multiplier: {tod_mult}x ({now.strftime('%H:%M')} EST)")

            # Fetch all open positions once per cycle for short-thesis-exit check
            _short_positions: dict = {}
            try:
                _all_open = await asyncio.to_thread(self.trading_client.get_all_positions)
                _short_positions = {
                    p.symbol: p for p in _all_open
                    if str(getattr(p, 'side', '')).lower() == 'short'
                }
                if _short_positions:
                    print(
                        f"[Swing] {len(_short_positions)} open short position(s) — "
                        f"will check thesis reversal: {list(_short_positions.keys())}"
                    )
            except Exception as _pos_fetch_err:
                print(f"[Swing] Could not fetch open positions for short-exit check (non-fatal): {_pos_fetch_err}")

            for symbol in symbols:
                # Short thesis reversal check — runs before cooldown so existing shorts can exit
                # even if the 4-hour entry cooldown is still active.
                if symbol in _short_positions:
                    _short_pos = _short_positions[symbol]
                    try:
                        import pandas_ta as _pdt
                        _exit_data = get_historical_bars(symbol, TimeFrame.Day, 365, self.stock_data_client)
                        if _exit_data is not None and len(_exit_data) >= 3:
                            _rsi_s    = _pdt.rsi(_exit_data['close'], length=14)
                            _macd_e   = _pdt.macd(_exit_data['close'], fast=12, slow=26, signal=9)
                            if (_rsi_s is not None and _macd_e is not None and
                                    not _rsi_s.empty and len(_macd_e) >= 2):
                                _curr_rsi  = float(_rsi_s.iloc[-1])
                                _curr_macd = float(_macd_e.iloc[:, 0].iloc[-1])
                                _curr_msig = float(_macd_e.iloc[:, 2].iloc[-1])
                                _prev_macd = float(_macd_e.iloc[:, 0].iloc[-2])
                                _prev_msig = float(_macd_e.iloc[:, 2].iloc[-2])
                                _rsi_recovered  = _curr_rsi < 55
                                _macd_recovered = _curr_macd > _curr_msig and _prev_macd <= _prev_msig
                                print(
                                    f"[Swing] {symbol} short exit check — "
                                    f"RSI={_curr_rsi:.2f}(recovered={_rsi_recovered}) "
                                    f"MACD={_curr_macd:.4f}(recovered={_macd_recovered})"
                                )
                                if _rsi_recovered and _macd_recovered:
                                    print(
                                        f"[Swing] {symbol} SHORT thesis reversed — closing position early | "
                                        f"RSI={_curr_rsi:.2f} MACD={_curr_macd:.4f}"
                                    )
                                    # Cancel outstanding OCO orders before covering
                                    try:
                                        _open_ords = await asyncio.to_thread(
                                            self.trading_client.get_orders,
                                            GetOrdersRequest(
                                                status=QueryOrderStatus.OPEN,
                                                symbols=[symbol],
                                            ),
                                        )
                                        for _o in _open_ords:
                                            await asyncio.to_thread(
                                                self.trading_client.cancel_order_by_id, _o.id
                                            )
                                        print(f"[Swing] {symbol}: cancelled {len(_open_ords)} OCO order(s) before covering")
                                    except Exception as _ce:
                                        print(f"[Swing] {symbol}: OCO cancel error (non-fatal) — {_ce}")
                                    # Market buy-to-cover
                                    try:
                                        from alpaca.trading.requests import MarketOrderRequest as _MOR
                                        from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                                        _cover = await asyncio.to_thread(
                                            self.trading_client.submit_order,
                                            _MOR(
                                                symbol=symbol,
                                                qty=abs(float(_short_pos.qty)),
                                                side=_OS.BUY,
                                                time_in_force=_TIF.DAY,
                                            ),
                                        )
                                        print(f"[Swing] {symbol}: buy-to-cover submitted — id={str(_cover.id)[:12]}")
                                        asyncio.create_task(notifications.notify_alert(
                                            f"[ShortExit] {symbol} thesis reversed — covered at market | "
                                            f"RSI={_curr_rsi:.1f} MACD crossed above signal",
                                            level="INFO",
                                        ))
                                    except Exception as _cov_e:
                                        print(f"[Swing] {symbol}: buy-to-cover FAILED — {_cov_e}")
                    except Exception as _exit_err:
                        print(f"[Swing] {symbol}: short exit check failed (non-fatal) — {_exit_err}")
                    continue  # skip new signal evaluation for any symbol with an open short

                # 4-hour per-symbol cooldown — skip if a signal already fired recently
                _last_sig = self._swing_signal_times.get(symbol)
                if _last_sig and (datetime.now(pytz.utc) - _last_sig) < timedelta(hours=4):
                    continue

                print(f"DEBUG: swing evaluation started for {symbol}")
                if symbol in _no_edge:
                    print(
                        f"[Swing] {symbol}: no statistically validated edge "
                        "(p>0.05 across all 243 discovery combos) — monitoring only"
                    )

                # Use configured strategy for priority symbols; default for the rest
                strategy = self.swing_symbol_strategies.get(symbol)
                if strategy is None:
                    strategy = SwingStrategy(
                        f"{symbol} Swing",
                        db_engine=self._db_engine,
                        base_capital=self.start_of_day_equity or 0.0,
                    )

                discovery   = apply_to_swing_strategy(
                    symbol, spy_bars, preferred_strategy_type=preferred_strategy_type
                )
                risk_to_use = Config.SWING_EQUITY_RISK_PERCENT

                if discovery is not None:
                    s_type, s_params = discovery
                    upgraded = None
                    if s_type == "ema_trend":
                        rsi_gate = s_params.get("rsi_gate", [40, 60])
                        upgraded = SwingStrategy(
                            f"{symbol} Discovery[{s_type}]",
                            ema_short=s_params.get("ema_short", strategy.ema_short),
                            ema_long=s_params.get("ema_long",  strategy.ema_long),
                            rsi_period=s_params.get("rsi_period", strategy.rsi_period),
                            rsi_entry_low=rsi_gate[0],
                            rsi_entry_high=rsi_gate[1],
                        )
                    elif s_type == "bb_mean_reversion":
                        upgraded = BollingerMeanReversionStrategy(
                            f"{symbol} Discovery[{s_type}]",
                            bb_period=s_params.get("bb_period", 20),
                            bb_std=float(s_params.get("bb_std", 2.0)),
                            rsi_period=s_params.get("rsi_period", 14),
                            rsi_entry=s_params.get("rsi_entry", 30),
                            rsi_exit=s_params.get("rsi_exit", 65),
                        )
                    if upgraded is not None:
                        upgraded.discovery_strategy_type = s_type
                        strategy = upgraded
                        backtest_win_rate = None
                        if self._db_engine:
                            try:
                                with self._db_engine.connect() as _conn:
                                    _bwr = _conn.execute(sql_text("""
                                        SELECT win_rate FROM discovery_results
                                        WHERE symbol = :sym
                                          AND strategy_type = :st
                                          AND status = 'approved'
                                        ORDER BY test_sharpe DESC NULLS LAST
                                        LIMIT 1
                                    """), {"sym": symbol, "st": s_type}).scalar()
                                backtest_win_rate = float(_bwr) if _bwr is not None else None
                            except Exception as _bwr_e:
                                print(f"[Swing] {symbol}: discovery WR query failed — {_bwr_e}")
                        disc_mult, disc_reason = await self._get_discovery_risk_multiplier(
                            symbol, s_type, backtest_win_rate
                        )
                        risk_to_use = Config.SWING_EQUITY_RISK_PERCENT * disc_mult
                        print(f"[Swing] {symbol} Discovery[{s_type}]: {disc_reason}")
                        if disc_mult == 0.25:
                            strategy.discovery_size_note = (
                                "New discovery strategy — using 25% position size "
                                "for first 50 trades as validation period."
                            )

                if Config.EARNINGS_FILTER_ENABLED:
                    has_earnings_soon, report_date_soon = await self._check_upcoming_earnings(
                        symbol, days_ahead=1
                    )
                    if has_earnings_soon:
                        skip_msg = f"Earnings on {report_date_soon} — skipping new entries (earnings filter)"
                        print(f"[Earnings] {symbol}: {skip_msg}")
                        asyncio.create_task(notifications.notify_trade_skipped(
                            symbol, strategy.name, skip_msg
                        ))
                        continue

                if swing_regime == 'bear':
                    risk_to_use *= Config.BEAR_MARKET_SIZE_REDUCTION
                    strategy.bear_market_note = "🐻 Bear market mode — position size reduced 50%"
                    print(f"[Swing] {symbol}: bear market mode — risk_to_use={risk_to_use:.3f}%")

                # Portfolio gate (Task 4) — only evaluate symbols in the current
                # optimal portfolio. Fail-open when no portfolio has been built yet.
                if Config.PORTFOLIO_GATING_ENABLED and portfolio_has_data:
                    if symbol not in portfolio_symbols:
                        print(f"[Portfolio] {symbol} not in optimal portfolio — SKIP")
                        continue
                    print(f"[Portfolio] {symbol} in optimal portfolio — PROCEED")

                # Live regime gate — only trade a symbol when its validated strategy
                # covers the current regime. Fail-open when no regime data exists.
                if Config.REGIME_GATING_ENABLED:
                    gate_ok, valid_regimes, has_data = await asyncio.to_thread(
                        self._regime_gate_ok, symbol, current_regime_class
                    )
                    if has_data:
                        print(
                            f"[Regime] {symbol} strategy validated for {valid_regimes} — "
                            f"current regime {current_regime_class} — "
                            f"{'PROCEED' if gate_ok else 'SKIP'}"
                        )
                        if not gate_ok:
                            asyncio.create_task(notifications.notify_trade_skipped(
                                symbol, strategy.name,
                                f"Regime gate: validated for {valid_regimes}, "
                                f"current {current_regime_class}"
                            ))
                            continue
                    else:
                        print(
                            f"[Regime] {symbol} no regime validation data — "
                            f"current regime {current_regime_class} — PROCEED (fail-open)"
                        )

                risk_to_use *= tod_mult
                print(f"Evaluating {symbol} for swing signals [{strategy.name}]")

                try:
                    await self._process_symbol(
                        symbol,
                        [strategy],
                        is_crypto=False,
                        risk_percent=risk_to_use,
                        stop_loss_percent=Config.STOP_LOSS_PERCENT,
                        pre_execute_hook=self._swing_pre_trade_hook,
                    )

                except Exception as _sym_err:
                    import traceback as _tb
                    print(
                        f"[Swing] {symbol}: evaluation error — {_sym_err}\n{_tb.format_exc()}"
                    )

            print(f"📈 Swing cycle {_cycle} complete — {len(symbols)} symbols screened.")
            await asyncio.sleep(300)  # 5-minute cadence

    async def health_report_loop(self):
        import datetime as _dt_module
        print(f"🏥 Starting Daily Health Report Loop (9:00 AM EST)...")
        print(f"[HealthLoop] System clock: {_dt_module.datetime.now()} | "
              f"UTC: {_dt_module.datetime.now(_dt_module.timezone.utc)} | "
              f"EST: {datetime.now(pytz.timezone('America/New_York'))}")
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            sleep_seconds = (target - now).total_seconds()
            print(f"[HealthLoop] Sleeping {sleep_seconds/3600:.2f}h | "
                  f"now={now.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                  f"next_fire={target.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            await asyncio.sleep(sleep_seconds)

            wake_est = datetime.now(pytz.timezone('America/New_York'))
            print(f"[HealthLoop] Woke up at {wake_est.strftime('%Y-%m-%d %H:%M:%S %Z')} — fetching account")
            await self._check_account_status()
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                uptime_seconds = (datetime.now(pytz.utc) - _bot_start_time).total_seconds()
                uptime_str = str(timedelta(seconds=int(uptime_seconds)))
                equity = float(account.equity)
                buying_power = float(account.buying_power)
                asyncio.create_task(notifications.notify_daily_health(uptime_str, equity, buying_power, self.daily_pnl))
                _health_state["last_health_report_utc"] = datetime.now(pytz.utc).isoformat()
            else:
                print("[HealthLoop] account fetch returned None — health report skipped this cycle")

    async def performance_report_loop(self):
        print("📊 Starting Weekly Performance Report Loop (Sunday 6:00 PM EST)...")
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            days_ahead = 6 - now.weekday() # Sunday is 6
            target = now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
            if now >= target:
                target += timedelta(days=7)
                
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
            
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                equity = float(account.equity)
                try:
                    positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                    active_positions_count = len(positions)
                except Exception:
                    active_positions_count = 0

                asyncio.create_task(notifications.notify_weekly_performance(equity, active_positions_count, self.daily_pnl))
                brain_stats = await asyncio.to_thread(self._fetch_weekly_brain_stats)
                asyncio.create_task(notifications.notify_weekly_performance_brain(brain_stats))

    async def news_loop(self):
        """Continuously polls Benzinga news via Alpaca and routes signals to Slack / trade execution."""
        print("📰 Starting Benzinga News Sentiment Loop...")
        strategy = NewsStrategy(db_engine=self._db_engine)
        while True:
            signals: list[dict] = []
            try:
                if not self.trading_halted_for_day and not _bot_paused:
                    signals = await strategy.scan_once()
                    _health_state["claude_api_calls_today"] = strategy._claude_calls_today
                    for sig in signals:
                        ticker  = sig["ticker"]
                        strength = sig["strength"]
                        action  = sig["action"]

                        # Only alert for actionable directions — HOLD means no trade
                        if action.upper() not in ("BUY", "SELL"):
                            continue
                        asyncio.create_task(notifications.notify_news_signal(
                            ticker, sig["headline"], sig["sentiment"], strength, action
                        ))
                        asyncio.create_task(self._record_daily_signal(ticker, 'News'))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: macro regime conviction (VIX > 30 → 0.7× multiplier)
                        macro_mult = get_conviction_multiplier()
                        if macro_mult < 1.0:
                            effective = round(sig["strength"] * macro_mult, 2)
                            if effective < Config.NEWS_SIGNAL_AUTO_TRADE_THRESHOLD:
                                asyncio.create_task(notifications.notify_trade_skipped(
                                    ticker, strategy.name,
                                    f"Macro veto: VIX elevated — effective strength {effective} < {Config.NEWS_SIGNAL_AUTO_TRADE_THRESHOLD}"
                                ))
                                continue

                        # Guard: signal stacking — register & check for cross-source boost
                        _news_stacked, stack_mult = await self._push_signal_stack(
                            ticker, "news", strength
                        )

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (news)"))
                                continue

                        # Guard: buy requires no existing position; sell requires one
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            has_position = True
                        except Exception:
                            has_position = False
                        if action == "sell" and not has_position:
                            print(f"[DEBUG] News sell skipped for {ticker} — no open position.")
                            continue
                        elif action == "buy" and has_position:
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (news)"))
                            continue

                        # Execute using swing risk parameters (stack_mult = 1.3 when stacked)
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier * stack_mult
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = await asyncio.to_thread(
                                self.stock_data_client.get_stock_latest_trade,
                                StockLatestTradeRequest(symbol_or_symbols=ticker)
                            )
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY)
                            )

                            asyncio.create_task(notifications.notify_news_trade(
                                ticker, sig["headline"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-news-{action}"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[NewsLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[NewsLoop] Unexpected error: {e}")
            finally:
                _health_state["last_news_scan_utc"] = datetime.now(pytz.utc).isoformat()
                sleep_seconds = _get_scan_sleep_seconds()
                print(f"📰 News scan complete — {strategy._last_articles_scanned} headlines analyzed, "
                      f"{len(signals)} signals above threshold, next scan in {sleep_seconds}s")
                await asyncio.sleep(sleep_seconds)

    async def truth_social_loop(self):
        """Polls Trump's Truth Social feed. Disabled until Quiver Quantitative integration is wired up."""
        if not Config.TRUTH_SOCIAL_ENABLED:
            print("🇺🇸 Truth Social loop disabled (TRUTH_SOCIAL_ENABLED=False) — exiting loop.")
            return
        print("🇺🇸 Starting Truth Social Sentiment Loop (60s polling)...")
        strategy = TruthSocialStrategy()
        while True:
            try:
                if not self.trading_halted_for_day and not _bot_paused:
                    signals = await strategy.scan_once(trading_client=self.trading_client)
                    for sig in signals:
                        ticker  = sig["ticker"]
                        strength = sig["strength"]
                        action  = sig["action"]

                        # Always alert Slack about the signal
                        asyncio.create_task(notifications.notify_truth_social_signal(
                            sig["post_text"], [ticker], sig["sentiment"], strength, action
                        ))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (TS)"))
                                continue

                        # Guard: buy requires no existing position; sell requires one
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            has_position = True
                        except Exception:
                            has_position = False
                        if action == "sell" and not has_position:
                            print(f"[DEBUG] TruthSocial sell skipped for {ticker} — no open position.")
                            continue
                        elif action == "buy" and has_position:
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (TS)"))
                            continue

                        # Execute using Truth Social risk overrides (50% size, 2% SL, 8% TP)
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = (
                                Config.SWING_EQUITY_RISK_PERCENT
                                * self.risk_multiplier
                                * Config.TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER
                            )
                            risk_dollars = equity * (scaled_risk / 100.0)

                            entry_price = sig.get("current_price", 0.0)
                            if entry_price <= 0:
                                latest = await asyncio.to_thread(
                                    self.stock_data_client.get_stock_latest_trade,
                                    StockLatestTradeRequest(symbol_or_symbols=ticker)
                                )
                                entry_price = float(latest[ticker].price)

                            stop_distance = entry_price * (Config.TRUTH_SOCIAL_STOP_LOSS / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY)
                            )

                            asyncio.create_task(notifications.notify_truth_social_trade(
                                ticker, sig["post_text"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-ts-{action}"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[TruthSocialLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[TruthSocialLoop] Unexpected error: {e}")
            finally:
                await asyncio.sleep(60)

    async def sec_edgar_loop(self):
        """Polls SEC EDGAR Form 4 insider trade filings every 30 minutes."""
        print("📋 Starting SEC EDGAR Insider Trade Loop (30-min polling)...")
        strategy = SECEdgarStrategy()
        while True:
            signals: list[dict] = []
            try:
                if not self.trading_halted_for_day and not _bot_paused:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker   = sig["ticker"]
                        strength = sig["strength"]
                        action   = sig["action"]

                        # Always send to #trading-decisions with 📋 emoji
                        asyncio.create_task(notifications.notify_edgar_signal(
                            ticker, sig["headline"], sig["sentiment"], strength, action
                        ))
                        asyncio.create_task(self._record_daily_signal(ticker, 'EDGAR'))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: macro regime conviction (VIX > 30 → 0.7× multiplier)
                        macro_mult = get_conviction_multiplier()
                        if macro_mult < 1.0:
                            effective = round(sig["strength"] * macro_mult, 2)
                            if effective < Config.SEC_EDGAR_AUTO_TRADE_THRESHOLD:
                                asyncio.create_task(notifications.notify_trade_skipped(
                                    ticker, strategy.name,
                                    f"Macro veto: VIX elevated — effective strength {effective} < {Config.SEC_EDGAR_AUTO_TRADE_THRESHOLD}"
                                ))
                                continue

                        # Guard: signal stacking — register & check for cross-source boost
                        _edgar_stacked, stack_mult = await self._push_signal_stack(
                            ticker, "edgar", strength
                        )

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (EDGAR)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (EDGAR)"))
                            continue
                        except Exception:
                            pass

                        # Execute using swing risk parameters (buys only; stack_mult = 1.3 when stacked)
                        if action != "buy":
                            continue
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier * stack_mult
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = await asyncio.to_thread(
                                self.stock_data_client.get_stock_latest_trade,
                                StockLatestTradeRequest(symbol_or_symbols=ticker)
                            )
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                            )
                            asyncio.create_task(notifications.notify_news_trade(
                                ticker, sig["headline"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-edgar-buy"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[EDGARLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[EDGARLoop] Unexpected error: {e}")
            finally:
                _health_state["last_edgar_scan_utc"] = datetime.now(pytz.utc).isoformat()
                print(f"📋 EDGAR scan complete — {len(signals)} insider signals above threshold, next scan in 30 min")
                await asyncio.sleep(1800)

    async def _validate_swing_symbols(self):
        """Fetch latest trade for each SWING_SYMBOLS entry to catch config typos at startup."""
        print("Validating swing symbols...")
        for symbol in Config.SWING_SYMBOLS:
            try:
                self.stock_data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
                print(f"  {symbol} OK")
            except Exception as e:
                msg = f"WARNING: Symbol {symbol} failed validation — check config ({e})"
                print(msg)
                asyncio.create_task(notifications.notify_alert(msg))

    async def fred_loop(self):
        """Fetches FRED macro indicators daily at 7 PM EST. Weekly summary fires on Sundays."""
        if not Config.FRED_ENABLED:
            print("[FRED] Disabled (FRED_ENABLED=False) — exiting.")
            return
        print("📊 Starting FRED Macro Indicator Loop (daily 7 PM EST, weekly summary Sundays)...")
        strategy = FREDStrategy()
        est = pytz.timezone('America/New_York')

        # Fetch immediately on startup so conviction multiplier has real data from minute one
        print("[FRED] Running initial macro fetch on startup...")
        events = await strategy.scan_once()
        if "vix_extreme_fear" in events:
            vix_val = MACRO_SNAPSHOT.get("vix", 0) or 0
            asyncio.create_task(notifications.notify_alert(
                f"📊 EXTREME FEAR: VIX has spiked to {vix_val:.1f} (>40). "
                f"Auto-trade conviction reduced to 0.7× in news and EDGAR loops.",
                level="CRITICAL"
            ))

        while True:
            # Sleep until next 7 PM EST
            now = datetime.now(est)
            target = now.replace(hour=19, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            events = await strategy.scan_once()

            if "vix_extreme_fear" in events:
                vix_val = MACRO_SNAPSHOT.get("vix", 0) or 0
                asyncio.create_task(notifications.notify_alert(
                    f"📊 EXTREME FEAR: VIX has spiked to {vix_val:.1f} (>40). "
                    f"Auto-trade conviction reduced to 0.7× in news and EDGAR loops.",
                    level="CRITICAL"
                ))

            # Sunday 7 PM EST — send weekly macro summary, correlation heatmap, and advance prev-week baseline
            if datetime.now(est).weekday() == 6:
                asyncio.create_task(notifications.notify_macro_summary(dict(MACRO_SNAPSHOT)))
                asyncio.create_task(self._generate_correlation_heatmap())
                MACRO_SNAPSHOT["prev_week_fed_funds"]    = MACRO_SNAPSHOT.get("fed_funds_rate")
                MACRO_SNAPSHOT["prev_week_vix"]          = MACRO_SNAPSHOT.get("vix")
                MACRO_SNAPSHOT["prev_week_treasury"]     = MACRO_SNAPSHOT.get("treasury_10y")
                MACRO_SNAPSHOT["prev_week_unemployment"] = MACRO_SNAPSHOT.get("unemployment")
                MACRO_SNAPSHOT["prev_week_cpi_yoy"]      = MACRO_SNAPSHOT.get("cpi_yoy")

    async def congressional_trading_loop(self):
        """Polls Quiver Quantitative for congressional trades every 60 minutes."""
        if not Config.CONGRESSIONAL_ENABLED:
            print("[Congress] Disabled — free data sources are unavailable. Enable when Quiver Quantitative API key ($30/mo) is added to Railway as QUIVER_API_KEY.")
            return
        print("🏛️ Starting Congressional Trading Loop (60-min polling)...")
        strategy = CongressionalTradingStrategy()
        while True:
            signals: list[dict] = []
            try:
                if not self.trading_halted_for_day and not _bot_paused:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker   = sig["ticker"]
                        strength = sig["strength"]
                        action   = sig["action"]

                        asyncio.create_task(notifications.notify_congressional_signal(
                            ticker, sig["headline"], sig["representative"],
                            sig["party"], sig["chamber"], sig["amount_range"],
                            sig["transaction"], strength, action,
                            informational=sig["informational"],
                        ))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (Congress)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (Congress)"))
                            continue
                        except Exception:
                            pass

                        # Execute using swing risk parameters (buys only; sells are informational)
                        if action != "buy":
                            continue
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = await asyncio.to_thread(
                                self.stock_data_client.get_stock_latest_trade,
                                StockLatestTradeRequest(symbol_or_symbols=ticker)
                            )
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                            )
                            asyncio.create_task(notifications.notify_news_trade(
                                ticker, sig["headline"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-congress-buy"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[CongressLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[CongressLoop] Unexpected error: {e}")

            if strategy._disabled:
                print("[CongressLoop] Disabled after auth failure — exiting loop permanently.")
                return
            buy_count  = sum(1 for s in signals if not s.get("informational"))
            sell_count = sum(1 for s in signals if s.get("informational"))
            print(f"🏛️ Congressional scan complete — {buy_count} buy signals, {sell_count} informational sell signals, next scan in 60 min")
            await asyncio.sleep(3600)

    async def market_open_notification_loop(self):
        """Sends a morning briefing to #trading-alerts at 9:30 AM EST, weekdays only."""
        print("🔔 Starting Market Open Notification Loop (9:30 AM EST, Mon-Fri)...")
        est = pytz.timezone('America/New_York')
        while True:
            now = datetime.now(est)
            target = now.replace(hour=9, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            # Advance past weekend days
            while target.weekday() >= 5:
                target += timedelta(days=1)

            await asyncio.sleep((target - now).total_seconds())

            # Double-check we landed on a weekday (clock skew guard)
            if datetime.now(est).weekday() >= 5:
                continue

            try:
                account = await asyncio.to_thread(self.trading_client.get_account)
                equity = float(account.equity) if account else 0.0
            except Exception:
                equity = 0.0

            regime = await self._get_market_regime()
            watchlist = ", ".join(Config.SWING_SYMBOLS)
            asyncio.create_task(notifications.notify_market_open(equity, watchlist, regime))

    async def discovery_loop(self):
        """
        Loop 13 — fires every Friday at 4:30 PM EST (30 min after market close).
        Runs discovery_engine_v2 as a subprocess so CPU-intensive work never blocks trading.
        Sends hourly progress pings and a completion report via Slack.
        """
        est = pytz.timezone('America/New_York')
        print("[Discovery] Discovery loop started — fires every Friday at 4:30 PM EST")
        while True:
            now = datetime.now(est)
            days_until_friday = (4 - now.weekday()) % 7
            target = now.replace(hour=16, minute=30, second=0, microsecond=0)
            if days_until_friday > 0:
                target += timedelta(days=days_until_friday)
            elif now >= target:
                target += timedelta(days=7)

            await asyncio.sleep((target - now).total_seconds())

            print("[Discovery] Starting Discovery Engine v2 subprocess")

            # Friday macro brief before subprocess launch
            try:
                brief_resp = await call_llm_with_model(
                    MODEL_FLASH,
                    "Summarize the key macroeconomic themes and market-moving events from this week. "
                    "Focus on: Fed policy signals, earnings surprises, sector rotation, and any geopolitical risks "
                    "that could affect US equities next week. Write 3-4 concise bullet points.",
                    plugins=[{"id": "web", "max_results": 3}],
                    max_tokens=400,
                )
                asyncio.create_task(notifications.notify_weekly_macro_brief(brief_resp.text, brief_resp.citations))
            except Exception as _macro_err:
                print(f"[Discovery] Macro brief failed (non-fatal): {_macro_err}")

            asyncio.create_task(notifications.notify_alert(
                ":mag: Discovery Engine v2 starting — weekly backtest run. "
                "Results will arrive in #trading-decisions when complete (~2h).",
                level="INFO",
            ))

            try:
                start_time = asyncio.get_event_loop().time()
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "discovery.discovery_engine_v2",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                last_progress = asyncio.get_event_loop().time()

                async def _drain():
                    async for line in proc.stdout:
                        print(f"[Discovery] {line.decode('utf-8', errors='replace').rstrip()}")

                drain_task = asyncio.create_task(_drain())

                while not drain_task.done():
                    await asyncio.sleep(60)
                    now_t = asyncio.get_event_loop().time()
                    if now_t - last_progress >= 3600:
                        elapsed_min = int((now_t - start_time) / 60)
                        asyncio.create_task(notifications.notify_discovery_progress(elapsed_min))
                        last_progress = now_t

                await drain_task
                returncode = await proc.wait()
                if returncode != 0:
                    asyncio.create_task(notifications.notify_alert(
                        f":x: Discovery Engine v2 subprocess exited with code {returncode}",
                        level="ERROR",
                    ))

            except Exception as e:
                print(f"[Discovery] Subprocess error: {e}")
                asyncio.create_task(notifications.notify_alert(
                    f":x: Discovery loop error: {e}", level="ERROR"
                ))

    async def reddit_loop(self):
        """Loop 14 — polls r/wallstreetbets + r/stocks every 30 min for retail momentum signals."""
        if not Config.REDDIT_ENABLED:
            print("[Reddit] Disabled (REDDIT_ENABLED=False) — exiting loop.")
            return
        print("🤖 Starting Reddit Momentum Loop (30-min polling, alert-only)...")
        from strategies.reddit_strategy import RedditStrategy
        strategy = RedditStrategy()
        while True:
            try:
                if not self.trading_halted_for_day and not _bot_paused:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        asyncio.create_task(notifications.notify_reddit_signal(
                            sig["ticker"],
                            sig["score"],
                            sig["mention_count"],
                            sig["subreddits"],
                            sig["sample_titles"],
                        ))
            except Exception as e:
                print(f"[RedditLoop] Unexpected error: {e}")
            await asyncio.sleep(Config.REDDIT_POLL_INTERVAL)

    async def market_close_digest_loop(self):
        """Loop 16 — fires at exactly 4:00 PM EST every weekday with a daily close summary."""
        print("📊 Starting Market Close Digest Loop (4:00 PM EST weekdays)...")
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            target = now.replace(hour=16, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            while target.weekday() >= 5:  # skip Saturday (5) and Sunday (6)
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                trades_today = 0
                if self._db_engine:
                    today_str = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
                    with self._db_engine.connect() as conn:
                        row = conn.execute(sql_text(
                            "SELECT COUNT(*) AS cnt FROM signal_outcomes "
                            "WHERE entry_time::date = :today"
                        ), {"today": today_str}).mappings().fetchone()
                        trades_today = int(row["cnt"]) if row else 0

                account = await asyncio.to_thread(self.trading_client.get_account)
                daily_pnl_pct = 0.0
                if account and self.start_of_day_equity:
                    daily_pnl_pct = (
                        (float(account.equity) - self.start_of_day_equity)
                        / self.start_of_day_equity * 100
                    )

                vix = MACRO_SNAPSHOT.get("vix")
                regime = await self._get_market_regime()

                now_utc = datetime.now(pytz.utc)
                cooldown_syms = [
                    sym for sym, t in self.last_loss_times.items()
                    if (now_utc - t).total_seconds() < 86400
                ]

                asyncio.create_task(notifications.notify_market_close_digest(
                    trades_today,
                    daily_pnl_pct,
                    _health_state["signals_fired_total"],
                    vix,
                    regime,
                    cooldown_syms,
                ))
                print(f"[CloseDigest] 4pm digest sent — trades={trades_today} pnl={daily_pnl_pct:+.2f}%")
            except Exception as e:
                print(f"[CloseDigest] Error: {e}")

    async def prioritizer_loop(self):
        """Loop 20 — refreshes active_tickers table every 30 min for news scorer."""
        if not self._db_engine:
            print("[TickerPrioritizer] No DB engine — loop disabled")
            return
        from discovery.ticker_prioritizer import refresh_active_tickers
        print("📊 Starting Ticker Prioritizer Loop (30-min refresh)...")
        while True:
            try:
                await asyncio.to_thread(refresh_active_tickers, self._db_engine, self.stock_data_client)
            except Exception as e:
                print(f"[TickerPrioritizer] Refresh error: {e}")
            await asyncio.sleep(30 * 60)

    async def symbol_universe_loop(self):
        """Loop 15 — refreshes symbol_universe table every Sunday at midnight EST."""
        if not self._db_engine:
            print("[SymbolUniverse] No DB — loop disabled")
            return
        from discovery.symbol_universe import refresh_symbol_universe
        print("🌐 Starting Symbol Universe Loop (Sunday midnight EST)...")
        while True:
            now = datetime.now(pytz.timezone("America/New_York"))
            # Next Sunday midnight
            days_until_sunday = (6 - now.weekday()) % 7  # weekday() 6 = Sunday
            next_sunday = (now + timedelta(days=days_until_sunday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if now >= next_sunday:
                next_sunday += timedelta(days=7)
            await asyncio.sleep((next_sunday - now).total_seconds())
            try:
                print("[SymbolUniverse] Sunday midnight — refreshing symbol universe...")
                await asyncio.to_thread(
                    refresh_symbol_universe,
                    self._db_engine,
                    self.stock_data_client,
                )
            except Exception as e:
                print(f"[SymbolUniverse] Refresh error: {e}")

    async def grok_loop(self):
        """Loop 17 — polls Grok xAI X/Twitter for BTC/ETH sentiment (alert-only)."""
        if not Config.GROK_ENABLED:
            print("[Grok] Disabled (GROK_ENABLED=False) — exiting loop.")
            return
        if not Config.GROK_API_KEY:
            print("[Grok] No GROK_API_KEY — exiting loop.")
            return
        interval_min = Config.GROK_STRATEGY_INTERVAL_MINUTES
        print(f"🐦 Starting Grok X/Twitter Sentiment Loop ({interval_min}-min polling, alert-only)...")
        strategy = GrokStrategy()
        while True:
            try:
                if not _bot_paused:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        _health_state["signals_fired_total"] += 1
                        asyncio.create_task(notifications.notify_grok_signal(
                            sig["coin"],
                            sig["sentiment"],
                            sig["score"],
                            sig["confidence"],
                            sig["reasoning"],
                            sig["theme"],
                        ))
            except Exception as e:
                print(f"[GrokLoop] Unexpected error: {e}")
            await asyncio.sleep(interval_min * 60)

    async def grok_sentiment_loop(self):
        """Loop 20 — scores top-50 S&P 500 tickers via Grok xAI, writes to grok_sentiment."""
        if not Config.XAI_API_KEY:
            print("[GrokSentiment] No XAI_API_KEY — exiting loop.")
            return
        if not self._db_engine:
            print("[GrokSentiment] No DB engine — exiting loop.")
            return
        interval_min = Config.GROK_SENTIMENT_INTERVAL_MINUTES
        print(f"🐦 Starting Grok X/Twitter Stock Sentiment Loop ({interval_min}-min polling, top-50 tickers)...")
        while True:
            try:
                if not _bot_paused:
                    scored = await asyncio.to_thread(
                        refresh_grok_sentiment, self._db_engine
                    )
                    print(f"🐦 Grok sentiment refresh complete — {scored} tickers scored")
            except Exception as e:
                print(f"[GrokSentimentLoop] Unexpected error: {e}")
            await asyncio.sleep(interval_min * 60)

    async def webull_loop(self):
        """Loop 18 — polls Webull top-active/top-gainer every 15 min on weekdays (alert-only)."""
        if not Config.WEBULL_ENABLED:
            print("[Webull] Disabled (WEBULL_ENABLED=False) — exiting loop.")
            return
        print("📉 Starting Webull Contrarian Loop (15-min polling weekdays, alert-only)...")
        strategy = WebullStrategy()
        while True:
            try:
                now = datetime.now(pytz.timezone("America/New_York"))
                if now.weekday() < 5 and not _bot_paused and not strategy.disabled:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        _health_state["signals_fired_total"] += 1
                        asyncio.create_task(notifications.notify_webull_signal(
                            sig["ticker"],
                            sig["rank"],
                            sig["change_pct"],
                            sig["score"],
                            sig["reasoning"],
                        ))
            except Exception as e:
                print(f"[WebullLoop] Unexpected error: {e}")
            await asyncio.sleep(15 * 60)

    async def indicator_discovery_loop(self):
        """
        Loop 19 — fires every Saturday at 11 PM EST.

        Runs discovery.discovery_scheduler as a subprocess (CPU-heavy GP work never
        blocks the trading event loop). Requires the Friday discovery_engine_v2 run
        to have populated the discovery/data/ parquet cache first.
        """
        est = pytz.timezone("America/New_York")
        print("[IndicatorDiscovery] Loop started — fires every Saturday at 11:00 PM EST")
        while True:
            now                  = datetime.now(est)
            days_until_saturday  = (5 - now.weekday()) % 7
            target               = now.replace(hour=23, minute=0, second=0, microsecond=0)
            if days_until_saturday > 0:
                target += timedelta(days=days_until_saturday)
            elif now >= target:
                target += timedelta(days=7)

            await asyncio.sleep((target - now).total_seconds())

            asyncio.create_task(notifications.notify_alert(
                ":dna: Indicator Discovery Engine starting — overnight GP run. "
                "Results in #trading-decisions when complete (~1h).",
                level="INFO",
            ))

            try:
                start_time = asyncio.get_event_loop().time()
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "discovery.discovery_scheduler",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                async def _drain():
                    async for line in proc.stdout:
                        print(f"[IndicatorDiscovery] {line.decode('utf-8', errors='replace').rstrip()}")

                drain_task = asyncio.create_task(_drain())
                await drain_task
                returncode = await proc.wait()
                elapsed_min = int((asyncio.get_event_loop().time() - start_time) / 60)
                if returncode != 0:
                    asyncio.create_task(notifications.notify_alert(
                        f":x: Indicator Discovery subprocess exited with code {returncode}",
                        level="ERROR",
                    ))
                else:
                    print(f"[IndicatorDiscovery] Completed in {elapsed_min}m")
            except Exception as e:
                print(f"[IndicatorDiscovery] Subprocess error: {e}")
                asyncio.create_task(notifications.notify_alert(
                    f":x: Indicator Discovery loop error: {e}", level="ERROR"
                ))

    async def _log_startup_health(self):
        """Runs once after strategy instantiation, before async loops start.

        Prints one summary line per strategy showing live signal-module parameters,
        then prints market open/closed status from the Alpaca clock.
        """
        print("[Startup] ===== Strategy Health Check =====")

        for s in self.scalp_strategies:
            parts = [f"{s.name} ({type(s).__name__}): OK"]
            if hasattr(s, "_kalman"):
                k = s._kalman
                parts.append(f"Kalman Q={k.Q} R={k.R} noise_thresh={k.noise_thresh}")
            if hasattr(s, "_avwap"):
                a = s._avwap
                parts.append(
                    f"AVWAP window={a.window} "
                    f"dist_pct={a.distance_threshold_pct}% "
                    f"vol_ratio={a.volume_ratio_threshold}x"
                )
            if hasattr(s, "rr_ratio"):
                parts.append(f"rr_ratio={s.rr_ratio}:1")
            print("[Startup]  " + " | ".join(parts))

        for s in self.swing_symbol_strategies.values():
            parts = [f"{s.name} ({type(s).__name__}): OK"]
            if hasattr(s, "ema_short"):
                parts.append(
                    f"ema={s.ema_short}/{s.ema_long} "
                    f"rsi_period={s.rsi_period} "
                    f"rsi_gate=[{s.rsi_entry_low},{s.rsi_entry_high}]"
                )
            if hasattr(s, "_kalman"):
                k = s._kalman
                parts.append(f"Kalman Q={k.Q} R={k.R} noise_thresh={k.noise_thresh}")
            if hasattr(s, "_hurst"):
                h = s._hurst
                parts.append(f"Hurst window={h.rolling_window} trend_thresh={h.trending_threshold}")
            print("[Startup]  " + " | ".join(parts))

        try:
            clock = await asyncio.to_thread(self.trading_client.get_clock)
            est = pytz.timezone("America/New_York")
            if clock.is_open:
                closes = clock.next_close.astimezone(est).strftime("%Y-%m-%d %H:%M %Z")
                print(f"[Startup] Market: OPEN  | closes {closes}")
            else:
                opens = clock.next_open.astimezone(est).strftime("%Y-%m-%d %H:%M %Z")
                print(f"[Startup] Market: CLOSED | next open {opens}")
        except Exception as e:
            print(f"[Startup] Market clock unavailable: {e}")

        if self._db_engine:
            try:
                with self._db_engine.connect() as conn:
                    cb_rows = conn.execute(sql_text(
                        "SELECT strategy_name, reason, tripped_at "
                        "FROM strategy_circuit_breakers ORDER BY tripped_at"
                    )).mappings().fetchall()
                if cb_rows:
                    for cb in cb_rows:
                        print(
                            f"[Startup] CB ACTIVE: {cb['strategy_name']} — "
                            f"{cb['reason']} (since {str(cb['tripped_at'])[:19]})"
                        )
                else:
                    print("[Startup] Circuit breakers: none active")
            except Exception:
                print("[Startup] Circuit breakers: table not yet created (will be on next startup)")

        _cg = self._correlation_guard
        print(
            f"[Startup] CorrelationGuard: max_corr={_cg.max_portfolio_correlation:.2f} "
            f"max_correlated_pos={_cg.max_correlated_positions} "
            f"sector_map={len(CorrelationGuard.SECTOR_MAP)} symbols "
            f"lookback={_cg.price_lookback_days}d"
        )
        _si = self._si_signal
        print(
            f"[Startup] ShortInterestSignal: source=FINRA-CNMSshvol "
            f"threshold={_si.high_short_interest_threshold:.0%} "
            f"squeeze_price_chg={_si.squeeze_price_change_threshold:.0%} "
            f"cache_ttl={int(_si._cache_ttl // 3600)}h"
        )
        print(
            "[Startup] IndicatorDiscovery: population=50 generations=20 "
            f"symbols={len(Config.SWING_SYMBOLS)} IC_threshold=0.05 schedule=Sat_23:00_EST"
        )
        print("[Startup] ===================================")

    async def _cancel_symbol_positions(self, symbol: str) -> None:
        """Cancel open orders and close any open position for a symbol (CRITICAL decay)."""
        import traceback as _tb
        try:
            try:
                open_ords = await asyncio.to_thread(
                    self.trading_client.get_orders,
                    GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]),
                )
                for o in open_ords:
                    await asyncio.to_thread(self.trading_client.cancel_order_by_id, o.id)
                if open_ords:
                    print(f"[Decay] {symbol}: cancelled {len(open_ords)} open order(s)")
            except Exception as _oe:
                print(f"[Decay] {symbol}: order cancel failed (non-fatal) — {_oe}")
            try:
                await asyncio.to_thread(self.trading_client.get_open_position, symbol)
            except Exception:
                return  # no open position to close
            await asyncio.to_thread(self.trading_client.close_position, symbol)
            print(f"[Decay] {symbol}: position closed due to CRITICAL decay")
        except Exception:
            print(f"[Decay] {symbol}: cancel positions failed:\n{_tb.format_exc()}")

    async def decay_monitor_loop(self):
        """
        Loop 22 — every 6 hours, scan every strategy/symbol with >= 30 closed
        signals for decay and apply the tiered response (size cut / re-validation /
        disable + close). Fail-open: any exception logs a full traceback and the
        loop continues. The gating cache is refreshed each run for _process_symbol.
        """
        # Honor the kill switch at startup: log clearly and stay idle so the
        # disabled state is observable in Railway logs and no DB work is done.
        if not Config.DECAY_MONITOR_ENABLED:
            print("[Decay] Monitor disabled via DECAY_MONITOR_ENABLED=false at startup — Loop 22 idle")
            return

        print("🩺 Starting Strategy Decay Monitor Loop (Loop 22 — 6h cadence)...")
        # Prime the gating cache at startup so disabled strategies are blocked
        # before the first scan completes.
        try:
            self._decay_status_map = await asyncio.to_thread(self._decay_monitor.load_status_map)
            print(f"[Decay] Loaded {len(self._decay_status_map)} decay status entries at startup")
        except Exception as _le:
            print(f"[Decay] startup status load failed (non-fatal): {_le}")

        while True:
            try:
                current_regime = await self._get_current_regime_class()
                results = await asyncio.to_thread(
                    self._decay_monitor.get_decay_status_all_strategies, current_regime, False
                )
                print(f"[Decay] Scan complete — {len(results)} combo(s) with >= {Config.DECAY_MIN_SIGNALS} signals")

                for r in results:
                    try:
                        action = await asyncio.to_thread(
                            self._decay_monitor.apply_decay_response,
                            r["strategy_name"], r["symbol"], r,
                        )
                        if action.get("notify"):
                            msg, level = action["notify"]
                            asyncio.create_task(notifications.notify_alert(msg, level=level))
                        if action.get("cancel_positions"):
                            await self._cancel_symbol_positions(r["symbol"])
                    except Exception:
                        import traceback as _tb
                        print(f"[Decay] response failed for {r.get('symbol')}/{r.get('strategy_name')}:\n{_tb.format_exc()}")

                # Refresh the gating cache used by _process_symbol.
                self._decay_status_map = await asyncio.to_thread(self._decay_monitor.load_status_map)
            except Exception:
                import traceback as _tb
                print(f"[Decay] monitor loop error (fail-open):\n{_tb.format_exc()}")

            await asyncio.sleep(Config.DECAY_LOOP_INTERVAL_SECONDS)

    async def earnings_monitor_loop(self):
        """Loop 23 — fires at 8:00 AM EST weekdays to scan open positions for upcoming earnings.

        Advisory only: sends Slack alerts but never closes positions automatically.
        Gated by EARNINGS_PROTECTION_ENABLED.
        """
        if not Config.EARNINGS_PROTECTION_ENABLED:
            print("[EarningsMon] EARNINGS_PROTECTION_ENABLED=False — loop inactive")
            return
        print("[EarningsMon] Earnings monitor loop started (8:00 AM EST, Mon-Fri)")
        _est = pytz.timezone("America/New_York")
        while True:
            _now = datetime.now(_est)
            _target = _now.replace(hour=8, minute=0, second=0, microsecond=0)
            if _now >= _target:
                _target += timedelta(days=1)
            while _target.weekday() >= 5:  # skip weekends
                _target += timedelta(days=1)
            await asyncio.sleep((_target - _now).total_seconds())

            if not Config.EARNINGS_PROTECTION_ENABLED:
                continue
            if datetime.now(_est).weekday() >= 5:
                continue

            try:
                positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                _alerts: list[tuple] = []
                for _pos in positions:
                    _sym = getattr(_pos, "symbol", None)
                    if not _sym:
                        continue
                    try:
                        _has, _rpt = await self._check_upcoming_earnings(_sym, days_ahead=2)
                        if _has:
                            _pnl_pct = float(getattr(_pos, "unrealized_plpc", 0)) * 100
                            _alerts.append((_sym, _rpt, _pnl_pct))
                    except Exception as _se:
                        print(f"[EarningsMon] {_sym} check failed: {_se}")

                for _sym, _rpt, _pnl_pct in _alerts:
                    _msg = (
                        f"⚠️ Earnings alert — {_sym} reports {_rpt} | "
                        f"current P/L={_pnl_pct:+.2f}% | consider closing before announcement"
                    )
                    print(f"[EarningsMon] {_msg}")
                    asyncio.create_task(notifications.notify_alert(_msg, level="WARNING"))

                print(
                    f"[EarningsMon] Scanned {len(positions)} open position(s) — "
                    f"{len(_alerts)} earnings alert(s) sent"
                )
            except Exception as _e:
                print(f"[EarningsMon] Morning scan failed: {_e}")

    async def start_dual_engine(self):
        print("🚀 Hybrid Trading Bot starting...")

        import signal as _signal

        def _on_sigterm():
            print("[Shutdown] SIGTERM received — cancelling all tasks...")
            _health_state["crypto_polling_active"] = False
            for _task in asyncio.all_tasks():
                _task.cancel()

        try:
            asyncio.get_event_loop().add_signal_handler(_signal.SIGTERM, _on_sigterm)
            print("[Shutdown] SIGTERM handler registered.")
        except (NotImplementedError, RuntimeError):
            # Windows dev environment — signal handlers not supported in asyncio event loop
            pass

        log_model_config()

        # ── Validate Slack webhooks synchronously so Railway logs show result immediately ──
        _webhook_vars = {
            "SLACK_ALERTS_WEBHOOK":      Config.SLACK_ALERTS_WEBHOOK,
            "SLACK_DECISIONS_WEBHOOK":   Config.SLACK_DECISIONS_WEBHOOK,
            "SLACK_HEALTH_WEBHOOK":      Config.SLACK_HEALTH_WEBHOOK,
            "SLACK_PERFORMANCE_WEBHOOK": Config.SLACK_PERFORMANCE_WEBHOOK,
        }
        missing = [k for k, v in _webhook_vars.items() if not v]
        if missing:
            print(f"[Slack] WARNING: {len(missing)} webhook(s) not configured: {missing}")
            print("[Slack] Set these env vars in Railway → Project → Variables")
        else:
            print("[Slack] All 4 webhook env vars are set — testing #trading-alerts...")
            try:
                import requests as _req
                _resp = _req.post(
                    Config.SLACK_ALERTS_WEBHOOK,
                    json={"text": "🔌 Bot diagnostic test — Slack connection confirmed"},
                    timeout=10,
                )
                print(f"[Slack] Startup webhook test → HTTP {_resp.status_code}"
                      + ("" if _resp.status_code == 200 else f" ERROR: {_resp.text[:200]}"))
            except Exception as _e:
                print(f"[Slack] Startup webhook test → EXCEPTION: {_e}")

        if not await self._check_account_status():
            return

        await self._validate_swing_symbols()

        try:
            account = await asyncio.to_thread(self.trading_client.get_account)
            equity = float(account.equity)
            pnl = self.daily_pnl
            pnl_sign = "+" if pnl >= 0 else ""
            startup_msg = (
                f"🚀 Hybrid Trading Bot started\n"
                f"Equity: ${equity:,.2f}  |  "
                f"Opening equity: ${self.start_of_day_equity:,.2f}  |  "
                f"Daily P&L: {pnl_sign}${pnl:,.2f}\n"
                f"Swing watchlist: {', '.join(Config.SWING_SYMBOLS)}"
            )
        except Exception:
            startup_msg = "🚀 Hybrid Trading Bot has successfully started and connected to Slack!"

        print(startup_msg)
        asyncio.create_task(notifications.notify_alert(startup_msg, level="INFO"))

        # Ensure signal_outcomes table exists (creates if missing on Railway PostgreSQL)
        await asyncio.to_thread(self._ensure_signal_outcomes_table)
        # Cooldown table + restore from DB — must run before any swing cycle starts
        # so that cooldowns set before the last Railway redeploy are still honoured.
        await asyncio.to_thread(self._ensure_signal_cooldowns_table)
        self._load_signal_cooldowns()

        _initial_capital = self.start_of_day_equity or 0.0

        self.add_scalp_strategy(SMBStrategy("SMB Late Scalp", ema_window=9, rr_ratio=3,
                                             db_engine=self._db_engine, base_capital=_initial_capital))
        # Crypto momentum scalp (Task 6) — runs alongside SMB; best signal wins per tick.
        if Config.CRYPTO_MOMENTUM_ENABLED:
            self.add_scalp_strategy(CryptoMomentumStrategy("Crypto Momentum"))
            print("[Scalp] Crypto Momentum strategy enabled alongside SMB Late Scalp")

        # Per-symbol swing strategies — parameters from Discovery Engine walk-forward validation
        self.swing_symbol_strategies = {
            # 125/243 combos validated, best test Sharpe 0.87 — short EMA crossover dominates
            "COST":  SwingStrategy("COST Swing",  ema_short=20, ema_long=100, rsi_period=10, rsi_entry_low=35, rsi_entry_high=65,
                                   db_engine=self._db_engine, base_capital=_initial_capital),
            # 24/243 combos validated, best test Sharpe 0.90 — RSI21 + wide upper band required
            "BRK.B": SwingStrategy("BRK.B Swing", rsi_period=21, rsi_entry_low=40, rsi_entry_high=65,
                                   min_bars=240,
                                   db_engine=self._db_engine, base_capital=_initial_capital),
            # 9/243 combos validated — EMA50/200 with RSI upper=60 already matches defaults
            "SPY":   SwingStrategy("SPY Swing",   db_engine=self._db_engine, base_capital=_initial_capital),
            # 0/243 combos validated — defaults until further data
            "V":     SwingStrategy("V Swing",     db_engine=self._db_engine, base_capital=_initial_capital),
            # 0/243 combos validated — monitoring only (see swing_loop warning)
            "JPM":   SwingStrategy("JPM Swing",   db_engine=self._db_engine, base_capital=_initial_capital),
            "PG":    SwingStrategy("PG Swing",    db_engine=self._db_engine, base_capital=_initial_capital),
        }
        await self._log_startup_health()
        if self._db_engine:
            print("[TickerPrioritizer] Pre-populating active_tickers before loop startup...")
            try:
                from discovery.ticker_prioritizer import refresh_active_tickers, get_active_tickers
                await asyncio.to_thread(refresh_active_tickers, self._db_engine, self.stock_data_client)
                # Enrich sector map from Finnhub for the top 50 active symbols.
                # Fail-open — any errors are logged inside enrich_sector_map.
                _top_syms = await asyncio.to_thread(get_active_tickers, self._db_engine)
                await asyncio.to_thread(
                    CorrelationGuard.enrich_sector_map,
                    _top_syms[:50],
                    Config.FINNHUB_API_KEY,
                )
            except Exception as _tp_e:
                print(f"[TickerPrioritizer] Pre-populate error: {_tp_e}")
        await asyncio.gather(
            self.scalp_loop(),
            self.swing_loop(),
            self.prioritizer_loop(),
            self.news_loop(),
            self.truth_social_loop(),
            self.sec_edgar_loop(),
            self.fred_loop(),
            self.congressional_trading_loop(),
            self.health_report_loop(),
            self.performance_report_loop(),
            self.trailing_stop_monitor_loop(),
            self._exit_monitor_loop(),
            self.market_open_notification_loop(),
            self.discovery_loop(),
            self.reddit_loop(),
            self.symbol_universe_loop(),
            self.market_close_digest_loop(),
            self.grok_loop(),
            self.webull_loop(),
            self.indicator_discovery_loop(),
            self.grok_sentiment_loop(),
            self.decay_monitor_loop(),
            self.earnings_monitor_loop(),
        )

if __name__ == "__main__":
    start_health_server()  # port from HEALTH_PORT env var, default 8502
    bot = TradingBot()
    try:
        asyncio.run(bot.start_dual_engine())
    except Exception as _exc:
        try:
            import sentry_sdk as _sentry_sdk
            _sentry_sdk.capture_exception(_exc)
        except Exception:
            pass
        raise

