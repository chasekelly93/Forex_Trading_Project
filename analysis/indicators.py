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


def add_volume_ratio(df, period=20):
    """Calculate tick volume ratio relative to rolling mean. NaN filled with 1.0."""
    df["volume_ratio"] = df["volume"] / df["volume"].rolling(period).mean()
    df["volume_ratio"] = df["volume_ratio"].fillna(1.0)
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


def add_pivot_points(df):
    """
    Calculate classic daily pivot points from a rolling 24-period H/L/C approximation.
    Adds columns: pivot, r1, r2, s1, s2.
    """
    try:
        h = df["high"].rolling(24).max()
        l = df["low"].rolling(24).min()
        c = df["close"].shift(1)  # previous close approximation

        pp = (h + l + c) / 3
        df["pivot"] = pp
        df["r1"] = 2 * pp - l
        df["r2"] = pp + (h - l)
        df["s1"] = 2 * pp - h
        df["s2"] = pp - (h - l)
    except Exception:
        for col in ("pivot", "r1", "r2", "s1", "s2"):
            df[col] = None
    return df


def add_round_number_proximity(df, pip_size=0.0001):
    """
    Detect if close is within 20 pips of a round number (every 0.0050 for normal pairs,
    every 0.50 for JPY pairs where pip_size=0.01).
    Adds: near_round_number (bool), round_number_distance_pips (float).
    """
    try:
        round_interval = 0.50 if pip_size >= 0.01 else 0.0050
        threshold_price = 20 * pip_size  # 20 pips in price units

        close = df["close"]
        # Nearest round number for each close
        nearest = (close / round_interval).round() * round_interval
        distance_price = (close - nearest).abs()
        distance_pips = distance_price / pip_size

        df["near_round_number"] = distance_price <= threshold_price
        df["round_number_distance_pips"] = distance_pips.round(1)
    except Exception:
        df["near_round_number"] = False
        df["round_number_distance_pips"] = None
    return df


def detect_market_structure(df, lookback=30):
    """
    Analyse the last `lookback` candles to determine trend structure.
    Uses a 5-bar swing detection: a bar is a swing high if its high is greater than
    the 2 bars before and after it; swing low vice versa.
    Classifies last 4 detected swing points as "uptrend", "downtrend", or "ranging".
    Adds column: market_structure.
    """
    try:
        recent = df.tail(lookback).copy().reset_index(drop=True)
        hp = recent["high"].values
        lp = recent["low"].values

        swing_highs = []  # (index, value)
        swing_lows  = []

        for i in range(2, len(hp) - 2):
            if hp[i] > hp[i-1] and hp[i] > hp[i-2] and hp[i] > hp[i+1] and hp[i] > hp[i+2]:
                swing_highs.append((i, hp[i]))
            if lp[i] < lp[i-1] and lp[i] < lp[i-2] and lp[i] < lp[i+1] and lp[i] < lp[i+2]:
                swing_lows.append((i, lp[i]))

        structure = "ranging"  # default

        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            last_highs = [v for _, v in swing_highs[-2:]]
            last_lows  = [v for _, v in swing_lows[-2:]]

            hh = last_highs[-1] > last_highs[-2]  # higher high
            hl = last_lows[-1]  > last_lows[-2]   # higher low
            lh = last_highs[-1] < last_highs[-2]  # lower high
            ll = last_lows[-1]  < last_lows[-2]   # lower low

            if hh and hl:
                structure = "uptrend"
            elif lh and ll:
                structure = "downtrend"

        df["market_structure"] = structure
    except Exception:
        df["market_structure"] = "ranging"
    return df


def add_candlestick_patterns(df):
    """
    Detect pin bar and engulfing patterns on the last 3 candles.
    Adds boolean columns: pattern_pin_bull, pattern_pin_bear,
                          pattern_engulf_bull, pattern_engulf_bear.
    """
    # Defaults
    for col in ("pattern_pin_bull", "pattern_pin_bear", "pattern_engulf_bull", "pattern_engulf_bear"):
        df[col] = False

    try:
        if len(df) < 3:
            return df

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # ── Pin bar detection (last candle) ──
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        total_range = h - l
        if total_range > 0:
            body      = abs(c - o)
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            body_pct  = body / total_range

            if body_pct < 0.30:
                # Bullish pin: lower wick dominant, close in upper 40%
                if lower_wick > 0.60 * total_range and (c - l) / total_range >= 0.60:
                    df.iloc[-1, df.columns.get_loc("pattern_pin_bull")] = True
                # Bearish pin: upper wick dominant, close in lower 40%
                elif upper_wick > 0.60 * total_range and (h - c) / total_range >= 0.60:
                    df.iloc[-1, df.columns.get_loc("pattern_pin_bear")] = True

        # ── Engulfing detection (last vs prev candle) ──
        po, pc = prev["open"], prev["close"]
        co, cc = last["open"], last["close"]

        prev_bearish  = pc < po
        prev_bullish  = pc > po
        curr_bullish  = cc > co
        curr_bearish  = cc < co

        if prev_bearish and curr_bullish:
            if co <= pc and cc >= po:  # current body engulfs previous
                df.iloc[-1, df.columns.get_loc("pattern_engulf_bull")] = True

        if prev_bullish and curr_bearish:
            if co >= pc and cc <= po:
                df.iloc[-1, df.columns.get_loc("pattern_engulf_bear")] = True

    except Exception:
        pass

    return df


def add_bollinger_bands(df, period=20, num_std=2):
    """
    Bollinger Bands around a 20-period SMA.
    Adds: bb_upper, bb_lower, bb_mid, bb_pct_b (position within bands).
    %B = 0 → at lower band, 1 → at upper band, >1 → extended above, <0 → extended below.
    """
    sma = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    df["bb_upper"] = sma + num_std * std
    df["bb_lower"] = sma - num_std * std
    df["bb_mid"]   = sma
    band_width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_pct_b"] = (df["close"] - df["bb_lower"]) / band_width
    df["bb_pct_b"] = df["bb_pct_b"].fillna(0.5)
    return df


def run_all(df):
    """Apply every indicator. Returns enriched DataFrame."""
    df = df.copy()
    df = add_ema(df)
    df = add_macd(df)
    df = add_rsi(df)
    df = add_stochastic(df)
    df = add_atr(df)
    df = add_volume_ratio(df)
    df = add_adx(df)
    df = add_support_resistance(df)
    df = add_pivot_points(df)
    df = add_round_number_proximity(df)
    df = detect_market_structure(df)
    df = add_candlestick_patterns(df)
    df = add_bollinger_bands(df)
    return df
