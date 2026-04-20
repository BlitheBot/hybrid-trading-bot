from abc import ABC, abstractmethod
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import OrderStatus

class BaseStrategy(ABC):
    def __init__(self, name):
        self.name = name

    @abstractmethod
    def generate_signals(self, market_data):
        """
        Generates trading signals (buy, sell, hold) based on market data.
        """
        pass

    @abstractmethod
    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        """
        Executes a trade based on the generated signal and risk management parameters.
        """
        pass

    def is_already_in_position(self, symbol, trading_client):
        """
        Checks if we already have an open position or pending order for this symbol.
        """
        try:
            # 1. Check open positions
            positions = trading_client.get_all_positions()
            for position in positions:
                if position.symbol == symbol:
                    return True
            
            # 2. Check pending orders
            order_filter = GetOrdersRequest(status=OrderStatus.OPEN, symbols=[symbol])
            orders = trading_client.get_orders(filter_data=order_filter)
            if orders:
                return True
                
            return False
        except Exception as e:
            print(f"Error checking existing positions for {symbol}: {e}")
            return False

    def calculate_safe_quantity(self, symbol, entry_price, stop_price, account, equity_risk_percent, max_buying_power_utilization_percent):
        """
        Calculates a safe quantity based on:
        1. 2% Equity Risk
        2. Max Buying Power Utilization (10% of available cash)
        3. Hard Position Cap (5% of total equity)
        """
        current_equity = float(account.equity)
        available_cash = float(account.buying_power)
        
        # 1. Risk-Based Quantity (2% of equity)
        risk_amount = current_equity * (equity_risk_percent / 100)
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return 0
        qty_from_risk = int(risk_amount / risk_per_share)

        # 2. Buying Power Cap (10% of available cash)
        max_cash_for_trade = available_cash * (max_buying_power_utilization_percent / 100)
        qty_from_buying_power = int(max_cash_for_trade / entry_price) if entry_price > 0 else 0

        # 3. SAFETY LOCK: Hard Position Cap (Max 5% of total equity per trade)
        # This prevents one trade from hogging all buying power even if risk allows it
        max_position_value = current_equity * 0.05 
        qty_from_hard_cap = int(max_position_value / entry_price) if entry_price > 0 else 0

        # Final quantity is the minimum of all three safety checks
        qty = min(qty_from_risk, qty_from_buying_power, qty_from_hard_cap)
        
        return qty
