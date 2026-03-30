"""
Claude analysis agent — synthesizes technical + fundamental data into
a structured trade thesis using the Anthropic API.
"""
import json
import anthropic
from config import ANTHROPIC_API_KEY
from data.price_feed import PriceFeed
from analysis.signals import analyze_pair, check_correlation
from agent.fundamentals import get_fundamentals_for_pair
from data.store import save_signal

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a risk filter for an automated forex trading system.

The technical system has already generated a directional signal (BUY or SELL) with a confluence score.
Your role is NOT to generate trade ideas — it is to identify reasons NOT to take this trade.

You will be given:
1. The technically-confirmed direction and confluence score
2. Key price levels: pivot points, round number proximity, market structure
3. COT positioning data
4. Interest rate differential and carry bias
5. Upcoming high-impact economic events
6. Recent news headlines
7. Any correlation warnings

Your job: act as a veto filter. Look for fundamental or macro reasons the technical signal should be suppressed.

Veto reasons include (but are not limited to):
- High-impact news event within 4 hours that could reverse the move
- COT positioning extremely extended in the signal direction (crowded trade risk)
- Rate differential strongly opposed to the technical direction (carry headwind)
- Market structure conflict (technical says BUY but structure is lower highs/lower lows)
- Correlation warning: same direction as a correlated pair already in the portfolio
- Geopolitical or macro event that fundamentally changes the pair's outlook

Risk levels:
  HIGH   = suppress the trade (return NO_TRADE)
  MEDIUM = proceed but reduce confidence by 20%
  LOW    = proceed, note the risk

Always respond with valid JSON in exactly this structure:
{
  "direction": "BUY" | "SELL" | "NO_TRADE",
  "confidence": 0.0-1.0,
  "timeframe": "H1" | "H4" | "D",
  "veto_risk": "HIGH" | "MEDIUM" | "LOW" | "NONE",
  "veto_reasons": ["list of specific reasons to avoid or caution this trade, empty if none"],
  "entry_zone": "brief description",
  "stop_loss": "brief description",
  "take_profit": "brief description",
  "reasoning": "2-3 sentence summary of why to take or avoid this trade",
  "fundamental_alignment": "how fundamentals support or contradict the technical bias"
}

If veto_risk is HIGH, set direction to NO_TRADE.
If no veto reasons exist, confirm the technical direction with the provided confidence.
Do not invent trade ideas. Your only inputs are what is given to you."""

_VALID_DIRECTIONS  = {"BUY", "SELL", "NO_TRADE"}
_VALID_TIMEFRAMES  = {"H1", "H4", "D"}
_VALID_VETO_RISKS  = {"HIGH", "MEDIUM", "LOW", "NONE"}


def _validate_thesis(raw_dict):
    """
    Strict output validation. Raises ValueError on any schema violation.
    Prevents malformed Claude output from reaching the execution engine.
    """
    direction = raw_dict.get("direction")
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction: {direction!r}")

    confidence = raw_dict.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        raise ValueError(f"Invalid confidence: {confidence!r}")

    timeframe = raw_dict.get("timeframe")
    if timeframe not in _VALID_TIMEFRAMES:
        # Non-fatal — default to H4 rather than rejecting
        raw_dict["timeframe"] = "H4"

    veto_risk = raw_dict.get("veto_risk", "NONE")
    if veto_risk not in _VALID_VETO_RISKS:
        raw_dict["veto_risk"] = "NONE"

    # Ensure veto_reasons is a list
    if not isinstance(raw_dict.get("veto_reasons"), list):
        raw_dict["veto_reasons"] = []

    # Apply MEDIUM veto: reduce confidence
    if veto_risk == "MEDIUM":
        raw_dict["confidence"] = round(float(confidence) * 0.80, 3)

    return raw_dict


def build_prompt(pair, technicals, fundamentals, correlation_warning=None, technical_direction=None, confluence_score=None):
    tech_summary  = json.dumps(technicals, indent=2, default=str)
    fund_summary  = json.dumps(fundamentals, indent=2, default=str)
    corr_section  = f"\n## Correlation Warning\n{correlation_warning}" if correlation_warning else ""

    # Build Key Price Levels section from H4 timeframe (most relevant for setups)
    key_levels_lines = []
    try:
        h4 = technicals.get("timeframes", {}).get("H4", {})
        pivot = h4.get("pivot")
        r1, r2, s1, s2 = h4.get("r1"), h4.get("r2"), h4.get("s1"), h4.get("s2")
        near_round = h4.get("near_round_number", False)
        mkt_structure = h4.get("market_structure", "ranging")

        if pivot:
            key_levels_lines.append(f"Pivot (PP): {pivot}")
        if r1:
            key_levels_lines.append(f"R1: {r1} | R2: {r2}")
        if s1:
            key_levels_lines.append(f"S1: {s1} | S2: {s2}")
        if near_round:
            dist = h4.get("round_number_distance_pips", "?")
            key_levels_lines.append(f"Near round number: yes ({dist} pips away)")
        key_levels_lines.append(f"Market structure (H4): {mkt_structure}")

        # Bollinger Bands
        bb_upper = h4.get("bb_upper")
        bb_lower = h4.get("bb_lower")
        bb_mid   = h4.get("bb_mid")
        bb_pct_b = h4.get("bb_pct_b")
        if bb_upper and bb_lower:
            key_levels_lines.append(f"BB(20,2): upper={bb_upper} | mid={bb_mid} | lower={bb_lower}")
        if bb_pct_b is not None:
            bb_label = "above upper band" if bb_pct_b > 1 else "below lower band" if bb_pct_b < 0 else f"{bb_pct_b:.0%} up bands"
            key_levels_lines.append(f"BB %B: {bb_pct_b:.3f} ({bb_label})")
    except Exception:
        pass

    key_levels_section = (
        "\n## Key Price Levels\n" + "\n".join(key_levels_lines)
        if key_levels_lines else ""
    )

    direction_line = (
        f"Technical signal: {technical_direction} (confluence score: {confluence_score:+.2f})\n"
        if technical_direction and confluence_score is not None else ""
    )

    return f"""{direction_line}Your task: identify any fundamental or macro reasons NOT to take this {pair} trade.

