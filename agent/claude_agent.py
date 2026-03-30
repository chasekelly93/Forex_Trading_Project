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

SYSTEM_PROMPT = """You are an expert forex trading analyst. You will be given:
1. Technical analysis with a confluence score (-1.0 to +1.0), regime detection, and multi-timeframe breakdown
2. COT (Commitment of Traders) positioning data
3. Upcoming high-impact economic events
4. Recent relevant news headlines
5. Any correlation warnings (e.g. two correlated pairs signaling the same direction)

Confluence score guide:
  +0.6 to +1.0 = strong buy signal
  +0.3 to +0.6 = weak/developing buy
   0.0          = neutral
  -0.3 to -0.6 = weak/developing sell
  -0.6 to -1.0 = strong sell signal

Regime:
  "trending" = ADX >= 25, trend-following signals are reliable
  "ranging"  = ADX < 25, only mean-reversion signals used

MTF rules already enforced in the data:
  - Daily sets the directional bias
  - H4 provides the setup (primary entry timeframe)
  - H1 provides timing confirmation
  - Counter-trend trades against Daily are pre-blocked (score set to 0)

Always respond with valid JSON in exactly this structure:
{
  "direction": "BUY" | "SELL" | "NO_TRADE",
  "confidence": 0.0-1.0,
  "timeframe": "H1" | "H4" | "D",
  "entry_zone": "brief description of where to enter",
  "stop_loss": "brief description of stop placement",
  "take_profit": "brief description of target",
  "reasoning": "2-3 sentence explanation of the thesis",
  "risks": "key risks that could invalidate this trade",
  "fundamental_alignment": "how fundamentals support or contradict technical bias"
}

Be conservative. If there is a correlation warning, reduce confidence and note it in risks.
When in doubt, output NO_TRADE."""


def build_prompt(pair, technicals, fundamentals, correlation_warning=None):
    tech_summary  = json.dumps(technicals, indent=2, default=str)
    fund_summary  = json.dumps(fundamentals, indent=2, default=str)
    corr_section  = f"\n## Correlation Warning\n{correlation_warning}" if correlation_warning else ""

    return f"""Analyze {pair} and provide a trade recommendation.

## Technical Analysis (confluence-scored, multi-timeframe)
{tech_summary}
{corr_section}
## Fundamental Data
{fund_summary}

Respond with JSON only."""


def analyze(pair, all_signals=None):
    """
    Run full analysis on a pair and return a structured trade thesis.
    Pass all_signals dict to enable correlation checks across pairs.
    """
    feed = PriceFeed()

    print(f"[{pair}] Fetching technicals...")
    technicals = analyze_pair(feed, pair)

    # Store result so correlation check can reference it
    if all_signals is not None:
        all_signals[pair] = technicals

    correlation_warning = None
    if all_signals:
        correlation_warning = check_correlation(pair, all_signals)
        if correlation_warning:
            print(f"[{pair}] ⚠ {correlation_warning}")

    print(f"[{pair}] Fetching fundamentals...")
    fundamentals = get_fundamentals_for_pair(pair)

    prompt = build_prompt(pair, technicals, fundamentals, correlation_warning)

    print(f"[{pair}] Asking Claude...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    thesis = json.loads(raw)
    thesis["pair"] = pair
    thesis["confluence_score"] = technicals.get("final_score", 0)
    thesis["regime"] = technicals.get("timeframes", {}).get("H4", {}).get("regime", "unknown")
    thesis["correlation_warning"] = correlation_warning

    save_signal(
        pair=pair,
        timeframe=thesis.get("timeframe", "H4"),
        direction=thesis.get("direction", "NO_TRADE"),
        confidence=thesis.get("confidence", 0),
        reasoning=thesis.get("reasoning", "")
    )

    return thesis


def analyze_all(pairs):
    """Run analysis on every pair, passing a shared signals dict for correlation checks."""
    results = {}
    all_signals = {}
    for pair in pairs:
        try:
            results[pair] = analyze(pair, all_signals=all_signals)
        except Exception as e:
            print(f"[ERROR] {pair}: {e}")
            results[pair] = {"pair": pair, "direction": "ERROR", "error": str(e)}
    return results
