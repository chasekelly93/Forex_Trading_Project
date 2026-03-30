"""
Forex Trading Agent — Entry Point
Runs the full analysis + execution loop on a schedule.
"""
import sys
import time
import schedule
from datetime import datetime

from config import OANDA_API_KEY, OANDA_ACCOUNT_ID, ANTHROPIC_API_KEY, PAIRS
from data.store import init_db
from agent.claude_agent import analyze
from execution.executor import Executor

executor = Executor()


def check_config():
    missing = []
    if not OANDA_API_KEY or OANDA_API_KEY == "your_oanda_api_key_here":
        missing.append("OANDA_API_KEY")
    if not OANDA_ACCOUNT_ID or OANDA_ACCOUNT_ID == "your_oanda_account_id_here":
        missing.append("OANDA_ACCOUNT_ID")
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "your_anthropic_api_key_here":
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        print(f"[ERROR] Missing credentials in .env: {', '.join(missing)}")
        sys.exit(1)
    print("[OK] Config loaded.")


def run_cycle():
    """One full analysis + execution cycle across all pairs."""
    print(f"\n{'='*50}")
    print(f"[CYCLE] {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*50}")

    # Snapshot account state at the start of each cycle
    state = executor.snapshot_account()
    print(f"[ACCOUNT] Balance: ${state['balance']:,.2f} | NAV: ${state['nav']:,.2f} | Open P&L: ${state['open_pnl']:,.2f}")

    for pair in PAIRS:
        try:
            thesis = analyze(pair)
            direction = thesis.get("direction")
            confidence = thesis.get("confidence", 0)

            print(f"\n[{pair}] {direction} @ {confidence:.0%} confidence")
            print(f"  Reasoning: {thesis.get('reasoning', '')[:120]}...")

            if direction not in ("NO_TRADE", "ERROR"):
                executor.execute(thesis)

        except Exception as e:
            print(f"[ERROR] {pair}: {e}")

    print(f"\n[CYCLE COMPLETE]")


if __name__ == "__main__":
    check_config()
    init_db()

    args = sys.argv[1:]

    if "once" in args:
        # Run a single cycle and exit — useful for testing
        run_cycle()

    elif "close-all" in args:
        # Emergency: close every open position
        print("[EMERGENCY CLOSE] Closing all positions...")
        results = executor.close_all_positions()
        for r in results:
            print(f"  {r}")

    else:
        # Normal mode: run on H4 candle close (every 4 hours)
        print("[AGENT] Starting. First cycle in 5 seconds...")
        print("[AGENT] Scheduled to run every 4 hours. Press Ctrl+C to stop.\n")

        schedule.every(4).hours.do(run_cycle)

        # Run immediately on start
        time.sleep(5)
        run_cycle()

        while True:
            schedule.run_pending()
            time.sleep(60)
