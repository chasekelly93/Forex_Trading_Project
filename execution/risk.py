"""
Risk engine — all position sizing and safety checks live here.
Nothing gets executed unless this module approves it first.
Accepts an optional `params` dict to override defaults (used by Test Mode).
"""
import threading
from datetime import datetime, timezone
from config import (
    MAX_RISK_PER_TRADE_PCT,
    MAX_OPEN_POSITIONS,
    MAX_DAILY_LOSS_PCT
)
from data.oanda_client import OandaClient
from data.store import get_open_trades

_pair_locks = {}
_pair_locks_mutex = threading.Lock()


def _get_pair_lock(pair):
    with _pair_locks_mutex:
        if pair not in _pair_locks:
            _pair_locks[pair] = threading.Lock()
        return _pair_locks[pair]


# Pip sizes per pair
PIP_SIZE = {
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "AUD_USD": 0.0001,
    "NZD_USD": 0.0001, "USD_CAD": 0.0001, "USD_CHF": 0.0001,
    "USD_JPY": 0.01,
    "EUR_JPY": 0.01, "GBP_JPY": 0.01, "AUD_JPY": 0.01,
    "CAD_JPY": 0.01, "CHF_JPY": 0.01, "NZD_JPY": 0.01,
}

# Estimated slippage per pair in pips (entry + stop trigger)
SLIPPAGE_PIPS = {
    "EUR_USD": 1.5, "GBP_USD": 1.5, "USD_JPY": 1.5,
    "AUD_USD": 1.5, "USD_CAD": 1.5, "USD_CHF": 1.5, "NZD_USD": 1.5,
}
DEFAULT_SLIPPAGE = 2.5  # pips for crosses/minors

# Decimal precision per pair for price formatting
PRICE_DECIMALS = {
    "USD_JPY": 3,
}
DEFAULT_DECIMALS = 5

