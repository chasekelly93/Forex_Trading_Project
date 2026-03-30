# Forex Trading Agent — Project Context

Paste this file into a fresh Claude Code session to restore full project context.

---

## What We're Building

A fully autonomous AI-powered forex trading agent. It connects to a live OANDA brokerage account, runs multi-timeframe technical analysis across 7 major currency pairs, sends everything to Claude (Anthropic API) for a structured trade thesis, executes approved trades through OANDA's API, and exposes a web dashboard for monitoring and manual control.

**Current state:** Fully functional on paper/practice account. All phases built and tested. Not yet deployed to a VPS (still running locally).

---

## Architecture Decisions

| Decision | Choice | Reason |
|---|---|---|
| Broker | OANDA (practice) | Clean REST API, free demo account, no dealing desk |
| Execution library | oandapyV20 | Official Python SDK for OANDA v20 API |
| AI model | claude-sonnet-4-6 | Best balance of reasoning quality and cost |
| Database | SQLite (local file) | No server needed, sufficient for single-machine use |
| Web framework | Flask | Lightweight, no overhead, easy to extend |
| Tech stack | Python 3.11 | Best library support for finance/ML |
| Timeframes | H1, H4, Daily | Medium-term swing trading focus |
| Pairs | 7 major pairs | EUR_USD, GBP_USD, USD_JPY, USD_CHF, AUD_USD, USD_CAD, NZD_USD |
| Execution window | 13:00–17:00 UTC Mon–Fri | London/NY overlap — highest volume, tightest spreads |
| Max positions | 3 concurrent | Configured in config.py |
| Risk per trade | 1% of account balance | Configured in config.py |
| Daily drawdown kill-switch | 3% | Halts all trading if hit |
| Monitoring | Web dashboard (port 8080) | Telegram planned but not built yet |

---

## Project Structure

```
forex-agent/
├── config.py                  # All config: API keys, pairs, timeframes, risk params
├── main.py                    # Entry point — runs scheduled loop or one-shot cycle
├── requirements.txt           # Python dependencies
├── .env                       # Real credentials (never committed)
├── .env.example               # Template for .env
├── CONTEXT.md                 # This file
│
├── data/
│   ├── oanda_client.py        # All OANDA API calls (candles, prices, orders, positions)
│   ├── price_feed.py          # Fetches + formats candle DataFrames for all pairs/timeframes
│   └── store.py               # SQLite persistence — signals, trades, account snapshots
│
├── analysis/
│   ├── indicators.py          # Technical indicators: EMA(20/50/200), MACD, RSI, Stochastic, ATR, S/R
│   └── signals.py             # Interprets indicator values into a structured summary dict per timeframe
│
├── agent/
│   ├── claude_agent.py        # Sends technical + fundamental data to Claude, parses trade thesis JSON
│   └── fundamentals.py        # Fetches economic calendar (FF), COT reports (CFTC), news (RSS feeds)
│
├── execution/
│   ├── risk.py                # All safety checks: market hours, confidence threshold, duplicate pairs,
│   │                          #   max positions, drawdown kill-switch, position sizing
│   └── executor.py            # Places/closes orders via OANDA, saves trades to DB
│
├── dashboard/
│   ├── app.py                 # Flask app — serves dashboard, exposes API endpoints for controls
│   └── templates/index.html   # Single-page dashboard UI
│
└── tests/
    ├── test_pipeline.py       # Tests OANDA connection, candle fetch, DB init
    ├── test_analysis.py       # Tests technical analysis engine on live data
    ├── test_agent.py          # Tests full Claude analysis on a single pair
    └── test_execution.py      # Tests risk engine checks and position sizing
```

---

## Data Flow (one full cycle)

```
main.py (scheduled every 4h, or triggered from dashboard)
  └── for each of 7 pairs:
        ├── data/price_feed.py     → fetch H1, H4, D candles from OANDA
        ├── analysis/indicators.py → calculate EMA/MACD/RSI/Stochastic/ATR/S&R
        ├── analysis/signals.py    → interpret indicators into structured summary
        ├── agent/fundamentals.py  → fetch econ calendar, COT, news headlines
        ├── agent/claude_agent.py  → send everything to Claude → get trade thesis JSON
        │     { direction, confidence, timeframe, entry_zone, stop_loss,
        │       take_profit, reasoning, risks, fundamental_alignment }
        ├── data/store.py          → save signal + reasoning to SQLite
        └── execution/risk.py      → run all checks:
              1. Market hours (13:00–17:00 UTC weekdays only)
              2. Confidence >= 60%
              3. No existing position in this pair
              4. Max 3 open positions
              5. Daily drawdown < 3%
              → if approved: execution/executor.py places market order via OANDA
```

---

## Key Files — What Each Does

### `config.py`
Single source of truth for all settings. Edit here to change pairs, timeframes, risk %, max positions, drawdown limit.

### `data/oanda_client.py`
Wraps the oandapyV20 SDK. Key methods:
- `get_candles(pair, timeframe, count)` → raw candle list
- `get_live_price(pairs)` → current bid/ask
- `get_account()` → balance, NAV, P&L
- `get_open_positions()` → all open positions
- `place_market_order(pair, units)` → executes a trade (units negative = sell)
- `close_position(pair)` → detects long vs short side and closes it

### `data/price_feed.py`
Converts raw OANDA candles into clean pandas DataFrames. `get_all()` fetches all 7 pairs × 3 timeframes in one call.

### `data/store.py`
SQLite database at `data/forex_agent.db`. Three tables:
- `signals` — every Claude recommendation with full reasoning
- `trades` — every order placed with open/close price and P&L
- `account_snapshots` — periodic balance/NAV snapshots for history

