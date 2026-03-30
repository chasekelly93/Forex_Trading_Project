"""
Quick test — verifies OANDA connection, candle fetch, and DB init.
Run with: python -m tests.test_pipeline
"""
import sys
sys.path.insert(0, ".")

from data.oanda_client import OandaClient
from data.price_feed import PriceFeed
from data.store import init_db, save_signal, get_recent_signals


def test_account():
    print("\n--- Account ---")
    client = OandaClient()
    account = client.get_account()
    print(f"Balance:    {account['balance']}")
    print(f"Currency:   {account['currency']}")
    print(f"Open P&L:   {account['unrealizedPL']}")


def test_candles():
    print("\n--- Candles (EUR/USD H4, last 5) ---")
    feed = PriceFeed()
    df = feed.get_candles("EUR_USD", "H4", count=5)
    print(df.to_string())


def test_live_prices():
    print("\n--- Live Prices ---")
    feed = PriceFeed()
    prices = feed.get_live_prices()
    for pair, p in prices.items():
        print(f"{pair}: bid={p['bid']}  ask={p['ask']}  spread={p['spread']}")


def test_db():
    print("\n--- Database ---")
    init_db()
    save_signal("EUR_USD", "H4", "BUY", 0.75, "Test signal from pipeline test")
    signals = get_recent_signals(1)
    print(f"Saved and retrieved signal: {signals[0]}")


if __name__ == "__main__":
    test_account()
    test_candles()
    test_live_prices()
    test_db()
    print("\n[ALL TESTS PASSED]")
