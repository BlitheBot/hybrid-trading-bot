from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta

def get_historical_bars(symbol, timeframe, days_back, data_client):
    """
    Fetches historical bars for a given symbol.
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    request_params = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=timeframe,
        start=start_date,
        end=end_date
    )
    
    bars = data_client.get_stock_bars(request_params)
    return bars.df