### `analysis/indicators.py`
Pure functions that take a DataFrame and return it with new columns:
- `add_ema()` — EMA 20, 50, 200
- `add_macd()` — MACD line, signal, histogram
- `add_rsi()` — RSI 14
- `add_stochastic()` — Stochastic %K and %D
- `add_atr()` — ATR 14 (used for volatility / stop sizing)
- `add_support_resistance()` — swing high/low detection

### `analysis/signals.py`
`interpret(df)` reads the enriched DataFrame and returns a plain-English summary dict with trend, MACD state, RSI condition, stochastic state, S/R levels, and an overall bias (strong_buy / buy / neutral / sell / strong_sell). This summary is what gets sent to Claude.

### `agent/fundamentals.py`
Fetches free fundamental data:
- Economic calendar: `https://nfs.faireconomy.media/ff_calendar_thisweek.json`
- COT reports: CFTC disaggregated CSV at `cftc.gov`
- News: ForexLive + DailyFX RSS feeds

### `agent/claude_agent.py`
Builds a prompt combining technical summary + fundamental data, sends to `claude-sonnet-4-6`, parses the JSON response. Claude always returns:
```json
{
  "direction": "BUY|SELL|NO_TRADE",
  "confidence": 0.0-1.0,
  "timeframe": "H1|H4|D",
  "entry_zone": "...",
  "stop_loss": "...",
  "take_profit": "...",
  "reasoning": "...",
  "risks": "...",
  "fundamental_alignment": "..."
}
```

### `execution/risk.py`
The single gate all trades must pass. `approve(pair, thesis)` runs every check in sequence and returns `(approved: bool, reason: str, units: int)`. Position sizing uses 1% account risk ÷ (stop_pips × pip_value_per_unit). Capped at 100,000 units (1 standard lot).

### `execution/executor.py`
`execute(thesis)` calls `risk.approve()` first, then places the order, saves the trade to DB. `close_all_positions()` emergency-closes everything.

### `dashboard/app.py`
Flask app on port 8080. Key endpoints:
- `GET /` — renders the dashboard
- `POST /api/run-cycle` — triggers analysis cycle in background thread
- `GET /api/cycle-status` — returns running state + live log lines
- `POST /api/pause` / `/api/resume` — pauses/resumes order execution
- `POST /api/close-all` — closes all positions AND cancels running cycle
- `POST /api/close/<instrument>` — closes one position AND cancels running cycle
- `GET /api/account` — live account state for auto-refresh

### `dashboard/templates/index.html`
Single-page dashboard. Features:
- Live market clock (local time, UTC, session status, countdown to peak)
- Account cards (balance, NAV, open P&L, position count)
- Open positions table with per-position close button
- Recent signals table (pair, timeframe, direction, confidence)
- Trade history table
- Claude reasoning log (full text of each analysis)
- Run Analysis button → animated progress bar while running, unclickable mid-cycle
- Pause / Resume / Close All controls
- Tooltips on every meaningful element (toggle on/off, saved to localStorage)

### `main.py`
Three run modes:
- `python main.py` — scheduled loop, runs every 4 hours
- `python main.py once` — single cycle and exit
- `python main.py close-all` — emergency position close

---

## How to Run Locally

```bash
cd ~/Projects/forex-agent
source venv/bin/activate

# Start the dashboard
python -m dashboard.app
# Open http://localhost:8080

# Run a single analysis cycle (separate terminal tab)
python main.py once
```

---

## Environment Variables (`.env`)

```
OANDA_API_KEY=...
OANDA_ACCOUNT_ID=...         # Format: 101-001-XXXXXXX-001
OANDA_ENVIRONMENT=practice   # Change to "live" when ready
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=          # Empty — not built yet
TELEGRAM_CHAT_ID=            # Empty — not built yet
```

---

## Risk Rules (enforced in `execution/risk.py`)

1. **Market hours** — only executes 13:00–17:00 UTC, Mon–Fri (London/NY overlap)
2. **Weekend block** — no trading Saturday 22:00 UTC – Sunday 22:00 UTC
3. **Confidence threshold** — Claude must be ≥ 60% confident
4. **No duplicate pairs** — won't open a second position in the same pair
5. **Max 3 positions** — hard cap on concurrent open trades
6. **Daily drawdown kill-switch** — halts all trading if daily loss hits 3%

---

## Known Issues / Fixed Bugs

- `orders.Orders` → fixed to `orders.OrderCreate` (oandapyV20 version mismatch)
- `close_position` was sending both longUnits+shortUnits → fixed to detect which side exists first
- Running analysis cycle would re-open positions immediately after manual close → fixed with `_cycle_cancelled` flag
- Header tooltips were clipping off-screen → fixed to render below buttons
- Run Analysis tooltip clipped by `overflow:hidden` → fixed by wrapping button in a `span.tip`

---

## What's Not Built Yet

| Feature | Notes |
|---|---|
| Telegram alerts | Bot setup, trade notifications, `/pause` `/status` `/close` commands |
| VPS deployment | DigitalOcean/Linode, run 24/7 without local Mac, pull from GitHub |
| Backtesting | Historical P&L testing before live deployment |
| Stop-loss / take-profit orders | Currently market orders only — SL/TP not wired to OANDA yet |
| Close price + P&L in trade history | DB updates on close need to pull fill price from OANDA response |
| Account P&L chart | Snapshot data exists in DB, just needs a chart rendered in dashboard |

---

## GitHub

Repository: `https://github.com/chasekelly93/Forex_Trading_Project`
Branch: `main`
All commits pushed and up to date.
