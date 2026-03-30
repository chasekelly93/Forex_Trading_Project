"""
Test the Claude analysis agent on a single pair.
Run with: python -m tests.test_agent
"""
import sys, json
sys.path.insert(0, ".")

from agent.claude_agent import analyze
from data.store import init_db, get_recent_signals

if __name__ == "__main__":
    init_db()

    print("\n=== Running Claude analysis on EUR/USD ===\n")
    thesis = analyze("EUR_USD")

    print("\n--- Trade Thesis ---")
    for k, v in thesis.items():
        print(f"  {k:<26} {v}")

    print("\n--- Last signal in DB ---")
    signals = get_recent_signals(1)
    print(f"  {signals[0]}")

    print("\n[DONE]")
