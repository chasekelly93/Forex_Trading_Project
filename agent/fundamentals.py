"""
Fundamental data fetcher — economic calendar, COT reports, news headlines.
All free sources, no additional API keys required.
"""
import requests
import csv
import io
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET


# Central bank policy rates — update when rates change
INTEREST_RATES = {
    "USD": 5.25,
    "EUR": 4.50,
    "GBP": 5.25,
    "JPY": 0.10,
    "AUD": 4.35,
    "CAD": 5.00,
    "NZD": 5.50,
    "CHF": 1.75,
}

# Currency to country mapping for filtering news/events
CURRENCY_COUNTRY = {
    "EUR": ["EUR", "eurozone", "ecb", "euro"],
    "USD": ["USD", "united states", "fed", "fomc", "dollar"],
    "GBP": ["GBP", "united kingdom", "boe", "bank of england", "sterling"],
    "JPY": ["JPY", "japan", "boj", "bank of japan", "yen"],
    "AUD": ["AUD", "australia", "rba", "aussie"],
    "CAD": ["CAD", "canada", "boc", "bank of canada", "loonie"],
    "NZD": ["NZD", "new zealand", "rbnz"],
    "CHF": ["CHF", "switzerland", "snb", "franc"],
}

# Forex-related news RSS feeds (no API key needed)
NEWS_FEEDS = [
    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "https://www.forexlive.com/feed/news",
    "https://www.dailyfx.com/feeds/all",
]


_INJECTION_PATTERNS = [
    "ignore previous", "ignore prior", "disregard", "forget instructions",
    "new instructions", "system prompt", "you are now", "act as",
    "return buy", "return sell", "confidence 1.0", "confidence: 1",
]

def _sanitize_headline(text):
    """Strip potential prompt injection from external headline text."""
    if not text:
        return ""
    lower = text.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in lower:
            return "[headline removed — injection pattern detected]"
    # Remove any special characters that could break JSON or prompt structure
    return text.replace("```", "").replace("##", "").replace("\n", " ").strip()[:200]


def get_news_headlines(max_per_feed=5):
    """
    Fetch recent forex news headlines from RSS feeds.
    Returns a list of { title, source, published } dicts.
    """
    headlines = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ForexAgent/1.0)"}

    for url in NEWS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            root = ET.fromstring(resp.text)
            items = root.findall(".//item")[:max_per_feed]
            source = url.split("/")[2].replace("www.", "")
            for item in items:
                title = _sanitize_headline(item.findtext("title", "").strip())
                pub = item.findtext("pubDate", "").strip()
                if title:
                    headlines.append({"title": title, "source": source, "published": pub})
        except Exception as e:
            headlines.append({"title": f"[Feed unavailable: {source}]", "source": url, "published": ""})

    return headlines


def get_cot_report(currency="EUR"):
    """
    Fetch the latest CFTC Commitment of Traders data for a currency.
    Returns net positioning (large speculators: longs - shorts).
    Source: CFTC disaggregated futures-only report.
    """
    url = "https://www.cftc.gov/dea/newcot/c_disagg.txt"
    headers = {"User-Agent": "Mozilla/5.0"}

    # Map currency to the contract name used in CFTC data
    contract_map = {
        "EUR": "EURO FX",
        "GBP": "BRITISH POUND",
        "JPY": "JAPANESE YEN",
        "CHF": "SWISS FRANC",
        "AUD": "AUSTRALIAN DOLLAR",
        "CAD": "CANADIAN DOLLAR",
        "NZD": "NEW ZEALAND DOLLAR",
    }

    target = contract_map.get(currency.upper())
    if not target:
        return {"currency": currency, "error": "No COT data for this currency"}

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        reader = csv.reader(io.StringIO(resp.text))
        headers_row = next(reader)

        # Find column indices
        name_col = 0
        longs_col = headers_row.index("Noncommercial Positions-Long (All)")
        shorts_col = headers_row.index("Noncommercial Positions-Short (All)")
        date_col = headers_row.index("As of Date in Form YYYY-MM-DD")

        for row in reader:
            if target.lower() in row[name_col].lower():
                longs = int(row[longs_col].replace(",", ""))
                shorts = int(row[shorts_col].replace(",", ""))
                net = longs - shorts
                return {
                    "currency": currency,
                    "report_date": row[date_col],
                    "longs": longs,
                    "shorts": shorts,
                    "net": net,
                    "bias": "bullish" if net > 0 else "bearish",
                    "note": f"Large speculators net {'long' if net > 0 else 'short'} by {abs(net):,} contracts"
                }

        return {"currency": currency, "error": "Contract not found in report"}

    except Exception as e:
        return {"currency": currency, "error": str(e)}


