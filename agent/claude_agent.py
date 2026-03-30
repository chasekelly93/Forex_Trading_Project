"""
Claude analysis agent — synthesizes technical + fundamental data into
a structured trade thesis using the Anthropic API.
"""
import json
import anthropic
from config import ANTHROPIC_API_KEY
from data.price_feed import PriceFeed
from analysis.signals import analyze_pair
from agent.fundamentals import get_fundamentals_for_pair
from data.store import save_signal

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert forex trading analyst. You will be given:
1. Technical analysis across H1, H4, and Daily timeframes
2. COT (Commitment of Traders) positioning data
3. Upcoming high-impact economic events
4. Recent relevant news headlines

Your job is to synthesize all of this into a clear, actionable trade thesis.

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

Be conservative. Only recommend a trade when signals align across multiple timeframes.
When in doubt, output NO_TRADE."""


def build_prompt(pair, technicals, fundamentals):
    """Construct the analysis prompt for Claude."""

    tech_summary = json.dumps(technicals, indent=2)
    fund_summary = json.dumps(fundamentals, indent=2, default=str)

    return f"""Analyze {pair} and provide a trade recommendation.

## Technical Analysis (multi-timeframe)
{tech_summary}

## Fundamental Data
{fund_summary}

Respond with JSON only."""


def analyze(pair):
    """
    Run full analysis on a pair and return a structured trade thesis.
    Also saves the signal to the database.
    """
    feed = PriceFeed()

    print(f"[{pair}] Fetching technicals...")
    technicals = analyze_pair(feed, pair)

    print(f"[{pair}] Fetching fundamentals...")
    fundamentals = get_fundamentals_for_pair(pair)

    prompt = build_prompt(pair, technicals, fundamentals)

    print(f"[{pair}] Asking Claude...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    thesis = json.loads(raw)
    thesis["pair"] = pair

    # Save to database
    save_signal(
        pair=pair,
        timeframe=thesis.get("timeframe", "H4"),
        direction=thesis.get("direction", "NO_TRADE"),
        confidence=thesis.get("confidence", 0),
        reasoning=thesis.get("reasoning", "")
    )

    return thesis


def analyze_all(pairs):
    """Run analysis on every pair and return results."""
    results = {}
    for pair in pairs:
        try:
            results[pair] = analyze(pair)
        except Exception as e:
            print(f"[ERROR] {pair}: {e}")
            results[pair] = {"pair": pair, "direction": "ERROR", "error": str(e)}
    return results
