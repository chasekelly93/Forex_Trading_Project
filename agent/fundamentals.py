"""
Fundamental data fetcher — economic calendar, COT reports, news headlines.
All free sources, no additional API keys required.
"""
import requests
import csv
import io
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET


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
                title = item.findtext("title", "").strip()
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


def get_fundamentals_for_pair(pair):
    """
    Pull all fundamental context for a given pair (e.g. 'EUR_USD').
    Returns a dict with calendar events, COT, and news.
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

    return {
        "pair": pair,
        "cot": {base: cot_base, quote: cot_quote},
        "upcoming_events": pair_events,
        "news_headlines": relevant_news[:8],
    }