def get_economic_calendar():
    """
    Fetch upcoming high-impact economic events from ForexFactory RSS.
    Returns a list of event dicts for the next 7 days.
    """
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=8)
        events = resp.json()

        high_impact = []
        for e in events:
            if e.get("impact") in ("High", "Medium"):
                high_impact.append({
                    "date":     e.get("date", ""),
                    "time":     e.get("time", ""),
                    "currency": e.get("currency", ""),
                    "event":    e.get("title", ""),
                    "impact":   e.get("impact", ""),
                    "forecast": e.get("forecast", ""),
                    "previous": e.get("previous", ""),
                })

        return high_impact

    except Exception as e:
        return [{"error": str(e)}]


def get_rate_differential(pair):
    """
    Return interest rate differential context for a pair (e.g. 'EUR_USD').
    Includes base/quote rates, differential, carry bias, and a magnitude label.
    """
    try:
        base, quote = pair.split("_")
        base_rate  = INTEREST_RATES.get(base, 0.0)
        quote_rate = INTEREST_RATES.get(quote, 0.0)
        diff = base_rate - quote_rate

        if diff > 0:
            carry_bias = "long base"
        elif diff < 0:
            carry_bias = "long quote"
        else:
            carry_bias = "neutral"

        abs_diff = abs(diff)
        if abs_diff >= 3.0:
            magnitude = "large"
        elif abs_diff >= 1.0:
            magnitude = "moderate"
        else:
            magnitude = "small"

        return {
            "base":         base,
            "quote":        quote,
            "base_rate":    base_rate,
            "quote_rate":   quote_rate,
            "differential": round(diff, 2),
            "carry_bias":   carry_bias,
            "magnitude":    magnitude,
        }
    except Exception as e:
        return {"error": str(e)}


def check_news_blackout(pair, calendar=None):
    """
    Return (True, event_name, minutes_away) if a high-impact news event for either
    currency in the pair is within 60 minutes (before or after) of now UTC.
    Returns (False, None, None) when clear.
    """
    try:
        if calendar is None:
            calendar = get_economic_calendar()

        base, quote = pair.split("_")
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        for event in calendar:
            if event.get("currency") not in (base, quote):
                continue
            if event.get("impact") != "High":
                continue

            event_time_str = event.get("time", "")
            event_date_str = event.get("date", today_str)

            # Normalise date — ForexFactory sometimes uses "Mar 30" style
            try:
                # Try ISO format first
                event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                try:
                    event_date = datetime.strptime(event_date_str, "%b %d").replace(year=now_utc.year).date()
                except ValueError:
                    continue

            if not event_time_str or event_time_str.lower() in ("all day", "tentative", ""):
                continue

            try:
                # ForexFactory uses "8:30am" / "2:00pm" format
                event_dt_naive = datetime.strptime(
                    f"{event_date} {event_time_str.lower()}", "%Y-%m-%d %I:%M%p"
                )
                # ForexFactory times are US Eastern — treat as UTC for simplicity
                # (close enough for a 60-min blackout window)
                event_dt = event_dt_naive.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            delta_minutes = abs((event_dt - now_utc).total_seconds()) / 60
            if delta_minutes <= 60:
                return (True, event.get("event", "Unknown event"), round(delta_minutes))

        return (False, None, None)

    except Exception:
        return (False, None, None)


def get_fundamentals_for_pair(pair):
    """
    Pull all fundamental context for a given pair (e.g. 'EUR_USD').
    Returns a dict with calendar events, COT, news, rate differential, and news blackout.
    """
    base, quote = pair.split("_")

    cot_base = get_cot_report(base)
    cot_quote = get_cot_report(quote)
    calendar = get_economic_calendar()

    # Filter calendar to events for either currency
    pair_events = [
        e for e in calendar
        if e.get("currency") in (base, quote)
    ]

    headlines = get_news_headlines()

    # Filter headlines loosely relevant to either currency
    keywords = CURRENCY_COUNTRY.get(base, []) + CURRENCY_COUNTRY.get(quote, [])
    relevant_news = [
        h for h in headlines
        if any(kw.lower() in h["title"].lower() for kw in keywords)
    ]

    rate_diff   = get_rate_differential(pair)
    news_blackout = check_news_blackout(pair, calendar=calendar)

    return {
        "pair":             pair,
        "cot":              {base: cot_base, quote: cot_quote},
        "upcoming_events":  pair_events,
        "news_headlines":   relevant_news[:8],
        "rate_differential": rate_diff,
        "news_blackout":    news_blackout,
    }
