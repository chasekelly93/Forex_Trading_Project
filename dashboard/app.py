"""
Flask dashboard — view account state, signals, trades, and control the agent.
Run with: python -m dashboard.app
"""
from flask import Flask, render_template, jsonify, request, redirect, url_for
import sqlite3
import threading
from pathlib import Path
from data.oanda_client import OandaClient
from data.store import get_recent_signals, get_open_trades
from execution.executor import Executor

app = Flask(__name__)
client = OandaClient()
executor = Executor()

DB_PATH = Path(__file__).parent.parent / "data" / "forex_agent.db"

# Simple in-memory state — resets on server restart
_agent_paused = False
_cycle_running = False
_cycle_cancelled = False
_cycle_log = []


def _conn():
    return sqlite3.connect(DB_PATH)


def get_trade_history(limit=50):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows


def get_account_snapshots(limit=48):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, balance, nav, open_pnl FROM account_snapshots ORDER BY ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return list(reversed(rows))


# ── Pages ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    try:
        acct = client.get_account()
        account = {
            "balance":    float(acct["balance"]),
            "nav":        float(acct["NAV"]),
            "open_pnl":   float(acct["unrealizedPL"]),
            "margin_used": float(acct["marginUsed"]),
            "currency":   acct["currency"],
        }
    except Exception as e:
        account = {"error": str(e)}

    try:
        positions = client.get_open_positions()
    except Exception:
        positions = []

    signals = get_recent_signals(10)
    trades = get_trade_history(20)
    snapshots = get_account_snapshots(48)

    return render_template(
        "index.html",
        account=account,
        positions=positions,
        signals=signals,
        trades=trades,
        snapshots=snapshots,
        paused=_agent_paused,
    )


# ── API endpoints (used by dashboard controls) ─────────────────────────────

@app.route("/api/account")
def api_account():
    try:
        acct = client.get_account()
        return jsonify({
            "balance":  float(acct["balance"]),
            "nav":      float(acct["NAV"]),
            "open_pnl": float(acct["unrealizedPL"]),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pause", methods=["POST"])
def api_pause():
    global _agent_paused
    _agent_paused = True
    return jsonify({"status": "paused"})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    global _agent_paused
    _agent_paused = False
    return jsonify({"status": "resumed"})


@app.route("/api/close-all", methods=["POST"])
def api_close_all():
    global _cycle_cancelled
    _cycle_cancelled = True  # abort any running cycle immediately
    results = executor.close_all_positions()
    with _conn() as conn:
        conn.execute("UPDATE trades SET status='closed', closed=datetime('now') WHERE status='open'")
        conn.commit()
    return jsonify({"results": results})


@app.route("/api/close/<instrument>", methods=["POST"])
def api_close_pair(instrument):
    global _cycle_cancelled
    _cycle_cancelled = True  # abort cycle so it doesn't re-open this pair
    try:
        resp = client.close_position(instrument)

        # Mark open trades for this pair as closed in DB
        with _conn() as conn:
            fill = resp.get("relatedTransactionIDs", [])
            conn.execute(
                "UPDATE trades SET status='closed', closed=datetime('now') WHERE pair=? AND status='open'",
                (instrument,)
            )
            conn.commit()

        return jsonify({"status": "closed", "instrument": instrument})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/paused")
def api_paused():
    return jsonify({"paused": _agent_paused})


@app.route("/api/run-cycle", methods=["POST"])
def api_run_cycle():
    global _cycle_running, _cycle_log

    if _cycle_running:
        return jsonify({"status": "already_running"})

    if _agent_paused:
        return jsonify({"status": "paused", "message": "Agent is paused — resume it first"})

    def run():
        global _cycle_running, _cycle_cancelled, _cycle_log
        from config import PAIRS
        from agent.claude_agent import analyze
        from data.store import save_snapshot

        _cycle_running = True
        _cycle_cancelled = False
        _cycle_log = []

        try:
            state = executor.snapshot_account()
            _cycle_log.append(f"Account: ${state['balance']:,.2f} balance | ${state['open_pnl']:,.2f} open P&L")

            # Show market hours status upfront
            mkt_ok, mkt_msg = executor.risk.check_market_hours()
            if not mkt_ok:
                _cycle_log.append(f"⚠ {mkt_msg} — signals will generate but no orders will be placed.")

            for pair in PAIRS:
                if _cycle_cancelled:
                    _cycle_log.append("Cycle cancelled.")
                    break

                _cycle_log.append(f"Analyzing {pair}...")
                try:
                    thesis = analyze(pair)
                    direction = thesis.get("direction")
                    confidence = thesis.get("confidence", 0)
                    _cycle_log.append(f"{pair}: {direction} @ {confidence:.0%}")

                    if direction not in ("NO_TRADE", "ERROR"):
                        result = executor.execute(thesis)
                        status = result.get('status')
                        detail = result.get('error') or result.get('reason') or ''
                        _cycle_log.append(f"{pair} execution: {status} {('— ' + detail) if detail else ''}")
                except Exception as e:
                    _cycle_log.append(f"{pair} error: {str(e)}")

            if not _cycle_cancelled:
                _cycle_log.append("Cycle complete.")
        finally:
            _cycle_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/cycle-status")
def api_cycle_status():
    return jsonify({
        "running": _cycle_running,
        "log": _cycle_log,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
