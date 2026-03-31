"""
Backtester — replays historical OANDA candles through the signal + risk engine.

Usage:
    from analysis.backtest import run_backtest
    results = run_backtest("EUR_USD", "2024-01-01", "2024-03-31", granularity="H1")
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import numpy as np

from analysis.indicators import run_all
from analysis.signals import score_candle

# Pip sizes (same as risk.py)
_PIP_SIZE = {
    "USD_JPY": 0.01, "EUR_JPY": 0.01, "GBP_JPY": 0.01,
    "AUD_JPY": 0.01, "CAD_JPY": 0.01, "CHF_JPY": 0.01, "NZD_JPY": 0.01,
    "XAU_USD": 0.1,
}
_DEFAULT_PIP = 0.0001

# Granularity → approximate minutes
_GRAN_MINUTES = {
    "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D": 1440,
}

# Slippage in pips per side
_SLIPPAGE = {
    "EUR_USD": 1.0, "GBP_USD": 1.0, "USD_JPY": 1.0,
    "USD_CHF": 1.0, "AUD_USD": 1.0, "USD_CAD": 1.0, "NZD_USD": 1.0,
}
_DEFAULT_SLIPPAGE = 2.0


def _fetch_candles(pair: str, start: str, end: str, granularity: str) -> pd.DataFrame:
    """Fetch historical candles from OANDA and return as a DataFrame."""
    import oandapyV20.endpoints.instruments as instruments
    from oandapyV20 import API as OandaAPI
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.environ["OANDA_API_KEY"]
    env     = os.environ.get("OANDA_ENVIRONMENT", "practice")
    api     = OandaAPI(access_token=api_key, environment=env)

    # OANDA caps at 5000 candles per request — chunk into safe windows
    gran_mins   = _GRAN_MINUTES.get(granularity, 60)
    chunk_bars  = 4000                                       # stay under 5000 limit
    chunk_delta = timedelta(minutes=gran_mins * chunk_bars)

    end_dt  = datetime.fromisoformat(f"{end}T23:59:59").replace(tzinfo=timezone.utc)
    cursor  = datetime.fromisoformat(f"{start}T00:00:00").replace(tzinfo=timezone.utc)
    all_candles = []

    while cursor < end_dt:
        chunk_end = min(cursor + chunk_delta, end_dt)
        params = {
            "granularity": granularity,
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":   chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": "M",
        }
        r = instruments.InstrumentsCandles(pair, params=params)
        api.request(r)
        candles = r.response.get("candles", [])

        for c in candles:
            if not c["complete"]:
                continue
            mid = c["mid"]
            all_candles.append({
                "time":   c["time"][:19],
                "open":   float(mid["o"]),
                "high":   float(mid["h"]),
                "low":    float(mid["l"]),
                "close":  float(mid["c"]),
                "volume": int(c.get("volume", 0)),
            })

        cursor = chunk_end + timedelta(minutes=gran_mins)

    if not all_candles:
        raise ValueError(f"No candles returned for {pair} {start}→{end} {granularity}")

    df = pd.DataFrame(all_candles)
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    return df


def run_backtest(
    pair: str,
    start: str,
    end: str,
    granularity: str = "H1",
    stop_pips: float = 20.0,
    take_profit_ratio: float = 2.0,
    confidence_min: float = 0.55,
    confluence_min: float = 0.55,
    initial_balance: float = 10_000.0,
    risk_pct: float = 1.0,
) -> dict:
    """
    Replay historical candles for `pair` between `start` and `end`.

    Returns a dict with:
      - trades: list of simulated trade dicts
      - equity_curve: list of {time, equity} dicts
      - summary: win_rate, total_pnl, max_drawdown, sharpe, total_trades
    """
    df_raw = _fetch_candles(pair, start, end, granularity)
    df     = run_all(df_raw.copy())

    pip      = _PIP_SIZE.get(pair, _DEFAULT_PIP)
    slip     = _SLIPPAGE.get(pair, _DEFAULT_SLIPPAGE) * pip
    sl_dist  = stop_pips * pip
    tp_dist  = stop_pips * take_profit_ratio * pip
    risk_amt = initial_balance * (risk_pct / 100)

    trades      = []
    equity      = initial_balance
    equity_curve = []
    in_trade    = False
    entry_price = None
    direction   = None
    sl          = None
    tp          = None
    entry_time  = None

    rows = df.reset_index()

    for i, row in rows.iterrows():
        ts = row["time"]

        # ── Check if open trade hit SL or TP ─────────────────────────────
        if in_trade:
            high = row["high"]
            low  = row["low"]
            closed = False
            exit_price = None
            exit_reason = None

            if direction == "BUY":
                if low <= sl:
                    exit_price  = sl
                    exit_reason = "SL"
                    closed = True
                elif high >= tp:
                    exit_price  = tp
                    exit_reason = "TP"
                    closed = True
            else:  # SELL
                if high >= sl:
                    exit_price  = sl
                    exit_reason = "SL"
                    closed = True
                elif low <= tp:
                    exit_price  = tp
                    exit_reason = "TP"
                    closed = True

            if closed:
                raw = (exit_price - entry_price) if direction == "BUY" else (entry_price - exit_price)
                pnl_pips = raw / pip
                pnl_amt  = (raw / sl_dist) * risk_amt if sl_dist else 0
                equity  += pnl_amt

                trades.append({
                    "entry_time":  str(entry_time),
                    "exit_time":   str(ts),
                    "direction":   direction,
                    "entry_price": round(entry_price, 5),
                    "exit_price":  round(exit_price, 5),
                    "sl":          round(sl, 5),
                    "tp":          round(tp, 5),
                    "pnl_pips":    round(pnl_pips, 1),
                    "pnl_amt":     round(pnl_amt, 2),
                    "exit_reason": exit_reason,
                    "equity":      round(equity, 2),
                })
                equity_curve.append({"time": str(ts), "equity": round(equity, 2)})
                in_trade = False
                continue

        # ── Score this candle for a new signal ───────────────────────────
        if in_trade or i < 50:  # need 50 bars of history for indicators
            equity_curve.append({"time": str(ts), "equity": round(equity, 2)})
            continue

        window = df.iloc[max(0, i - 100): i + 1]
        try:
            scored = score_candle(window, pair)
        except Exception:
            equity_curve.append({"time": str(ts), "equity": round(equity, 2)})
            continue

        sig_dir    = scored.get("direction")
        confidence = scored.get("confidence", 0)
        confluence = scored.get("confluence_score", 0)

        if (
            sig_dir in ("BUY", "SELL")
            and confidence >= confidence_min
            and confluence >= confluence_min
        ):
            entry_raw = row["close"]
            if sig_dir == "BUY":
                entry_price = entry_raw + slip
                sl = entry_price - sl_dist
                tp = entry_price + tp_dist
            else:
                entry_price = entry_raw - slip
                sl = entry_price + sl_dist
                tp = entry_price - tp_dist

            in_trade   = True
            direction  = sig_dir
            entry_time = ts

        equity_curve.append({"time": str(ts), "equity": round(equity, 2)})

    # ── Summary stats ─────────────────────────────────────────────────────
    total   = len(trades)
    wins    = [t for t in trades if t["pnl_amt"] > 0]
    losses  = [t for t in trades if t["pnl_amt"] <= 0]
    total_pnl = sum(t["pnl_amt"] for t in trades)

    # Max drawdown
    peak = initial_balance
    max_dd = 0.0
    for point in equity_curve:
        e = point["equity"]
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe (daily returns)
    if len(equity_curve) > 1:
        equities = [p["equity"] for p in equity_curve]
        rets = np.diff(equities) / equities[:-1]
        sharpe = (np.mean(rets) / np.std(rets) * np.sqrt(252)) if np.std(rets) > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "pair":        pair,
        "start":       start,
        "end":         end,
        "granularity": granularity,
        "trades":      trades,
        "equity_curve": equity_curve,
        "summary": {
            "total_trades":  total,
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(len(wins) / total * 100, 1) if total else 0,
            "total_pnl":     round(total_pnl, 2),
            "avg_win":       round(sum(t["pnl_amt"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss":      round(sum(t["pnl_amt"] for t in losses) / len(losses), 2) if losses else 0,
            "max_drawdown":  round(max_dd, 2),
            "sharpe":        round(float(sharpe), 2),
            "final_equity":  round(equity, 2),
            "return_pct":    round((equity - initial_balance) / initial_balance * 100, 2),
        },
    }
