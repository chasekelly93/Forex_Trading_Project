"""
Risk engine — all position sizing and safety checks live here.
Nothing gets executed unless this module approves it first.
Accepts an optional `params` dict to override defaults (used by Test Mode).
"""
from datetime import datetime, timezone
from config import (
    MAX_RISK_PER_TRADE_PCT,
    MAX_OPEN_POSITIONS,
    MAX_DAILY_LOSS_PCT
)
from data.oanda_client import OandaClient
from data.store import get_open_trades


# Pip sizes per pair
PIP_SIZE = {
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "AUD_USD": 0.0001,
    "NZD_USD": 0.0001, "USD_CAD": 0.0001, "USD_CHF": 0.0001,
    "USD_JPY": 0.01,
}

# Decimal precision per pair for price formatting
PRICE_DECIMALS = {
    "USD_JPY": 3,
}
DEFAULT_DECIMALS = 5

# Default params (used when test mode is off)
DEFAULT_PARAMS = {
    "bypass_hours":       False,
    "confidence_min":     0.60,
    "max_risk_pct":       MAX_RISK_PER_TRADE_PCT,
    "max_positions":      MAX_OPEN_POSITIONS,
    "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
    "stop_pips":           20,
    "take_profit_ratio":   2.0,   # TP distance = stop_pips × this ratio (2.0 = 2:1 R:R)
    "trailing_stop":       False, # replace fixed SL with a trailing stop
    "trailing_stop_pips":  30,    # trailing distance in pips (default 1.5× stop_pips)
}


class RiskEngine:
    def __init__(self):
        self.client = OandaClient()
        self._daily_start_balance = None

    def get_account_state(self):
        acct = self.client.get_account()
        return {
            "balance":     float(acct["balance"]),
            "nav":         float(acct["NAV"]),
            "open_pnl":    float(acct["unrealizedPL"]),
            "margin_used": float(acct["marginUsed"]),
        }

    def check_market_hours(self, bypass=False):
        if bypass:
            return True, "Market hours bypassed (Test Mode)"

        now = datetime.now(timezone.utc)
        weekday = now.weekday()
        hour = now.hour

        if weekday == 5 and hour >= 22:
            return False, "Market closed — weekend (Saturday after 22:00 UTC)"
        if weekday == 6 and hour < 22:
            return False, "Market closed — weekend (Sunday before 22:00 UTC)"
        if not (13 <= hour < 17):
            return False, f"Off-peak hours ({hour:02d}:00 UTC) — agent executes only 13:00–17:00 UTC (London/NY overlap)"

        return True, "Peak session active (London/NY overlap)"

    def check_confidence(self, thesis, min_confidence):
        conf = thesis.get("confidence", 0)
        if conf < min_confidence:
            return False, f"Confidence too low: {conf:.0%} < {min_confidence:.0%} required"
        return True, f"Confidence OK: {conf:.0%}"

    def check_max_positions(self, max_positions):
        open_trades = get_open_trades()
        if len(open_trades) >= max_positions:
            return False, f"Max positions reached ({len(open_trades)}/{max_positions})"
        return True, f"Positions OK ({len(open_trades)}/{max_positions})"

    def check_drawdown(self, max_daily_loss_pct):
        state = self.get_account_state()
        if self._daily_start_balance is None:
            self._daily_start_balance = state["balance"]

        loss_pct = ((self._daily_start_balance - state["nav"]) / self._daily_start_balance) * 100

        if loss_pct >= max_daily_loss_pct:
            return False, f"Kill switch triggered: daily loss {loss_pct:.2f}% >= {max_daily_loss_pct}%"
        return True, f"Drawdown OK: {loss_pct:.2f}% of {max_daily_loss_pct}% limit"

    def calculate_units(self, pair, stop_pips, max_risk_pct, balance=None):
        if balance is None:
            balance = self.get_account_state()["balance"]

        risk_amount = balance * (max_risk_pct / 100)
        pip = PIP_SIZE.get(pair, 0.0001)
        pip_value_per_unit = pip

        if pair.startswith("USD_"):
            prices = self.client.get_live_price([pair])
            mid = (float(prices[0]["bids"][0]["price"]) + float(prices[0]["asks"][0]["price"])) / 2
            pip_value_per_unit = pip / mid

        units = int(risk_amount / (stop_pips * pip_value_per_unit))
        return min(units, 100_000)

    def calculate_sl_tp(self, pair, direction, stop_pips, take_profit_ratio):
        """
        Calculate absolute stop-loss and take-profit prices using the current live bid/ask.
        BUY:  SL below ask, TP above ask
        SELL: SL above bid, TP below bid
        Returns (sl_price, tp_price).
        """
        pip      = PIP_SIZE.get(pair, 0.0001)
        decimals = PRICE_DECIMALS.get(pair, DEFAULT_DECIMALS)
        sl_dist  = stop_pips * pip
        tp_dist  = sl_dist * take_profit_ratio

        prices = self.client.get_live_price([pair])
        bid = float(prices[0]["bids"][0]["price"])
        ask = float(prices[0]["asks"][0]["price"])

        if direction == "BUY":
            sl_price = round(ask - sl_dist, decimals)
            tp_price = round(ask + tp_dist, decimals)
        else:  # SELL
            sl_price = round(bid + sl_dist, decimals)
            tp_price = round(bid - tp_dist, decimals)

        return sl_price, tp_price

    def calculate_trailing_distance(self, pair, trailing_stop_pips):
        """
        Convert pips to the price-unit distance OANDA expects for trailingStopLossOnFill.
        e.g. EUR_USD 30 pips → 0.00300, USD_JPY 30 pips → 0.300
        """
        pip      = PIP_SIZE.get(pair, 0.0001)
        decimals = PRICE_DECIMALS.get(pair, DEFAULT_DECIMALS)
        return round(trailing_stop_pips * pip, decimals)

    def approve(self, pair, thesis, params=None):
        """
        Run all checks. Returns (approved, reason, units).
        Pass a params dict to override defaults (Test Mode).
        """
        p = {**DEFAULT_PARAMS, **(params or {})}
        direction = thesis.get("direction")

        if direction == "NO_TRADE":
            return False, "Claude recommended NO_TRADE", 0
        if direction == "ERROR":
            return False, "Analysis error — skipping", 0

        # 1. Market hours
        ok, msg = self.check_market_hours(bypass=p["bypass_hours"])
        if not ok:
            return False, msg, 0

        # 2. Confidence
        ok, msg = self.check_confidence(thesis, p["confidence_min"])
        if not ok:
            return False, msg, 0

        # 3. No duplicate pair
        open_positions = self.client.get_open_positions()
        open_pairs = [pos["instrument"] for pos in open_positions]
        if pair in open_pairs:
            return False, f"Already have an open position in {pair}", 0

        # 4. Max positions
        ok, msg = self.check_max_positions(p["max_positions"])
        if not ok:
            return False, msg, 0

        # 5. Drawdown kill switch
        ok, msg = self.check_drawdown(p["max_daily_loss_pct"])
        if not ok:
            return False, msg, 0

        # Position sizing
        state = self.get_account_state()
        units = self.calculate_units(pair, p["stop_pips"], p["max_risk_pct"], balance=state["balance"])

        if direction == "SELL":
            units = -units

        return True, "All checks passed", units
