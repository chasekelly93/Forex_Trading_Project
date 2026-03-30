"""
Flask dashboard — view account state, signals, trades, and control the agent.
Run with: python -m dashboard.app
"""
from flask import Flask, render_template, jsonify, request, redirect, url_for
import sqlite3
from pathlib import Path
from data.oanda_client import OandaClient
from data.store import get_recent_signals, get_open_trades
from execution.executor import Executor

app = Flask(__name__)
client = OandaClient()
executor = Executor()

DB_PATH = Path(__file__).parent.parent / "data" / "forex_agent.db"

# Simple in-memory pause flag — reset on server restart
_agent_paused = False


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
    results = executor.close_all_positions()
    return jsonify({"results": results})


@app.route("/api/close/<instrument>", methods=["POST"])
def api_close_pair(instrument):
    try:
        resp = client.close_position(instrument)
        return jsonify({"status": "closed", "instrument": instrument})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/paused")
def api_paused():
    return jsonify({"paused": _agent_paused})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
