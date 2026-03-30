"""
Technical indicators — calculated on top of raw OHLCV DataFrames.
All functions take a DataFrame and return it with new columns added.
"""
import pandas as pd
import numpy as np


def add_ema(df, periods=[20, 50, 200]):
    """Exponential Moving Averages."""
    for p in periods:
        df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def add_macd(df, fast=12, slow=26, signal=9):
    """MACD line, signal line, and histogram."""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


def add_rsi(df, period=14):
    """Relative Strength Index."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def add_stochastic(df, k_period=14, d_period=3):
    """Stochastic Oscillator %K and %D."""
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    df["stoch_k"] = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    df["stoch_d"] = df["stoch_k"].rolling(window=d_period).mean()
    return df


def add_atr(df, period=14):
    """Average True Range — measures volatility."""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(com=period - 1, adjust=False).mean()
    return df


def add_support_resistance(df, lookback=50, tolerance_pct=0.002):
    """
    Identifies key support and resistance levels from recent swing highs/lows.
    Returns the DataFrame unchanged but adds 'support' and 'resistance' columns
    with the nearest level below/above current price.
    """
    recent = df.tail(lookback)
    close = df["close"].iloc[-1]

    # Swing highs: candle high is higher than the two candles on each side
    highs = []
    lows = []
    prices = recent["high"].values
    for i in range(2, len(prices) - 2):
        if prices[i] > prices[i-1] and prices[i] > prices[i-2] and \
           prices[i] > prices[i+1] and prices[i] > prices[i+2]:
            highs.append(prices[i])

    prices = recent["low"].values
    for i in range(2, len(prices) - 2):
        if prices[i] < prices[i-1] and prices[i] < prices[i-2] and \
           prices[i] < prices[i+1] and prices[i] < prices[i+2]:
            lows.append(prices[i])

    # Nearest resistance above price
    resistances = [h for h in highs if h > close * (1 + tolerance_pct)]
    # Nearest support below price
    supports = [l for l in lows if l < close * (1 - tolerance_pct)]

    df["resistance"] = min(resistances) if resistances else None
    df["support"] = max(supports) if supports else None
    return df


def run_all(df):
    """Apply every indicator to a DataFrame. Returns enriched DataFrame."""
    df = df.copy()
    df = add_ema(df)
    df = add_macd(df)
    df = add_rsi(df)
    df = add_stochastic(df)
    df = add_atr(df)
    df = add_support_resistance(df)
    return df
