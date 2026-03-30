"""
OANDA API client — all communication with OANDA goes through here.
"""
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.orders as orders_ep
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.trades as trades_ep
from config import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT


class OandaClient:
    def __init__(self):
        env = "practice" if OANDA_ENVIRONMENT == "practice" else "live"
        self.client = oandapyV20.API(access_token=OANDA_API_KEY, environment=env)
        self.account_id = OANDA_ACCOUNT_ID

    def get_candles(self, pair, timeframe, count=200):
        """Fetch historical candles for a pair and timeframe."""
        params = {
            "granularity": timeframe,
            "count": count,
            "price": "M"  # midpoint prices
        }
        r = instruments.InstrumentsCandles(instrument=pair, params=params)
        self.client.request(r)
        return r.response["candles"]

    def get_live_price(self, pairs):
        """Get current bid/ask for a list of pairs."""
        params = {"instruments": ",".join(pairs)}
        r = pricing.PricingInfo(accountID=self.account_id, params=params)
        self.client.request(r)
        return r.response["prices"]

    def get_account(self):
        """Get account balance and open positions."""
        r = accounts.AccountDetails(accountID=self.account_id)
        self.client.request(r)
        return r.response["account"]

    def get_open_positions(self):
        """Return all currently open positions."""
        r = positions.OpenPositions(accountID=self.account_id)
        self.client.request(r)
        return r.response.get("positions", [])

    def place_market_order(self, pair, units, sl_price=None, tp_price=None,
                           trailing_distance=None):
        """
        Place a market order with optional stop-loss and take-profit.
        units > 0 = buy, units < 0 = sell.

        trailing_distance: price-unit distance for a trailing stop (e.g. 0.0030 = 30 pips on EUR_USD).
            When set, replaces sl_price with a trailing stop loss.
        sl_price: absolute fixed stop-loss price (used when trailing_distance is None).
        tp_price: absolute take-profit price (GTC, used regardless of SL type).
        """
        order = {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }
        if trailing_distance is not None:
            order["trailingStopLossOnFill"] = {
                "distance": str(trailing_distance),
                "timeInForce": "GTC",
            }
        elif sl_price is not None:
            order["stopLossOnFill"] = {
                "price": str(sl_price),
                "timeInForce": "GTC",
            }
        if tp_price is not None:
            order["takeProfitOnFill"] = {
                "price": str(tp_price),
                "timeInForce": "GTC",
            }
        r = orders_ep.OrderCreate(accountID=self.account_id, data={"order": order})
        self.client.request(r)
        return r.response

    def get_closed_trades(self, count=50):
        """
        Fetch the most recent closed trades from OANDA.
        Returns a list of dicts with: instrument, realizedPL, price (close), openTime, closeTime, id.
        """
        params = {"state": "CLOSED", "count": count}
        r = trades_ep.TradesList(accountID=self.account_id, params=params)
        self.client.request(r)
        raw = r.response.get("trades", [])
        result = []
        for t in raw:
            close_tx = t.get("closingTransactionIDs", [])
            result.append({
                "oanda_trade_id": t.get("id"),
                "instrument":     t.get("instrument"),
                "open_price":     float(t.get("price", 0)),
                "close_price":    float(t.get("averageClosePrice", 0)) or None,
                "pnl":            float(t.get("realizedPL", 0)),
                "units":          abs(int(t.get("initialUnits", 0))),
                "open_time":      t.get("openTime", ""),
                "close_time":     t.get("closeTime", ""),
            })
        return result

    def close_position(self, pair):
        """Close all units of an open position, detecting which side is open."""
        # First check which side exists
        r = positions.PositionDetails(accountID=self.account_id, instrument=pair)
        self.client.request(r)
        pos = r.response.get("position", {})

        long_units = int(pos.get("long", {}).get("units", 0))
        short_units = int(pos.get("short", {}).get("units", 0))

        data = {}
        if long_units > 0:
            data["longUnits"] = "ALL"
        if short_units < 0:
            data["shortUnits"] = "ALL"

        if not data:
            return {"message": "No open units to close"}

        r2 = positions.PositionClose(accountID=self.account_id, instrument=pair, data=data)
        self.client.request(r2)
        return r2.response
