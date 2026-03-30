"""
Price feed — fetches and formats candle data for all pairs and timeframes.
This is what the analysis engine reads from.
"""
import pandas as pd
from data.oanda_client import OandaClient
from config import PAIRS, TIMEFRAMES


class PriceFeed:
    def __init__(self):
        self.client = OandaClient()

    def get_candles(self, pair, timeframe, count=200):
        """
        Returns a DataFrame with columns: time, open, high, low, close, volume
        Sorted oldest to newest.
        """
        raw = self.client.get_candles(pair, timeframe, count)

        rows = []
        for c in raw:
            if c["complete"]:  # skip the still-forming current candle
                rows.append({
                    "time":   c["time"],
                    "open":   float(c["mid"]["o"]),
                    "high":   float(c["mid"]["h"]),
                    "low":    float(c["mid"]["l"]),
                    "close":  float(c["mid"]["c"]),
                    "volume": int(c["volume"])
                })

        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        return df

    def get_all(self, count=200):
        """
        Fetch candles for every pair + timeframe combination.
        Returns a dict: { "EUR_USD": { "H1": df, "H4": df, "D": df }, ... }
        """
        data = {}
        for pair in PAIRS:
            data[pair] = {}
            for tf in TIMEFRAMES:
                try:
                    df = self.get_candles(pair, tf, count)
                    data[pair][tf] = df
                    print(f"[OK] {pair} {tf}: {len(df)} candles")
                except Exception as e:
                    print(f"[ERROR] {pair} {tf}: {e}")
                    data[pair][tf] = None
        return data

    def get_live_prices(self):
        """Returns current bid/ask prices for all configured pairs."""
        raw = self.client.get_live_price(PAIRS)
        prices = {}
        for p in raw:
            prices[p["instrument"]] = {
                "bid": float(p["bids"][0]["price"]),
                "ask": float(p["asks"][0]["price"]),
                "spread": round(float(p["asks"][0]["price"]) - float(p["bids"][0]["price"]), 5)
            }
        return prices
