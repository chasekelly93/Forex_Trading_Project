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


def score_candle(df, adx_threshold=25):
    """
    Score a single enriched DataFrame (one pair, one timeframe).
    Returns a dict with the confluence score and all supporting values.
    adx_threshold: ADX value required for "trending" regime (default 25).
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

    regime = "trending" if adx >= adx_threshold else "ranging"
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

    # RSI signal (±0.20)
    if rsi <= 35:
        score += 0.20; factors.append(f"RSI oversold ({rsi:.0f})")
    elif rsi >= 65:
        score -= 0.20; factors.append(f"RSI overbought ({rsi:.0f})")
    elif rsi <= 45:
        score += 0.10; factors.append(f"RSI mildly oversold ({rsi:.0f})")
    elif rsi >= 55:
        score -= 0.10; factors.append(f"RSI mildly overbought ({rsi:.0f})")

    # Support/Resistance proximity (±0.10)
    support    = last["support"]
    resistance = last["resistance"]

    if support and abs(close - support) / close < 0.003:
        score += 0.10; factors.append(f"near support ({support:.5f})")
    if resistance and abs(close - resistance) / close < 0.003:
        score -= 0.10; factors.append(f"near resistance ({resistance:.5f})")

    # ── Pivot point proximity (±0.08) ──────────────────────────────────────
    try:
        pivot = last.get("pivot")
        r1    = last.get("r1")
        r2    = last.get("r2")
        s1    = last.get("s1")
        s2    = last.get("s2")

        def _near(level):
            return level is not None and not (isinstance(level, float) and level != level) \
                   and abs(close - level) / close < 0.003

        if _near(s1) or _near(s2):
            score += 0.08; factors.append("near pivot support (S1/S2)")
        if _near(r1) or _near(r2):
            score -= 0.08; factors.append("near pivot resistance (R1/R2)")
        if _near(pivot) and not _near(s1) and not _near(s2) and not _near(r1) and not _near(r2):
            factors.append("at pivot point")
    except Exception:
        pivot = r1 = r2 = s1 = s2 = None

    # ── Round number proximity (±0.06) ──────────────────────────────────────
    try:
        near_round = last.get("near_round_number", False)
        dist_pips  = last.get("round_number_distance_pips", 0) or 0
        if near_round:
            bump = 0.06 if score > 0 else -0.06
            score += bump
            factors.append(f"near round number ({dist_pips:.1f} pips away)")
    except Exception:
        near_round = False
        dist_pips  = 0.0

    # ── Market structure alignment (±0.10) ──────────────────────────────────
    try:
        mkt_structure = last.get("market_structure", "ranging")
        if mkt_structure == "uptrend" and score > 0:
            score += 0.10; factors.append("market structure: uptrend confirms")
        elif mkt_structure == "downtrend" and score < 0:
            score += 0.10; factors.append("market structure: downtrend confirms")
        elif mkt_structure == "uptrend" and score < 0:
            score *= 0.8; factors.append("market structure conflict")
        elif mkt_structure == "downtrend" and score > 0:
            score *= 0.8; factors.append("market structure conflict")
    except Exception:
        mkt_structure = "ranging"

    # ── Candlestick patterns (±0.12) ────────────────────────────────────────
    try:
        pin_bull     = bool(last.get("pattern_pin_bull", False))
        pin_bear     = bool(last.get("pattern_pin_bear", False))
        engulf_bull  = bool(last.get("pattern_engulf_bull", False))
        engulf_bear  = bool(last.get("pattern_engulf_bear", False))

        if pin_bull:
            score += 0.12; factors.append("bullish pin bar")
        if pin_bear:
            score -= 0.12; factors.append("bearish pin bar")
        if engulf_bull:
            score += 0.12; factors.append("bullish engulfing")
        if engulf_bear:
            score -= 0.12; factors.append("bearish engulfing")
    except Exception:
        pin_bull = pin_bear = engulf_bull = engulf_bear = False

    # ── Bollinger Band position (±0.10) ────────────────────────────────────────
    try:
        bb_pct_b  = last.get("bb_pct_b", 0.5)
        bb_upper  = last.get("bb_upper")
        bb_lower  = last.get("bb_lower")
        bb_mid    = last.get("bb_mid")

        if bb_pct_b is not None and bb_pct_b == bb_pct_b:  # NaN check
            if bb_pct_b <= 0.05:
                score += 0.10; factors.append(f"below BB lower band (%B={bb_pct_b:.2f}) — oversold extension")
            elif bb_pct_b >= 0.95:
                score -= 0.10; factors.append(f"above BB upper band (%B={bb_pct_b:.2f}) — overbought extension")
            elif bb_pct_b <= 0.20 and score > 0:
                score += 0.05; factors.append(f"near BB lower band (%B={bb_pct_b:.2f})")
            elif bb_pct_b >= 0.80 and score < 0:
                score -= 0.05; factors.append(f"near BB upper band (%B={bb_pct_b:.2f})")
    except Exception:
        bb_pct_b = bb_upper = bb_lower = bb_mid = None

    # Tick volume multiplier — high volume confirms signal, low volume dampens it
    try:
        vol_ratio = last.get("volume_ratio", 1.0)
        if vol_ratio is None or vol_ratio != vol_ratio:  # NaN check
            vol_ratio = 1.0
        if vol_ratio >= 2.0:
            score *= 1.15; factors.append(f"High volume confirmation ({vol_ratio:.1f}× avg)")
        elif vol_ratio >= 1.5:
            score *= 1.07; factors.append(f"Above-average volume ({vol_ratio:.1f}× avg)")
        elif vol_ratio <= 0.5:
            score *= 0.75; factors.append(f"Low volume — signal dampened ({vol_ratio:.1f}× avg)")
    except Exception:
        pass

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
        "pivot":      round(pivot, 5) if pivot and pivot == pivot else None,
        "r1":         round(r1, 5)    if r1    and r1    == r1    else None,
        "r2":         round(r2, 5)    if r2    and r2    == r2    else None,
        "s1":         round(s1, 5)    if s1    and s1    == s1    else None,
        "s2":         round(s2, 5)    if s2    and s2    == s2    else None,
        "near_round_number":      near_round,
        "market_structure":       mkt_structure,
        "pattern_pin_bull":       pin_bull,
        "pattern_pin_bear":       pin_bear,
        "pattern_engulf_bull":    engulf_bull,
        "pattern_engulf_bear":    engulf_bear,
        "volume_ratio":           round(float(last.get("volume_ratio") or 1.0), 2),
        "bb_upper":   round(bb_upper, 5) if bb_upper and bb_upper == bb_upper else None,
        "bb_lower":   round(bb_lower, 5) if bb_lower and bb_lower == bb_lower else None,
        "bb_mid":     round(bb_mid, 5)   if bb_mid   and bb_mid   == bb_mid   else None,
        "bb_pct_b":   round(float(bb_pct_b), 3) if bb_pct_b is not None and bb_pct_b == bb_pct_b else None,
        "factors":    factors,
    }


def check_mtf_alignment(daily_score, h4_score, daily_threshold=0.3):
    """
    Daily sets the bias. H4 must agree with it.
    daily_threshold: how decisive the Daily must be before it can veto H4 (default 0.3).
    Returns (aligned: bool, reason: str).
    """
    if daily_score >= daily_threshold and h4_score < 0:
        return False, f"H4 bearish ({h4_score}) conflicts with Daily bullish bias ({daily_score})"
    if daily_score <= -daily_threshold and h4_score > 0:
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


def analyze_pair(feed, pair, timeframes=["H1", "H4", "D"], params=None):
    """
    Run full multi-timeframe technical analysis on a pair.
    Returns a structured dict ready to pass to Claude.
    params: optional dict to override thresholds (used by Test Mode):
      - adx_threshold (default 25): ADX minimum for trending regime
      - mtf_daily_threshold (default 0.3): Daily bias strength required to veto H4
      - confluence_min (default 0.6): score threshold for BUY/SELL direction hint to Claude
      - require_h1_confirm (default True): whether H1 non-confirmation dampens H4 score
    """
    p = params or {}
    adx_threshold      = p.get("adx_threshold", 25)
    mtf_daily_threshold = p.get("mtf_daily_threshold", 0.3)
    confluence_min     = p.get("confluence_min", 0.6)
    require_h1_confirm = p.get("require_h1_confirm", True)

    raw = {}
    for tf in timeframes:
        df = feed.get_candles(pair, tf, count=200)
        enriched = run_all(df)
        raw[tf] = score_candle(enriched, adx_threshold=adx_threshold)

    daily = raw.get("D", {})
    h4    = raw.get("H4", {})
    h1    = raw.get("H1", {})

    daily_score = daily.get("score", 0)
    h4_score    = h4.get("score", 0)
    h1_score    = h1.get("score", 0)

    mtf_ok, mtf_reason = check_mtf_alignment(daily_score, h4_score, daily_threshold=mtf_daily_threshold)

    # H1 confirmation: same direction as H4?
    h1_confirms = (h4_score > 0 and h1_score > 0) or (h4_score < 0 and h1_score < 0)

    # Final actionable score: H4 is primary, dampen if MTF conflicts or H1 doesn't confirm
    final_score = h4_score
    if not mtf_ok:
        final_score = 0.0  # blocked by Daily bias
    elif require_h1_confirm and not h1_confirms:
        final_score = round(h4_score * 0.7, 3)  # reduce but don't block

    if final_score >= confluence_min:
        direction = "BUY"
    elif final_score <= -confluence_min:
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
