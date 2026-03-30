"""
Signal interpreter — reads indicator values and produces a structured
summary for each pair/timeframe. This is what gets fed to Claude.
"""
import pandas as pd
from analysis.indicators import run_all


def interpret(df):
    """
    Given an enriched DataFrame (after run_all), return a plain-English
    summary dict describing current market conditions.
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    summary = {}

    # --- Trend ---
    close = last["close"]
    ema20, ema50, ema200 = last["ema_20"], last["ema_50"], last["ema_200"]

    if close > ema20 > ema50 > ema200:
        summary["trend"] = "strong_uptrend"
    elif close > ema50 > ema200:
        summary["trend"] = "uptrend"
    elif close < ema20 < ema50 < ema200:
        summary["trend"] = "strong_downtrend"
    elif close < ema50 < ema200:
        summary["trend"] = "downtrend"
    else:
        summary["trend"] = "ranging"

    summary["price_vs_ema200"] = "above" if close > ema200 else "below"

    # --- MACD ---
    if last["macd"] > last["macd_signal"] and prev["macd"] <= prev["macd_signal"]:
        summary["macd"] = "bullish_crossover"
    elif last["macd"] < last["macd_signal"] and prev["macd"] >= prev["macd_signal"]:
        summary["macd"] = "bearish_crossover"
    elif last["macd"] > last["macd_signal"]:
        summary["macd"] = "bullish"
    else:
        summary["macd"] = "bearish"

    summary["macd_hist_direction"] = "rising" if last["macd_hist"] > prev["macd_hist"] else "falling"

    # --- RSI ---
    rsi = last["rsi"]
    summary["rsi"] = round(rsi, 1)
    if rsi >= 70:
        summary["rsi_condition"] = "overbought"
    elif rsi <= 30:
        summary["rsi_condition"] = "oversold"
    elif rsi >= 60:
        summary["rsi_condition"] = "bullish_momentum"
    elif rsi <= 40:
        summary["rsi_condition"] = "bearish_momentum"
    else:
        summary["rsi_condition"] = "neutral"

    # --- Stochastic ---
    k, d = last["stoch_k"], last["stoch_d"]
    summary["stoch_k"] = round(k, 1)
    summary["stoch_d"] = round(d, 1)
    if k > 80 and d > 80:
        summary["stochastic"] = "overbought"
    elif k < 20 and d < 20:
        summary["stochastic"] = "oversold"
    elif k > d:
        summary["stochastic"] = "bullish"
    else:
        summary["stochastic"] = "bearish"

    # --- Support / Resistance ---
    summary["support"] = round(last["support"], 5) if last["support"] else None
    summary["resistance"] = round(last["resistance"], 5) if last["resistance"] else None
    summary["current_price"] = round(close, 5)

    # --- ATR (volatility) ---
    summary["atr"] = round(last["atr"], 5)

    # --- Overall bias ---
    bull_signals = sum([
        summary["trend"] in ("uptrend", "strong_uptrend"),
        summary["macd"] in ("bullish", "bullish_crossover"),
        summary["rsi_condition"] in ("bullish_momentum",),
        summary["stochastic"] in ("bullish", "oversold"),
        summary["price_vs_ema200"] == "above"
    ])
    bear_signals = sum([
        summary["trend"] in ("downtrend", "strong_downtrend"),
        summary["macd"] in ("bearish", "bearish_crossover"),
        summary["rsi_condition"] in ("bearish_momentum",),
        summary["stochastic"] in ("bearish", "overbought"),
        summary["price_vs_ema200"] == "below"
    ])

    if bull_signals >= 4:
        summary["bias"] = "strong_buy"
    elif bull_signals >= 3:
        summary["bias"] = "buy"
    elif bear_signals >= 4:
        summary["bias"] = "strong_sell"
    elif bear_signals >= 3:
        summary["bias"] = "sell"
    else:
        summary["bias"] = "neutral"

    summary["bull_signals"] = bull_signals
    summary["bear_signals"] = bear_signals

    return summary


def analyze_pair(feed, pair, timeframes=["H1", "H4", "D"]):
    """
    Run full technical analysis on a pair across all timeframes.
    Returns a dict of { timeframe: summary_dict }
    """
    results = {}
    for tf in timeframes:
        df = feed.get_candles(pair, tf, count=200)
        enriched = run_all(df)
        results[tf] = interpret(enriched)
    return results
