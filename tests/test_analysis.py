"""
Test the technical analysis engine against live OANDA data.
Run with: python -m tests.test_analysis
"""
import sys, json
sys.path.insert(0, ".")

from data.price_feed import PriceFeed
from analysis.signals import analyze_pair


if __name__ == "__main__":
    feed = PriceFeed()

    print("\n=== Technical Analysis: EUR/USD ===")
    results = analyze_pair(feed, "EUR_USD")

    for tf, summary in results.items():
        print(f"\n--- {tf} ---")
        for k, v in summary.items():
            print(f"  {k:<25} {v}")

    print("\n[DONE]")
