"""
Trade executor — takes an approved thesis and places the order via OANDA.
Never call this directly; always go through RiskEngine.approve() first.
"""
from data.oanda_client import OandaClient
from data.store import save_trade, close_trade, get_open_trades, save_snapshot
from execution.risk import RiskEngine, DEFAULT_PARAMS


class Executor:
    def __init__(self):
        self.client = OandaClient()
        self.risk = RiskEngine()

    def execute(self, thesis, params=None):
        """
        Main entry point. Runs risk checks, then places the trade if approved.
        Pass params dict to override risk defaults (Test Mode).
        Returns a result dict describing what happened.
        """
        pair = thesis["pair"]
        direction = thesis.get("direction")

        print(f"\n[EXECUTOR] {pair} | Direction: {direction} | Confidence: {thesis.get('confidence', 0):.0%}")

        approved, reason, units = self.risk.approve(pair, thesis, params=params)

        if not approved:
            print(f"[BLOCKED] {reason}")
            return {"status": "blocked", "reason": reason, "pair": pair}

        print(f"[APPROVED] {reason} | Units: {units:+,}")

        # Calculate SL/TP
        p = {**DEFAULT_PARAMS, **(params or {})}
        sl_price, tp_price, trailing_distance = None, None, None
        try:
            _, tp_price = self.risk.calculate_sl_tp(
                pair, direction, p["stop_pips"], p.get("take_profit_ratio", 2.0)
            )
            if p.get("trailing_stop"):
                trailing_distance = self.risk.calculate_trailing_distance(
                    pair, p.get("trailing_stop_pips", p["stop_pips"])
                )
                print(f"[SL/TP] Trailing: {trailing_distance} price units | TP: {tp_price}")
            else:
                sl_price, _ = self.risk.calculate_sl_tp(
                    pair, direction, p["stop_pips"], p.get("take_profit_ratio", 2.0)
                )
                print(f"[SL/TP] Fixed SL: {sl_price} | TP: {tp_price}")
        except Exception as e:
            print(f"[SL/TP ERROR] {e} — placing without SL/TP")

        # Place the order
        try:
            response = self.client.place_market_order(
                pair, units,
                sl_price=sl_price,
                tp_price=tp_price,
                trailing_distance=trailing_distance,
            )
            order_fill = response.get("orderFillTransaction", {})
            fill_price = float(order_fill.get("price", 0))
            trade_id = order_fill.get("tradeOpened", {}).get("tradeID")

            # Save to DB
            save_trade(
                pair=pair,
                direction=direction,
                units=abs(units),
                open_price=fill_price,
                sl_price=sl_price,
                tp_price=tp_price,
                is_test=bool(params and params.get("is_test", False)),
            )

            print(f"[FILLED] Trade ID: {trade_id} | Fill price: {fill_price}")
            return {
                "status": "filled",
                "pair": pair,
                "direction": direction,
                "units": units,
                "fill_price": fill_price,
                "trade_id": trade_id,
            }

        except Exception as e:
            import traceback
            print(f"[ORDER ERROR] {e}")
            traceback.print_exc()
            return {"status": "error", "pair": pair, "error": str(e)}

    def close_all_positions(self):
        """Emergency close — shuts every open position immediately."""
        positions = self.client.get_open_positions()
        results = []
        for pos in positions:
            pair = pos["instrument"]
            try:
                resp = self.client.close_position(pair)
                print(f"[CLOSED] {pair}")
                results.append({"pair": pair, "status": "closed"})
            except Exception as e:
                print(f"[CLOSE ERROR] {pair}: {e}")
                results.append({"pair": pair, "status": "error", "error": str(e)})
        return results

    def snapshot_account(self):
        """Save current account state to DB for dashboard history."""
        state = self.risk.get_account_state()
        save_snapshot(
            balance=state["balance"],
            nav=state["nav"],
            open_pnl=state["open_pnl"]
        )
        return state
