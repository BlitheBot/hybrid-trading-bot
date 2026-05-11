import os
import sys
import time
import asyncio
import threading
from collections import deque
from datetime import datetime, timedelta
import pytz
import pandas as pd
from flask import Flask, jsonify
import notifications
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

import anthropic
import requests as _requests

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import CryptoDataStream

from sqlalchemy import create_engine, text as sql_text

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.smb_strategy import SMBStrategy
from strategies.swing_strategy import SwingStrategy
from strategies.news_strategy import NewsStrategy, _get_scan_sleep_seconds
from strategies.truth_social_strategy import TruthSocialStrategy
from strategies.sec_edgar_strategy import SECEdgarStrategy
from strategies.congressional_trading_strategy import CongressionalTradingStrategy
from strategies.fred_strategy import FREDStrategy, get_conviction_multiplier, MACRO_SNAPSHOT
from discovery.regime_adapter import apply_to_swing_strategy
from utils import get_historical_bars, get_finnhub_price

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
    "websocket_connected": False,
}

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
        "websocket_connected": _health_state["websocket_connected"],
    }), 200

def start_health_server(port=8502):
    """Run the Flask health server in a daemon thread so it never blocks the bot."""
    thread = threading.Thread(
        target=lambda: _health_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True
    )
    thread.start()
    print(f"🩺 Health endpoint running on http://0.0.0.0:{port}/health")


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
        print("DEBUG: Initializing TradingBot...")
        
        # Determine Base URL
        base_url = "https://paper-api.alpaca.markets" if Config.PAPER_TRADING else "https://api.alpaca.markets"
        print(f"DEBUG: Using Base URL: {base_url}")

        # Explicitly passing None for oauth_token to ensure no conflict
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
        
        # FIX: CryptoDataStream does not take a 'paper' argument in some SDK versions.
        # It determines the environment from the keys or uses a default.
        self.crypto_stream = CryptoDataStream(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY
        )
        self.scalp_strategies = []
        self.swing_strategies = []
        self.swing_symbol_strategies: dict[str, SwingStrategy] = {}
        self._open_trade_ids: dict = {}       # symbol → (row_id, entry_price, entry_time)
        self._trade_ids_lock = asyncio.Lock() # guards all _open_trade_ids mutations
        self._db_engine = self._init_db_engine()
        self._regime_cache = None             # (regime_str, timestamp)
        self._claude = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.daily_pnl = 0.0
        self.start_of_day_equity = 0.0
        self.last_pnl_reset_date = datetime.now(pytz.timezone('America/New_York')).date()
        self.trading_halted_for_day = False
        self.risk_multiplier = 1.0
        self.active_signals = {}
        self.last_loss_times = {}
        self._alerted_negative_ev: set[str] = set()
        self._last_ev_check_date = None
        self.last_evaluated_price = {}
        self._recent_signals: deque = deque(maxlen=50)
        self._sector_alert_cooldown: dict[str, datetime] = {}

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
                print(f"Account Status: {account.status}, Equity: ${float(account.equity):,.2f}, Buying Power: ${float(account.buying_power):,.2f}")
                
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
                count = conn.execute(sql_text("SELECT COUNT(*) FROM signal_outcomes")).scalar()
            _health_state["db_connected"] = True
            print(f"[DB] signal_outcomes table verified — {count} existing rows")
        except Exception as e:
            _health_state["db_connected"] = False
            print(f"[DB] Table setup failed: {e}")

    def _log_trade_entry(self, symbol: str, signal_type: str, entry_price: float,
                          ema_short: int, ema_long: int, rsi_at_entry: float,
                          macd_at_entry: float, regime: str, entry_time) -> int | None:
        if not self._db_engine:
            return None
        try:
            with self._db_engine.begin() as conn:
                result = conn.execute(sql_text("""
                    INSERT INTO signal_outcomes
                        (symbol, signal_type, entry_time, entry_price, ema_short, ema_long,
                         rsi_at_entry, macd_at_entry, market_regime)
                    VALUES (:symbol, :signal_type, :entry_time, :entry_price, :ema_short, :ema_long,
                            :rsi_at_entry, :macd_at_entry, :market_regime)
                    RETURNING id
                """), {
                    "symbol": symbol, "signal_type": signal_type, "entry_time": entry_time,
                    "entry_price": float(entry_price), "ema_short": int(ema_short),
                    "ema_long": int(ema_long), "rsi_at_entry": float(rsi_at_entry),
                    "macd_at_entry": float(macd_at_entry), "market_regime": regime,
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
        return regime

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
        try:
            shared_data = (
                f"Symbol: {symbol}  Price: ${signal.get('entry_price', 0):.2f}  "
                f"RSI({getattr(strategy, 'rsi_period', 14)}): {signal.get('rsi_at_entry', 'N/A')}  "
                f"MACD: {signal.get('macd_at_entry', 'N/A')}  "
                f"EMA{getattr(strategy, 'ema_short', 50)} crossed above EMA{getattr(strategy, 'ema_long', 200)}.  "
                f"Signal detail: {signal.get('reasoning', '')}"
            )

            def _call(prompt):
                return self._claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=150,
                    messages=[{"role": "user", "content": prompt}],
                ).content[0].text.strip()

            bull, bear = await asyncio.gather(
                asyncio.to_thread(_call,
                    f"You are a bullish stock analyst. Make the strongest case FOR buying {symbol} right now. "
                    f"Data: {shared_data}  Respond in 2 sentences only."
                ),
                asyncio.to_thread(_call,
                    f"You are a bearish stock analyst. Make the strongest case AGAINST buying {symbol} right now. "
                    f"Data: {shared_data}  Respond in 2 sentences only."
                ),
            )
            decision = await asyncio.to_thread(_call,
                f"Bull case: {bull}\nBear case: {bear}\n"
                f"Should we buy {symbol} right now? "
                f"Start your response with BUY or SKIP, then give one sentence reason."
            )

            proceed = decision.upper().startswith("BUY")
            summary = f"*Bull:* {bull}\n*Bear:* {bear}\n*Decision:* {decision}"
            return proceed, summary

        except Exception as e:
            print(f"[Debate] {symbol} failed: {e}")
            return True, "debate unavailable"

    # ── Pre-trade hook: fundamentals → debate (Tasks 2 & 3) ──────────────────

    async def _swing_pre_trade_hook(self, symbol: str, signal: dict, strategy) -> tuple[bool, str]:
        # Earnings filter — reduce position size to 25% if earnings within 48h
        if Config.EARNINGS_FILTER_ENABLED:
            has_earnings, report_date = await self._check_upcoming_earnings(symbol, days_ahead=2)
            if has_earnings:
                strategy.earnings_override_multiplier = 0.25
                strategy.earnings_size_note = (
                    f"⚠️ Earnings within 48hrs for {symbol} "
                    f"(report: {report_date}) — reducing position size to 25%"
                )
                print(f"[Earnings] {strategy.earnings_size_note}")

        # Fundamentals check
        proceed, reason = await self._check_fundamentals(symbol)
        if not proceed:
            print(f"[Fundamentals] Blocking {symbol}: {reason}")
            asyncio.create_task(notifications.notify_trade_skipped(symbol, "Fundamentals", reason))
            return False, f"Fundamentals: {reason}"

        # Task 2 — Bull/Bear debate
        proceed, debate_summary = await self._debate_trade(symbol, signal, strategy)
        action_label = "BUY" if proceed else "SKIP"
        asyncio.create_task(notifications.notify_trade_decision(
            symbol, "Bull/Bear Debate",
            {"signal": "buy" if proceed else "hold",
             "reasoning": f"[{action_label}] {debate_summary}",
             "confidence": 0.0},
        ))

        if not proceed:
            return False, f"Debate SKIP — {debate_summary}"

        return True, debate_summary

    async def _process_symbol(self, symbol, strategies, is_crypto, risk_percent, stop_loss_percent,
                              current_price=None, pre_execute_hook=None):
        if self.trading_halted_for_day:
            return

        await self._update_loss_cache()
        if symbol in self.last_loss_times:
            if datetime.now(pytz.utc) - self.last_loss_times[symbol] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                return # Blocked by cooldown

        client = self.crypto_data_client if is_crypto else self.stock_data_client
        data = get_historical_bars(symbol, TimeFrame.Day, 365, client, is_crypto=is_crypto)
        
        if data is None:
            return

        # Ensure 'symbol' column exists in the DataFrame
        if 'symbol' not in data.columns:
            data['symbol'] = symbol

        if current_price is not None:
            current_bar = pd.DataFrame([{
                'timestamp': datetime.now(pytz.utc),
                'open': current_price,
                'high': current_price,
                'low': current_price,
                'close': current_price,
                'volume': 0,
                'vwap': current_price,
                'symbol': symbol  # Add symbol to the current bar as well
            }])
            data = pd.concat([data, current_bar], ignore_index=True)

        for strategy in strategies:
            print(f"Running strategy: {strategy.name} for {symbol}")
            if isinstance(strategy, SMBStrategy):
                signal = strategy.generate_signals(data, self.stock_data_client)
            else:
                signal = strategy.generate_signals(data)
            
            if signal:
                if self.trading_halted_for_day:
                    asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Daily loss limit hit"))
                    continue
                    
                if signal['signal'] == "hold":
                    asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Signal was hold (insufficient RR ratio or bear case stronger)"))
                    continue

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

                    # Pre-execute hook: fundamentals check + bull/bear debate (swing only)
                    if pre_execute_hook:
                        hook_proceed, hook_reason = await pre_execute_hook(symbol, signal, strategy)
                        if not hook_proceed:
                            asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, hook_reason))
                            continue

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
                        symbol, strategy.name, block_msg
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

                _notes = [n for n in [
                    getattr(strategy, 'discovery_size_note', None),
                    getattr(strategy, 'earnings_size_note', None),
                    getattr(strategy, 'bear_market_note', None),
                    vix_note,
                ] if n]
                asyncio.create_task(notifications.notify_trade_decision(
                    symbol, strategy.name, signal,
                    discovery_note="\n".join(_notes) if _notes else None,
                ))

                entry_time = datetime.now(pytz.utc)
                try:
                    earnings_mult    = getattr(strategy, 'earnings_override_multiplier', 1.0)
                    confidence_mult  = signal.get('confidence_multiplier', 1.0)
                    scaled_risk_percent = (
                        risk_percent * self.risk_multiplier
                        * earnings_mult * vix_risk_mult * confidence_mult
                    )
                    strategy.execute_trade(
                        signal,
                        self.trading_client,
                        scaled_risk_percent,
                        stop_loss_percent,
                        Config.TAKE_PROFIT_PERCENT,
                        Config.MAX_BUYING_POWER_UTILIZATION_PERCENT
                    )

                    # Task 1 — log entry to signal_outcomes after successful execute_trade
                    if signal['signal'] == 'buy':
                        disc_type = getattr(strategy, 'discovery_strategy_type', None)
                        if isinstance(strategy, SwingStrategy) and disc_type:
                            signal_type = f"discovery_{disc_type}"
                        elif isinstance(strategy, SwingStrategy):
                            signal_type = 'swing_long'
                        else:
                            signal_type = 'scalp_long'
                        regime = await self._get_market_regime()
                        row_id = await asyncio.to_thread(
                            self._log_trade_entry,
                            symbol, signal_type, float(signal.get('entry_price', 0)),
                            getattr(strategy, 'ema_short', 50), getattr(strategy, 'ema_long', 200),
                            float(signal.get('rsi_at_entry', 0)), float(signal.get('macd_at_entry', 0)),
                            regime, entry_time,
                        )
                        if row_id:
                            async with self._trade_ids_lock:
                                self._open_trade_ids[symbol] = (row_id, float(signal.get('entry_price', 0)), entry_time)

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

    async def _on_crypto_trade(self, trade):
        symbol = trade.symbol
        price = trade.price
        
        if symbol in self.last_evaluated_price:
            last_price = self.last_evaluated_price[symbol]
            if abs(price - last_price) / last_price < Config.MIN_PRICE_MOVEMENT_PCT:
                return # Not enough movement
        self.last_evaluated_price[symbol] = price
        
        winner = await self._get_stronger_momentum_crypto()
        if winner and symbol != winner:
            return # Skip if this symbol doesn't have the strongest momentum
            
        await self._process_symbol(
            symbol, 
            self.scalp_strategies, 
            is_crypto=True, 
            risk_percent=Config.EQUITY_RISK_PER_TRADE_PERCENT, 
            stop_loss_percent=Config.CRYPTO_SCALP_STOP_LOSS_PERCENT,
            current_price=price
        )

    async def scalp_loop(self):
        print(f"🚀 Starting Crypto Scalping Bot for {Config.SCALP_SYMBOLS} (Websocket)...")
        retry_delay = 5
        consecutive_failures = 0
        while True:
            print(f"WebSocket retry in {retry_delay}s...")
            await asyncio.sleep(retry_delay)

            connect_time = time.time()
            try:
                self.crypto_stream = CryptoDataStream(
                    api_key=Config.ALPACA_API_KEY,
                    secret_key=Config.ALPACA_SECRET_KEY
                )
                self.crypto_stream.subscribe_trades(self._on_crypto_trade, *Config.SCALP_SYMBOLS)
                # _connect() makes a single connection attempt and returns when it drops.
                # _run_forever() has an internal retry loop that bypasses our backoff — avoid it.
                _health_state["websocket_connected"] = True
                await self.crypto_stream._connect()
                _health_state["websocket_connected"] = False
                print("WebSocket stream closed cleanly.")
            except Exception as e:
                _health_state["websocket_connected"] = False
                msg = f"WebSocket error: {e}"
                print(msg)
                asyncio.create_task(notifications.notify_alert(f"{msg} Retrying in {retry_delay}s..."))

            if time.time() - connect_time > 60:
                retry_delay = 5
                consecutive_failures = 0
                print(f"WebSocket was stable for >60s. Backoff reset to 5s.")
            else:
                retry_delay = min(retry_delay * 2, 60)
                consecutive_failures += 1
                if consecutive_failures >= 10:
                    asyncio.create_task(notifications.notify_alert(
                        "Crypto websocket has failed 10 consecutive times — possible Alpaca outage"
                    ))
                    consecutive_failures = 0

    async def trailing_stop_monitor_loop(self):
        print("🛡️ Starting Trailing Stop Monitor Loop...")
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        while True:
            await asyncio.sleep(Config.TRAILING_STOP_MONITOR_INTERVAL)
            try:
                positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                for pos in positions:
                    unrealized_pct = float(pos.unrealized_plpc)
                    if unrealized_pct >= Config.TRAILING_STOP_ACTIVATION_PCT:
                        req = GetOrdersRequest(
                            status=QueryOrderStatus.OPEN,
                            symbols=[pos.symbol]
                        )
                        orders = await asyncio.to_thread(self.trading_client.get_orders, req)
                        for order in orders:
                            if order.order_type.value == "stop":
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

        if len(recent_syms) < 3:
            return

        last_alert = self._sector_alert_cooldown.get(sector)
        if last_alert and now - last_alert < window:
            return

        self._sector_alert_cooldown[sector] = now
        syms_str = ", ".join(sorted(recent_syms))
        await notifications.notify_alert(
            f"🔥 Sector hot: {sector} — {len(recent_syms)} signals in 30min "
            f"({syms_str}). Possible sector rotation.",
            level="INFO",
        )

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

    async def swing_loop(self):
        print(f"📈 Starting Stock Swing Bot for {Config.SWING_SYMBOLS} (10:30 AM EST Polling)...")
        # Symbols with no statistically validated edge — evaluated but flagged in logs
        _no_edge = {"JPM", "PG"}
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            target = now.replace(hour=10, minute=30, second=0, microsecond=0)

            # If it's past 10:30 AM, move to tomorrow.
            if now >= target:
                target += timedelta(days=1)
            # Skip weekends
            while target.weekday() > 4: # 5=Sat, 6=Sun
                target += timedelta(days=1)

            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            await self._check_account_status()
            print(f"📈 Swing evaluation starting at {datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %I:%M:%S %p')} EST")
            spy_bars = None
            try:
                spy_bars = get_historical_bars("SPY", TimeFrame.Day, 365, self.stock_data_client)
            except Exception:
                pass

            swing_regime = await self._get_market_regime()

            # Time-of-day safety multiplier: swing loop fires at 10:30am but guard
            # against any edge case where it runs outside 10:00–11:00am EST.
            _eval_now = datetime.now(pytz.timezone('America/New_York'))
            tod_mult = 1.0 if 10 <= _eval_now.hour < 11 else 0.7
            if tod_mult < 1.0:
                print(
                    f"[Swing] Evaluation outside 10–11am EST "
                    f"({_eval_now.strftime('%H:%M')}) — applying 0.7x conviction"
                )

            for symbol in Config.SWING_SYMBOLS:
                if symbol in _no_edge:
                    print(f"[Swing] {symbol}: no statistically validated edge (p>0.05 across all 243 discovery combos) — monitoring only")

                strategy = self.swing_symbol_strategies.get(symbol)
                if strategy is None:
                    print(f"[Swing] {symbol}: no strategy configured, skipping")
                    continue

                # Check for an approved discovery strategy; upgrade if ema_trend found.
                # Hardcoded strategies always use 100% size — no change to existing behavior.
                discovery  = apply_to_swing_strategy(symbol, spy_bars)
                risk_to_use = Config.SWING_EQUITY_RISK_PERCENT

                if discovery is not None:
                    s_type, s_params = discovery
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
                        # Attribute used by _process_symbol to tag signal_outcomes rows
                        upgraded.discovery_strategy_type = s_type
                        strategy = upgraded

                        # Fetch backtest win_rate for this approved combo
                        backtest_win_rate = None
                        if self._db_engine:
                            try:
                                with self._db_engine.connect() as conn:
                                    bwr = conn.execute(sql_text("""
                                        SELECT win_rate FROM discovery_results
                                        WHERE symbol = :sym
                                          AND strategy_type = :st
                                          AND status = 'approved'
                                        ORDER BY test_sharpe DESC NULLS LAST
                                        LIMIT 1
                                    """), {"sym": symbol, "st": s_type}).scalar()
                                backtest_win_rate = float(bwr) if bwr is not None else None
                            except Exception:
                                pass

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

                # Skip new entries entirely if earnings today or tomorrow
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

                # Bear market position size reduction
                if swing_regime == 'bear':
                    risk_to_use *= Config.BEAR_MARKET_SIZE_REDUCTION
                    strategy.bear_market_note = "🐻 Bear market mode — position size reduced 50%"
                    print(f"[Swing] {symbol}: bear market mode — risk_to_use={risk_to_use:.3f}%")

                # Time-of-day safety multiplier
                risk_to_use *= tod_mult

                print(f"Evaluating {symbol} for swing signals [{strategy.name}]")
                await self._process_symbol(
                    symbol,
                    [strategy],
                    is_crypto=False,
                    risk_percent=risk_to_use,
                    stop_loss_percent=Config.STOP_LOSS_PERCENT,
                    pre_execute_hook=self._swing_pre_trade_hook,
                )
            print(f"📈 Swing evaluation complete for {len(Config.SWING_SYMBOLS)} symbols.")

    async def health_report_loop(self):
        print("🏥 Starting Daily Health Report Loop (9:00 AM EST)...")
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
            
            await self._check_account_status()
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                uptime_seconds = (datetime.now(pytz.utc) - _bot_start_time).total_seconds()
                uptime_str = str(timedelta(seconds=int(uptime_seconds)))
                equity = float(account.equity)
                buying_power = float(account.buying_power)
                asyncio.create_task(notifications.notify_daily_health(uptime_str, equity, buying_power, self.daily_pnl))

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

    async def news_loop(self):
        """Continuously polls Benzinga news via Alpaca and routes signals to Slack / trade execution."""
        print("📰 Starting Benzinga News Sentiment Loop...")
        strategy = NewsStrategy()
        while True:
            try:
                if not self.trading_halted_for_day:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker  = sig["ticker"]
                        strength = sig["strength"]
                        action  = sig["action"]

                        # Always alert Slack about the signal
                        asyncio.create_task(notifications.notify_news_signal(
                            ticker, sig["headline"], sig["sentiment"], strength, action
                        ))

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

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (news)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (news)"))
                            continue
                        except Exception:
                            pass  # No open position — proceed

                        # Execute using swing risk parameters
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
                if not self.trading_halted_for_day:
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

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (TS)"))
                            continue
                        except Exception:
                            pass

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
                if not self.trading_halted_for_day:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker   = sig["ticker"]
                        strength = sig["strength"]
                        action   = sig["action"]

                        # Always send to #trading-decisions with 📋 emoji
                        asyncio.create_task(notifications.notify_edgar_signal(
                            ticker, sig["headline"], sig["sentiment"], strength, action
                        ))

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

                        # Execute using swing risk parameters (buys only)
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
                if not self.trading_halted_for_day:
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

    async def start_dual_engine(self):
        print("🚀 Hybrid Trading Bot starting...")

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

        self.add_scalp_strategy(SMBStrategy("SMB Late Scalp", ema_window=9, rr_ratio=3))

        # Per-symbol swing strategies — parameters from Discovery Engine walk-forward validation
        self.swing_symbol_strategies = {
            # 125/243 combos validated, best test Sharpe 0.87 — short EMA crossover dominates
            "COST":  SwingStrategy("COST Swing",  ema_short=20, ema_long=100, rsi_period=10, rsi_entry_low=35, rsi_entry_high=65),
            # 24/243 combos validated, best test Sharpe 0.90 — RSI21 + wide upper band required
            "BRK.B": SwingStrategy("BRK.B Swing", rsi_period=21, rsi_entry_low=40, rsi_entry_high=65),
            # 9/243 combos validated — EMA50/200 with RSI upper=60 already matches defaults
            "SPY":   SwingStrategy("SPY Swing"),
            # 0/243 combos validated — defaults until further data
            "V":     SwingStrategy("V Swing"),
            # 0/243 combos validated — monitoring only (see swing_loop warning)
            "JPM":   SwingStrategy("JPM Swing"),
            "PG":    SwingStrategy("PG Swing"),
        }
        await asyncio.gather(
            self.scalp_loop(),
            self.swing_loop(),
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
        )

if __name__ == "__main__":
    start_health_server(port=8502)
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

