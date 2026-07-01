import requests
import requests.adapters
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta
import pytz
import pandas as pd
from config import Config

# Default timeout (seconds) applied to every outbound HTTP request made by this
# bot — both raw `requests` calls and Alpaca SDK clients (see apply_http_timeout
# below). Without this, a hung connection blocks its thread/event-loop task
# forever, which can eventually exhaust the asyncio.to_thread executor pool and
# freeze all 23 async loops.
DEFAULT_HTTP_TIMEOUT = 30


class _TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """requests.HTTPAdapter that injects a default timeout when the caller
    didn't specify one on the individual request."""

    def __init__(self, *args, timeout: float = DEFAULT_HTTP_TIMEOUT, **kwargs):
        self._default_timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._default_timeout
        return super().send(request, **kwargs)


def apply_http_timeout(alpaca_client, timeout: float = DEFAULT_HTTP_TIMEOUT) -> None:
    """Mount a default-timeout adapter onto an Alpaca SDK client's requests.Session.

    alpaca-py's RESTClient._one_request() calls self._session.request(...) with
    no `timeout` kwarg at all, so every Alpaca HTTP call (orders, positions,
    bars, news) has NO timeout and can hang indefinitely. Call this once, right
    after constructing any TradingClient / StockHistoricalDataClient /
    CryptoHistoricalDataClient / NewsClient instance. Fail-open: if the SDK's
    internal session attribute ever changes name, this silently does nothing
    rather than crashing startup.
    """
    session = getattr(alpaca_client, "_session", None)
    if session is None:
        print(f"[HTTPTimeout] {type(alpaca_client).__name__} has no _session attribute — timeout not applied")
        return
    adapter = _TimeoutHTTPAdapter(timeout=timeout)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def get_finnhub_price(symbol):
    """
    Fetches real-time price from Finnhub to bypass Alpaca's 15-min delay.
    """
    if not Config.FINNHUB_API_KEY:
        return None

    # Finnhub free tier crypto is limited, so we only use it for stocks here.
    # For crypto, we'll rely on Alpaca's real-time data if available, or accept the delay.
    if "/" in symbol or symbol in ["BTCUSD", "ETHUSD"]:
        return None

    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={Config.FINNHUB_API_KEY}"
        response = requests.get(url, timeout=DEFAULT_HTTP_TIMEOUT)
        data = response.json()

        if 'c' in data and data['c'] != 0:
            return float(data['c'])
        return None
    except Exception as e:
        print(f"Error fetching Finnhub price for {symbol}: {e}")
        return None

def get_historical_bars(symbol, timeframe, days_back, data_client, is_crypto=False):
    """
    Fetches historical bars from Alpaca and updates the last price with Finnhub data for stocks.
    """
    # Alpaca SIP stock data requires a 15-minute delay (we use 16 to be safe).
    # Crypto data on Alpaca is real-time — no delay needed.
    end_date = datetime.now(pytz.utc) if is_crypto else datetime.now(pytz.utc) - timedelta(minutes=16)
    start_date = end_date - timedelta(days=days_back)
    
    try:
        if is_crypto:
            request_params = CryptoBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=timeframe,
                start=start_date,
                end=end_date
            )
            bars = data_client.get_crypto_bars(request_params)
        else:
            request_params = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=timeframe,
                start=start_date,
                end=end_date,
                feed='iex' # Use IEX feed for free tier
            )
            bars = data_client.get_stock_bars(request_params)
        
        if not hasattr(bars, 'df') or bars.df is None or bars.df.empty:
            print(f"No data returned for {symbol}")
            return None
        
        df = bars.df.copy()
        
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()
            if 'timestamp' in df.columns:
                df = df.set_index('timestamp')
            elif 'level_1' in df.columns: # Sometimes it resets differently
                df = df.set_index('level_1')

        # Guarantee ascending order — pandas_ta.vwap() requires a sorted DatetimeIndex
        df = df.sort_index()
        
        # Only use Finnhub for stocks (Finnhub free tier crypto is limited)
        if not is_crypto:
            real_time_price = get_finnhub_price(symbol)
            if real_time_price:
                # Append the real-time price as a new row to the dataframe
                # This allows strategies to see the current price as the 'latest' candle
                new_row = df.iloc[-1:].copy()
                new_row.index = [pd.Timestamp.now(tz=pytz.utc)]
                new_row['close'] = real_time_price
                new_row['open'] = real_time_price
                new_row['high'] = real_time_price
                new_row['low'] = real_time_price
                
                df = pd.concat([df, new_row])
                print(f"✅ Integrated Finnhub real-time price for {symbol}: ${real_time_price}")
        
        return df
    except Exception as e:
        print(f"Error fetching bars for {symbol}: {e}")
        return None

def get_spy_data(data_client, days_back=365):
    """
    Fetches historical SPY data for relative strength calculations.
    """
    return get_historical_bars("SPY", TimeFrame.Day, days_back, data_client, is_crypto=False)
