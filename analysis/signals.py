"""
Signal interpreter — produces a confluence-scored signal dict from indicator data.

Scoring system: -1.0 (strong sell) to +1.0 (strong buy)
A signal is only actionable when |score| >= 0.6.

Multi-timeframe rules:
  - Daily sets the bias (long/short only allowed in that direction)
  - H4 provides the setup
  - H1 provides timing confirmation
  - Counter-trend trades against Daily bias are blocked

Regime rules:
  - ADX >= 25 (trending): use EMA/MACD as primary signals
  - ADX < 25 (ranging):   use RSI/Stochastic only; EMA/MACD signals ignored
"""

from analysis.indicators import run_all

# Pairs that move together — flag as correlated exposure
CORRELATED_PAIRS = [
    {"EUR_USD", "GBP_USD"},
    {"AUD_USD", "NZD_USD"},
]


def score_candle(df):
    """
    Score a single enriched DataFrame (one pair, one timeframe).
    Returns a dict with the confluence score and all supporting values.
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    close     = last["close"]
    ema20     = last["ema_20"]
    ema50     = last["ema_50"]
    ema200    = last["ema_200"]
    adx       = last["adx"]
    plus_di   = last["plus_di"]
    minus_di  = last["minus_di"]
    rsi       = last["rsi"]
    stoch_k   = last["stoch_k"]
    stoch_d   = last["stoch_d"]
    macd      = last["macd"]
    macd_sig  = last["macd_signal"]
    macd_hist = last["macd_hist"]

    regime = "trending" if adx >= 25 else "ranging"
    score = 0.0
    factors = []

    if regime == "trending":
        # EMA alignment (0.0 to ±0.25)
        if close > ema20 > ema50 > ema200:
            score += 0.25; factors.append("strong uptrend (EMA stack)")
        elif close > ema50 > ema200:
            score += 0.15; factors.append("uptrend (price above EMA50/200)")
        elif close < ema20 < ema50 < ema200:
            score -= 0.25; factors.append("strong downtrend (EMA stack)")
        elif close < ema50 < ema200:
            score -= 0.15; factors.append("downtrend (price below EMA50/200)")

        # Price vs EMA200 (bias filter, ±0.10)
        if close > ema200:
            score += 0.10; factors.append("price above EMA200")
        else:
            score -= 0.10; factors.append("price below EMA200")

        # MACD (0.0 to ±0.25)
        if macd > macd_sig and prev["macd"] <= prev["macd_signal"]:
            score += 0.25; factors.append("MACD bullish crossover")
        elif macd < macd_sig and prev["macd"] >= prev["macd_signal"]:
            score -= 0.25; factors.append("MACD bearish crossover")
        elif macd > macd_sig:
            score += 0.12; factors.append("MACD bullish")
        else:
            score -= 0.12; factors.append("MACD bearish")

        # ADX directional bias (±0.10)
        if plus_di > minus_di:
            score += 0.10; factors.append("+DI > -DI (bullish pressure)")
        else:
            score -= 0.10; factors.append("-DI > +DI (bearish pressure)")

    else:
        # Ranging regime — only use RSI + Stochastic
        factors.append(f"ranging market (ADX {adx:.1f} < 25) — trend indicators ignored")

    # RSI + Stochastic weighted together as ONE signal (±0.20)
    rsi_bull   = rsi <= 35
    rsi_bear   = rsi >= 65
    stoch_bull = stoch_k < 25 and stoch_d < 25
    stoch_bear = stoch_k > 75 and stoch_d > 75

    if rsi_bull and stoch_bull:
        score += 0.20; factors.append(f"RSI+Stoch oversold ({rsi:.0f}/{stoch_k:.0f})")
    elif rsi_bear and stoch_bear:
        score -= 0.20; factors.append(f"RSI+Stoch overbought ({rsi:.0f}/{stoch_k:.0f})")
    elif rsi_bull or stoch_bull:
        score += 0.10; factors.append(f"partial oversold signal (RSI {rsi:.0f}, Stoch {stoch_k:.0f})")
    elif rsi_bear or stoch_bear:
        score -= 0.10; factors.append(f"partial overbought signal (RSI {rsi:.0f}, Stoch {stoch_k:.0f})")

    # Support/Resistance proximity (±0.10)
    support    = last["support"]
    resistance = last["resistance"]

    if support and abs(close - support) / close < 0.003:
        score += 0.10; factors.append(f"near support ({support:.5f})")
    if resistance and abs(close - resistance) / close < 0.003:
        score -= 0.10; factors.append(f"near resistance ({resistance:.5f})")

    score = round(max(-1.0, min(1.0, score)), 3)

    return {
        "score":      score,
        "regime":     regime,
        "adx":        round(adx, 1),
        "rsi":        round(rsi, 1),
        "stoch_k":    round(stoch_k, 1),
        "stoch_d":    round(stoch_d, 1),
        "macd":       round(macd, 5),
        "ema_20":     round(ema20, 5),
        "ema_50":     round(ema50, 5),
        "ema_200":    round(ema200, 5),
        "current_price": round(close, 5),
        "support":    round(support, 5) if support else None,
        "resistance": round(resistance, 5) if resistance else None,
        "atr":        round(last["atr"], 5),
        "factors":    factors,
    }


def check_mtf_alignment(daily_score, h4_score):
    """
    Daily sets the bias. H4 must agree with it.
    Returns (aligned: bool, reason: str).
    """
    if daily_score >= 0.3 and h4_score < 0:
        return False, f"H4 bearish ({h4_score}) conflicts with Daily bullish bias ({daily_score})"
    if daily_score <= -0.3 and h4_score > 0:
        return False, f"H4 bullish ({h4_score}) conflicts with Daily bearish bias ({daily_score})"
    return True, "MTF aligned"


def check_correlation(pair, all_signals):
    """
    Flag if a correlated pair also has the same directional signal.
    Returns a warning string or None.
    """
    for group in CORRELATED_PAIRS:
        if pair not in group:
            continue
        sister = (group - {pair}).pop()
        if sister not in all_signals:
            continue
        sister_score = all_signals[sister].get("h4", {}).get("score", 0)
        this_score   = all_signals[pair].get("h4", {}).get("score", 0)
        if (this_score > 0.3 and sister_score > 0.3) or (this_score < -0.3 and sister_score < -0.3):
            return f"Correlated exposure warning: {pair} and {sister} both signal the same direction — this is one macro bet, not two independent trades"
    return None


def analyze_pair(feed, pair, timeframes=["H1", "H4", "D"]):
    """
    Run full multi-timeframe technical analysis on a pair.
    Returns a structured dict ready to pass to Claude.
    """
    raw = {}
    for tf in timeframes:
        df = feed.get_candles(pair, tf, count=200)
        enriched = run_all(df)
        raw[tf] = score_candle(enriched)

    daily = raw.get("D", {})
    h4    = raw.get("H4", {})
    h1    = raw.get("H1", {})

    daily_score = daily.get("score", 0)
    h4_score    = h4.get("score", 0)
    h1_score    = h1.get("score", 0)

    mtf_ok, mtf_reason = check_mtf_alignment(daily_score, h4_score)

    # H1 confirmation: same direction as H4?
    h1_confirms = (h4_score > 0 and h1_score > 0) or (h4_score < 0 and h1_score < 0)

    # Final actionable score: H4 is primary, dampen if MTF conflicts or H1 doesn't confirm
    final_score = h4_score
    if not mtf_ok:
        final_score = 0.0  # blocked by Daily bias
    elif not h1_confirms:
        final_score = round(h4_score * 0.7, 3)  # reduce but don't block

    if final_score >= 0.6:
        direction = "BUY"
    elif final_score <= -0.6:
        direction = "SELL"
    else:
        direction = "NO_TRADE"

    return {
        "pair":         pair,
        "direction":    direction,
        "final_score":  final_score,
        "mtf_aligned":  mtf_ok,
        "mtf_reason":   mtf_reason,
        "h1_confirms":  h1_confirms,
        "timeframes": {
            "D":  daily,
            "H4": h4,
            "H1": h1,
        },
    }