# Optimal trading sessions per pair (UTC hours, inclusive start, exclusive end)
PAIR_SESSIONS = {
    "USD_JPY": [(0, 8), (13, 17)],   # Tokyo + NY/London overlap
    "EUR_JPY": [(0, 8), (7, 17)],
    "GBP_JPY": [(7, 17)],
    "AUD_USD": [(0, 8), (13, 17)],   # Sydney/Tokyo + NY overlap
    "NZD_USD": [(0, 8), (13, 17)],
    # Default (majors): London/NY overlap only
}
DEFAULT_SESSION = [(13, 17)]

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
    "atr_multiplier":      1.5,   # stop = ATR(H4) × this multiplier
    "use_atr_stops":       True,  # when True, overrides stop_pips with ATR-based distance
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

    def check_market_hours(self, bypass=False, pair=None):
        if bypass:
            return True, "Market hours bypassed (Test Mode)"

        now = datetime.now(timezone.utc)
        weekday = now.weekday()
        hour = now.hour

        if weekday == 5 and hour >= 22:
            return False, "Market closed — weekend (Saturday after 22:00 UTC)"
        if weekday == 6 and hour < 22:
            return False, "Market closed — weekend (Sunday before 22:00 UTC)"

        sessions = PAIR_SESSIONS.get(pair, DEFAULT_SESSION) if pair else DEFAULT_SESSION
        in_session = any(start <= hour < end for (start, end) in sessions)

        if not in_session:
            session_str = ", ".join(f"{s:02d}:00–{e:02d}:00" for s, e in sessions)
            return False, (
                f"Off-peak hours ({hour:02d}:00 UTC) — "
                f"{'pair ' + pair + ' ' if pair else ''}executes only during {session_str} UTC"
            )

        session_names = []
        for start, end in sessions:
            if start <= hour < end:
                if start < 8:
                    session_names.append("Tokyo/Sydney")
                elif start < 13:
                    session_names.append("London")
                else:
                    session_names.append("London/NY overlap")
        return True, f"Active session: {', '.join(session_names) or 'open'}"

    def check_friday_cutoff(self, cutoff_hour_utc=21):
        """Block new trades after Friday 21:00 UTC to avoid weekend gap risk."""
        now = datetime.now(timezone.utc)
        if now.weekday() == 4 and now.hour >= cutoff_hour_utc:  # Friday
            return False, f"Friday close-out: no new trades after {cutoff_hour_utc}:00 UTC (weekend gap risk)"
        return True, "OK"

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
        """Rolling 24-hour drawdown window instead of calendar day reset."""
        state = self.get_account_state()

        # Use rolling 24h: get account snapshot from 24h ago if available
        from data.store import get_account_snapshot_24h_ago
        snapshot = get_account_snapshot_24h_ago()

        if snapshot:
            baseline = snapshot["balance"]
        else:
            # Fall back to session baseline
            if self._daily_start_balance is None:
                self._daily_start_balance = state["balance"]
            baseline = self._daily_start_balance

        loss_pct = ((baseline - state["nav"]) / baseline) * 100

        if loss_pct >= max_daily_loss_pct:
            return False, f"Kill switch: {loss_pct:.2f}% drawdown in rolling 24h >= {max_daily_loss_pct}% limit"
        return True, f"Drawdown OK: {loss_pct:.2f}% of {max_daily_loss_pct}% limit (rolling 24h)"

    def calculate_units(self, pair, stop_pips, max_risk_pct, balance=None):
        if balance is None:
            balance = self.get_account_state()["balance"]

        # Kelly adjustment from rolling trade performance
        kelly_fraction = self._get_kelly_fraction(max_risk_pct)
        risk_amount = balance * (kelly_fraction / 100)

        pip = PIP_SIZE.get(pair, 0.0001)
        pip_value_per_unit = pip

        if pair.startswith("USD_"):
            try:
                prices = self.client.get_live_price([pair])
                mid = (float(prices[0]["bids"][0]["price"]) + float(prices[0]["asks"][0]["price"])) / 2
                pip_value_per_unit = pip / mid
            except Exception:
                pass

        units = int(risk_amount / (stop_pips * pip_value_per_unit))
        return min(units, 100_000)

    def _get_kelly_fraction(self, base_risk_pct):
        """
        Half-Kelly position sizing based on rolling 50-trade performance.
        Falls back to base_risk_pct if insufficient data.
        Kelly fraction = W - (1-W)/R  where W=win_rate, R=avg_win/avg_loss ratio
        Half-Kelly = Kelly / 2 (for safety)
        Capped at 2× base_risk_pct, floored at 0.25× base_risk_pct.
        """
        try:
            from data.store import get_rolling_performance
            perf = get_rolling_performance(n=50)
            if not perf or perf["total"] < 20:
                return base_risk_pct

            win_rate = perf["win_rate"]
            avg_win  = perf["avg_win"]
            avg_loss = abs(perf["avg_loss"])

            if avg_loss == 0 or avg_win == 0:
                return base_risk_pct

            R = avg_win / avg_loss
            kelly = win_rate - (1 - win_rate) / R
            half_kelly_pct = (kelly / 2) * 100

            # Cap and floor
            result = max(base_risk_pct * 0.25, min(base_risk_pct * 2.0, half_kelly_pct))
            return round(result, 3)
        except Exception:
            return base_risk_pct

    def check_slippage_rr(self, pair, stop_pips, take_profit_ratio, min_rr=1.0):
        """
        Verify R:R remains above min_rr after subtracting estimated slippage on
        both entry and stop trigger sides. Default estimates: majors 1.5 pips,
        crosses/minors 2.5 pips.
        """
        slippage = SLIPPAGE_PIPS.get(pair, DEFAULT_SLIPPAGE)
        total_slippage = slippage * 2  # entry slip + stop-trigger slip
        adjusted_stop  = stop_pips + total_slippage
        adjusted_tp    = stop_pips * take_profit_ratio - total_slippage
        if adjusted_stop <= 0:
            return False, f"Stop too small for slippage model ({stop_pips} pips)"
        adjusted_rr = adjusted_tp / adjusted_stop
        if adjusted_rr < min_rr:
            return False, (
                f"R:R after slippage too low: {adjusted_rr:.2f} < {min_rr:.1f} "
                f"(slippage ~{slippage} pips/side)"
            )
        return True, f"Slippage-adjusted R:R OK: {adjusted_rr:.2f}"

    def check_usd_exposure(self, pair, direction, max_net_usd_positions=3):
        """
        Track net USD directional exposure across all open trades.
        Long USD (+1): BUY USD_XXX or SELL XXX_USD.
        Short USD (-1): SELL USD_XXX or BUY XXX_USD.
        Blocks if adding this trade pushes |net| beyond max_net_usd_positions.
        """
        def _usd_bias(p, d):
            try:
                base, quote = p.split("_")
            except ValueError:
                return 0
            if base == "USD":
                return 1 if d == "BUY" else -1
            if quote == "USD":
                return -1 if d == "BUY" else 1
            return 0

        open_trades = get_open_trades()
        net = sum(_usd_bias(t[3], t[4]) for t in open_trades)
        new_bias = _usd_bias(pair, direction)
        if new_bias != 0 and abs(net + new_bias) > max_net_usd_positions:
            side = "long" if (net + new_bias) > 0 else "short"
            return False, (
                f"USD exposure limit: {abs(net + new_bias)} net {side}-USD positions "
                f"would exceed {max_net_usd_positions} limit"
            )
        return True, "USD exposure OK"

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

    def calculate_atr_stop_pips(self, pair, thesis, multiplier=1.5):
        """
        Calculate a stop distance in pips based on ATR from H4 (falls back to H1).
        Returns rounded pip value, or None if ATR data is unavailable.
        """
        try:
            timeframes = thesis.get("timeframes", {})
            atr_value = (
                timeframes.get("H4", {}).get("atr") or
                timeframes.get("H1", {}).get("atr")
            )
            if atr_value is None:
                return None
            pip_size = PIP_SIZE.get(pair, 0.0001)
            atr_pips = atr_value / pip_size
            return round(atr_pips * multiplier, 1)
        except Exception:
            return None

    def get_portfolio_heat(self):
        """
        Dynamic heat: calculates current risk based on live price vs stop,
        not entry price vs stop.
        """
        from data.store import get_open_trades
        open_trades = get_open_trades()
        if not open_trades:
            return 0.0

        try:
            state = self.get_account_state()
            balance = state["balance"]
        except Exception:
            return 0.0

        total_heat = 0.0
        for trade in open_trades:
            try:
                # trade columns: id, opened, closed, pair, direction, units, open_price,
                #                close_price, pnl, status, signal_id, is_test, account_id, sl_price, tp_price
                pair      = trade[3]
                direction = trade[4]
                units     = abs(trade[5])
                sl_price  = trade[13]
                pip       = PIP_SIZE.get(pair, 0.0001)

                if sl_price:
                    # Get current live price
                    prices = self.client.get_live_price([pair])
                    mid = (float(prices[0]["bids"][0]["price"]) + float(prices[0]["asks"][0]["price"])) / 2
                    # Distance from current price to stop
                    if direction == "BUY":
                        risk_pips = (mid - sl_price) / pip
                    else:
                        risk_pips = (sl_price - mid) / pip
                    risk_pips = max(risk_pips, 1)  # floor at 1 pip
                else:
                    risk_pips = DEFAULT_PARAMS["stop_pips"]

                risk_amount = risk_pips * pip * units
                heat_pct = (risk_amount / balance) * 100
                total_heat += heat_pct
            except Exception:
                total_heat += DEFAULT_PARAMS["max_risk_pct"]  # assume worst case

        return round(total_heat, 2)

    def approve(self, pair, thesis, params=None, fundamentals=None):
        """
        Run all checks. Returns (approved, reason, units).
        Pass a params dict to override defaults (Test Mode).
        Pass fundamentals dict to enable news blackout check.
        """
        p = {**DEFAULT_PARAMS, **(params or {})}
        direction = thesis.get("direction")

        if direction == "NO_TRADE":
            return False, "Claude recommended NO_TRADE", 0
        if direction == "ERROR":
            return False, "Analysis error — skipping", 0

        # 0. News blackout check
        if fundamentals is not None:
            blackout_info = fundamentals.get("news_blackout", (False, None, None))
            if blackout_info and blackout_info[0]:
                event_name = blackout_info[1]
                mins = blackout_info[2]
                return False, f"News blackout: {event_name} in {mins} min", 0

        # 1. Market hours
        ok, msg = self.check_market_hours(bypass=p["bypass_hours"], pair=pair)
        if not ok:
            return False, msg, 0

        # 1.5 Friday close-out
        if not p["bypass_hours"]:
            ok, msg = self.check_friday_cutoff()
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

        # 4.5 USD directional exposure
        ok, msg = self.check_usd_exposure(
            pair, direction,
            max_net_usd_positions=p.get("max_usd_exposure_positions", 3)
        )
        if not ok:
            return False, msg, 0

        # 5. Drawdown kill switch
        ok, msg = self.check_drawdown(p["max_daily_loss_pct"])
        if not ok:
            return False, msg, 0

        # 6. Portfolio heat check
        heat = self.get_portfolio_heat()
        max_heat = p.get("max_positions", 3) * p["max_risk_pct"]  # e.g. 3 × 1% = 3% max
        if heat + p["max_risk_pct"] > max_heat * 1.2:
            return False, f"Portfolio heat too high: {heat:.1f}% active risk", 0

        # 6.5 Slippage-adjusted R:R check
        # ATR stop may override stop_pips below, so check with current p["stop_pips"] first
        ok, msg = self.check_slippage_rr(
            pair,
            p["stop_pips"],
            p.get("take_profit_ratio", 2.0),
            min_rr=p.get("min_rr_after_slippage", 1.0)
        )
        if not ok:
            return False, msg, 0

        # ATR-based stop override
        reason_suffix = ""
        if p.get("use_atr_stops"):
            atr_stop = self.calculate_atr_stop_pips(pair, thesis, multiplier=p.get("atr_multiplier", 1.5))
            if atr_stop is not None:
                p["stop_pips"] = atr_stop
                reason_suffix = f" | ATR stop: {atr_stop} pips"

        # Position sizing
        state = self.get_account_state()
        units = self.calculate_units(pair, p["stop_pips"], p["max_risk_pct"], balance=state["balance"])

        if direction == "SELL":
            units = -units

        return True, f"All checks passed{reason_suffix}", units