## Technical Analysis (confluence-scored, multi-timeframe)
{tech_summary}
{key_levels_section}
{corr_section}
## Fundamental Data
{fund_summary}

Respond with JSON only. Do not wrap in markdown."""


def analyze(pair, all_signals=None, params=None):
    """
    Run full analysis on a pair and return a structured trade thesis.
    Pass all_signals dict to enable correlation checks across pairs.
    Pass params dict to override analysis thresholds (Test Mode).
    """
    feed = PriceFeed()

    print(f"[{pair}] Fetching technicals...")
    technicals = analyze_pair(feed, pair, params=params)

    # Store result so correlation check can reference it
    if all_signals is not None:
        all_signals[pair] = technicals

    correlation_warning = None
    if all_signals:
        correlation_warning = check_correlation(pair, all_signals)
        if correlation_warning:
            print(f"[{pair}] ⚠ {correlation_warning}")

    # Gate: skip Claude entirely if technicals are too weak to act on
    confluence_score = technicals.get("final_score", 0)
    confluence_min = (params or {}).get("confluence_min", 0.3)
    if abs(confluence_score) < confluence_min:
        print(f"[{pair}] Skipping Claude — confluence {confluence_score:.2f} below threshold {confluence_min:.2f}")
        return {
            "pair":              pair,
            "direction":         "NO_TRADE",
            "confidence":        0.0,
            "confluence_score":  confluence_score,
            "regime":            technicals.get("timeframes", {}).get("H4", {}).get("regime", "unknown"),
            "correlation_warning": correlation_warning,
            "reasoning":         f"Technicals too weak (score {confluence_score:.2f}) — Claude not consulted.",
        }

    print(f"[{pair}] Fetching fundamentals...")
    fundamentals = get_fundamentals_for_pair(pair)

    technical_direction = technicals.get("direction")
    prompt = build_prompt(
        pair, technicals, fundamentals, correlation_warning,
        technical_direction=technical_direction,
        confluence_score=confluence_score,
    )

    print(f"[{pair}] Asking Claude (veto filter)...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break

    try:
        parsed = json.loads(raw)
        thesis = _validate_thesis(parsed)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[{pair}] Claude output validation failed: {e} — defaulting to NO_TRADE")
        thesis = {
            "direction":   "NO_TRADE",
            "confidence":  0.0,
            "timeframe":   "H4",
            "veto_risk":   "HIGH",
            "veto_reasons": [f"Output validation failed: {e}"],
            "reasoning":   "Claude returned malformed output — trade suppressed for safety.",
        }

    thesis["pair"] = pair
    thesis["confluence_score"] = technicals.get("final_score", 0)
    thesis["regime"] = technicals.get("timeframes", {}).get("H4", {}).get("regime", "unknown")
    thesis["correlation_warning"] = correlation_warning
    thesis["timeframes"] = technicals.get("timeframes", {})
    thesis["news_blackout"] = fundamentals.get("news_blackout", (False, None, None))

    save_signal(
        pair=pair,
        timeframe=thesis.get("timeframe", "H4"),
        direction=thesis.get("direction", "NO_TRADE"),
        confidence=thesis.get("confidence", 0),
        reasoning=thesis.get("reasoning", "")
    )

    return thesis


def analyze_all(pairs, params=None):
    """Run analysis on every pair, passing a shared signals dict for correlation checks."""
    results = {}
    all_signals = {}
    for pair in pairs:
        try:
            results[pair] = analyze(pair, all_signals=all_signals, params=params)
        except Exception as e:
            print(f"[ERROR] {pair}: {e}")
            results[pair] = {"pair": pair, "direction": "ERROR", "error": str(e)}
    return results
