from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta
import pytz
import pandas as pd

def get_historical_bars(symbol, timeframe, days_back, data_client):
    """
    Fetches historical bars for a given symbol, accounting for the 15-minute 
    delay required by Alpaca's free market data plan.
    """
    # Alpaca Free Plan requires a 15-minute delay for SIP data
    # We use 16 minutes to be safe
    end_date = datetime.now(pytz.utc) - timedelta(minutes=16)
    start_date = end_date - timedelta(days=days_back)
    
    request_params = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=timeframe,
        start=start_date,
        end=end_date,
        feed='iex' # Use IEX feed for free tier
    )
    
    try:
        bars = data_client.get_stock_bars(request_params)
        
        # Check if we got any data
        if not hasattr(bars, 'df') or bars.df is None or bars.df.empty:
            print(f"No data returned for {symbol}")
            return None
        
        df = bars.df.copy()
        
        # Alpaca-py returns a MultiIndex (symbol, timestamp)
        # We want to make it easier for our strategies to read
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()
            # Rename 'timestamp' if it exists to match our strategy expectations
            if 'timestamp' in df.columns:
                df = df.set_index('timestamp')
        
        return df
    except Exception as e:
        print(f"Error fetching bars for {symbol}: {e}")
        return None
