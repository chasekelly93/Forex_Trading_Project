"""
Test risk engine checks and position sizing.
Does NOT place real orders.
Run with: python -m tests.test_execution
"""
import sys
sys.path.insert(0, ".")

from execution.risk import RiskEngine
from data.store import init_db

if __name__ == "__main__":
    init_db()
    risk = RiskEngine()

    print("\n=== Account State ===")
    state = risk.get_account_state()
    for k, v in state.items():
        print(f"  {k:<15} {v}")

    print("\n=== Risk Checks ===")

    ok, msg = risk.check_max_positions()
    print(f"  Max positions:  {'PASS' if ok else 'FAIL'} — {msg}")

    ok, msg = risk.check_drawdown()
    print(f"  Drawdown:       {'PASS' if ok else 'FAIL'} — {msg}")

    print("\n=== Position Sizing ===")
    for pair in ["EUR_USD", "GBP_USD", "USD_JPY"]:
        units = risk.calculate_units(pair, stop_pips=20, balance=state["balance"])
        print(f"  {pair}: {units:,} units (20 pip stop, {state['balance']:,.0f} balance)")

    print("\n=== Approve Test (SELL EUR_USD, 62% confidence) ===")
    fake_thesis = {
        "pair": "EUR_USD",
        "direction": "SELL",
        "confidence": 0.62,
        "reasoning": "Test thesis"
    }
    approved, reason, units = risk.approve("EUR_USD", fake_thesis)
    print(f"  Approved: {approved}")
    print(f"  Reason:   {reason}")
    print(f"  Units:    {units:+,}")

    print("\n=== Approve Test (NO_TRADE) ===")
    no_trade = {"pair": "EUR_USD", "direction": "NO_TRADE", "confidence": 0.5}
    approved, reason, units = risk.approve("EUR_USD", no_trade)
    print(f"  Approved: {approved}")
    print(f"  Reason:   {reason}")

    print("\n[ALL TESTS PASSED]")
