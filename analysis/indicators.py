"""
Technical indicators — all functions accept a DataFrame and return it enriched.
Written for backtesting compatibility: no hardcoded "current price" logic.
"""
import pandas as pd
import numpy as np


def add_ema(df, periods=[20, 50, 200]):
    for p in periods:
        df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def add_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


def add_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def add_stochastic(df, k_period=14, d_period=3):
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    df["stoch_k"] = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    df["stoch_d"] = df["stoch_k"].rolling(window=d_period).mean()
    return df


def add_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(com=period - 1, adjust=False).mean()
    return df


def add_adx(df, period=14):
    """
    Average Directional Index — measures trend STRENGTH, not direction.
    ADX > 25: trending market (use trend-following signals)
    ADX < 25: ranging market (avoid trend signals, prefer mean-reversion)
    Also adds +DI and -DI for directional bias within the trend.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    high_low = high - low
    high_close = (high - close.shift()).abs()
    low_close = (low - close.shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

    atr = tr.ewm(com=period - 1, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"]      = dx.ewm(com=period - 1, adjust=False).mean()
    df["plus_di"]  = plus_di
    df["minus_di"] = minus_di
    return df


def add_support_resistance(df, lookback=50, tolerance_pct=0.002):
    """Detect swing high/low levels nearest to current price."""
    recent = df.tail(lookback)
    close = df["close"].iloc[-1]

    highs, lows = [], []
    hp = recent["high"].values
    lp = recent["low"].values

    for i in range(2, len(hp) - 2):
        if hp[i] > hp[i-1] and hp[i] > hp[i-2] and hp[i] > hp[i+1] and hp[i] > hp[i+2]:
            highs.append(hp[i])
    for i in range(2, len(lp) - 2):
        if lp[i] < lp[i-1] and lp[i] < lp[i-2] and lp[i] < lp[i+1] and lp[i] < lp[i+2]:
            lows.append(lp[i])

    resistances = [h for h in highs if h > close * (1 + tolerance_pct)]
    supports    = [l for l in lows  if l < close * (1 - tolerance_pct)]

    df["resistance"] = min(resistances) if resistances else None
    df["support"]    = max(supports)    if supports    else None
    return df


def detect_regime(df):
    """
    Classify the last candle's market regime using ADX.
    Returns 'trending' or 'ranging'.
    Requires add_adx() to have been called first.
    """
    adx = df["adx"].iloc[-1]
    return "trending" if adx >= 25 else "ranging"


def run_all(df):
    """Apply every indicator. Returns enriched DataFrame."""
    df = df.copy()
    df = add_ema(df)
    df = add_macd(df)
    df = add_rsi(df)
    df = add_stochastic(df)
    df = add_atr(df)
    df = add_adx(df)
    df = add_support_resistance(df)
    return df
