"""
Risk engine — all position sizing and safety checks live here.
Nothing gets executed unless this module approves it first.
"""
from config import (
    MAX_RISK_PER_TRADE_PCT,
    MAX_OPEN_POSITIONS,
    MAX_DAILY_LOSS_PCT
)
from data.oanda_client import OandaClient
from data.store import get_open_trades


# Pip sizes per pair (how much 1 pip is worth in price terms)
PIP_SIZE = {
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "AUD_USD": 0.0001,
    "NZD_USD": 0.0001, "USD_CAD": 0.0001, "USD_CHF": 0.0001,
    "USD_JPY": 0.01,
}


class RiskEngine:
    def __init__(self):
        self.client = OandaClient()
        self._daily_start_balance = None

    def get_account_state(self):
        acct = self.client.get_account()
        return {
            "balance":    float(acct["balance"]),
            "nav":        float(acct["NAV"]),
            "open_pnl":   float(acct["unrealizedPL"]),
            "margin_used": float(acct["marginUsed"]),
        }

    def check_max_positions(self):
        """Return True if we're under the max open position limit."""
        open_trades = get_open_trades()
        if len(open_trades) >= MAX_OPEN_POSITIONS:
            return False, f"Max positions reached ({len(open_trades)}/{MAX_OPEN_POSITIONS})"
        return True, f"Positions OK ({len(open_trades)}/{MAX_OPEN_POSITIONS})"

    def check_drawdown(self):
        """
        Return True if daily loss is within limit.
        Compares current NAV to the balance at session start.
        """
        state = self.get_account_state()
        balance = state["balance"]

        if self._daily_start_balance is None:
            self._daily_start_balance = balance

        loss_pct = ((self._daily_start_balance - state["nav"]) / self._daily_start_balance) * 100

        if loss_pct >= MAX_DAILY_LOSS_PCT:
            return False, f"Kill switch triggered: daily loss {loss_pct:.2f}% >= {MAX_DAILY_LOSS_PCT}%"

        return True, f"Drawdown OK: {loss_pct:.2f}% of {MAX_DAILY_LOSS_PCT}% limit"

    def check_confidence(self, thesis, min_confidence=0.60):
        """Only trade if Claude's confidence meets the minimum threshold."""
        conf = thesis.get("confidence", 0)
        if conf < min_confidence:
            return False, f"Confidence too low: {conf:.0%} < {min_confidence:.0%} required"
        return True, f"Confidence OK: {conf:.0%}"

    def calculate_units(self, pair, stop_pips, balance=None):
        """
        Calculate position size in units based on:
        - Account balance
        - Max risk % per trade
        - Distance to stop loss in pips

        For USD-quoted pairs (EUR_USD etc): straightforward
        For JPY pairs and inverted pairs: pip value differs
        """
        if balance is None:
            balance = self.get_account_state()["balance"]

        risk_amount = balance * (MAX_RISK_PER_TRADE_PCT / 100)
        pip = PIP_SIZE.get(pair, 0.0001)

        # Standard lot = 100,000 units, 1 pip on EUR_USD = $10 per lot
        # For USD-base quote pairs, pip value per unit = pip size
        # units = risk_amount / (stop_pips * pip_value_per_unit)
        pip_value_per_unit = pip  # $0.0001 per unit for most pairs

        # For pairs where USD is the base (USD_JPY, USD_CAD, USD_CHF),
        # pip value in USD = pip / current_price (approximated)
        if pair.startswith("USD_"):
            # Get current price to convert
            prices = self.client.get_live_price([pair])
            mid = (float(prices[0]["bids"][0]["price"]) + float(prices[0]["asks"][0]["price"])) / 2
            pip_value_per_unit = pip / mid

        units = int(risk_amount / (stop_pips * pip_value_per_unit))

        # Cap at reasonable size — never more than 1 standard lot in practice mode
        units = min(units, 100_000)
        return units

    def approve(self, pair, thesis):
        """
        Run all checks. Returns (approved: bool, reason: str, units: int).
        This is the single gate every trade must pass through.
        """
        direction = thesis.get("direction")

        if direction == "NO_TRADE":
            return False, "Claude recommended NO_TRADE", 0

        if direction == "ERROR":
            return False, "Analysis error — skipping", 0

        # Check 1: confidence
        ok, msg = self.check_confidence(thesis)
        if not ok:
            return False, msg, 0

        # Check 2: already have a position in this pair?
        open_positions = self.client.get_open_positions()
        open_pairs = [p["instrument"] for p in open_positions]
        if pair in open_pairs:
            return False, f"Already have an open position in {pair}", 0

        # Check 3: max positions
        ok, msg = self.check_max_positions()
        if not ok:
            return False, msg, 0

        # Check 3: drawdown kill switch
        ok, msg = self.check_drawdown()
        if not ok:
            return False, msg, 0

        # Position sizing — estimate stop distance in pips from ATR
        # Claude gives stop as text; we'll use 2x ATR as a safe default
        state = self.get_account_state()
        pip = PIP_SIZE.get(pair, 0.0001)

        # Fallback: 20 pip stop if we can't parse it
        stop_pips = 20
        units = self.calculate_units(pair, stop_pips, balance=state["balance"])

        if direction == "SELL":
            units = -units  # negative = sell

        return True, "All checks passed", units
