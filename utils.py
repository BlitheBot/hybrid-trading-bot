import requests
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta
import pytz
import pandas as pd
from config import Config

def get_finnhub_price(symbol):
    """
    Fetches real-time price from Finnhub to bypass Alpaca's 15-min delay.
    """
    if not Config.FINNHUB_API_KEY:
        return None
    
    # Finnhub uses different symbols for crypto (e.g., BINANCE:BTCUSDT)
    # For now, we'll focus on stocks. If it's crypto, we'll skip Finnhub or use its crypto format.
    if "/" in symbol:
        return None 

    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={Config.FINNHUB_API_KEY}"
        response = requests.get(url)
        data = response.json()
        
        if 'c' in data and data['c'] != 0:
            return float(data['c'])
        return None
    except Exception as e:
        print(f"Error fetching Finnhub price for {symbol}: {e}")
        return None

def get_historical_bars(symbol, timeframe, days_back, data_client, is_crypto=False):
    """
    Fetches historical bars from Alpaca and updates the last price with Finnhub data.
    """
    end_date = datetime.now(pytz.utc) - timedelta(minutes=16)
    start_date = end_date - timedelta(days=days_back)
    
    try:
        if is_crypto:
            # Crypto Request
            request_params = CryptoBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=timeframe,
                start=start_date,
                end=end_date
            )
            bars = data_client.get_crypto_bars(request_params)
        else:
            # Stock Request
            request_params = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=timeframe,
                start=start_date,
                end=end_date,
                feed='iex'
            )
            bars = data_client.get_stock_bars(request_params)
        
        if not hasattr(bars, 'df') or bars.df is None or bars.df.empty:
            return None
        
        df = bars.df.copy()
        
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()
            if 'timestamp' in df.columns:
                df = df.set_index('timestamp')
            elif 'level_1' in df.columns: # Sometimes it resets differently
                df = df.set_index('level_1')
        
        # Only use Finnhub for stocks (Finnhub free tier crypto is limited)
        if not is_crypto:
            real_time_price = get_finnhub_price(symbol)
            if real_time_price:
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
