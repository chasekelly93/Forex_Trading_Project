"""
Flask dashboard — view account state, signals, trades, and control the agent.
Run with: python -m dashboard.app
"""
from flask import Flask, render_template, jsonify, request
import sqlite3
import threading
from pathlib import Path
from config import OANDA_ACCOUNT_ID
from data.oanda_client import OandaClient
from data import store
from data.store import (
    get_recent_signals, get_open_trades, get_open_test_trades,
    get_pnl_summary, get_setting, set_setting, set_active_account, init_db,
    save_account, get_saved_accounts, get_account_api_key,
)
from execution.executor import Executor

app = Flask(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "forex_agent.db"

# ── Account initialisation ─────────────────────────────────────────────────
# init_db first so the settings table exists before we read from it
set_active_account(OANDA_ACCOUNT_ID or "")  # temporary — updated below after init
init_db()
_current_account_id = get_setting("account_id") or OANDA_ACCOUNT_ID or ""
set_active_account(_current_account_id)

client   = OandaClient()
executor = Executor()
client.account_id = _current_account_id
executor.client.account_id = _current_account_id
executor.risk.client.account_id = _current_account_id

_current_account_name = "—"


def _fetch_and_save_account(account_id, api_key):
    """Fetch account name from OANDA and persist to the accounts table."""
    global _current_account_name
    try:
        acct = client.get_account()
        name = acct.get("alias") or acct.get("id") or account_id
    except Exception:
        name = account_id
    _current_account_name = name
    save_account(account_id, name, api_key)
    return name


# Register current account on startup
try:
    from config import OANDA_API_KEY as _startup_key
    _fetch_and_save_account(_current_account_id, _startup_key)
except Exception:
    pass


def _apply_account(account_id, api_key=None):
    """Update all runtime objects to use a new account ID."""
    global _current_account_id, _current_account_name
    _current_account_id = account_id
    set_active_account(account_id)
    set_setting("account_id", account_id)
    client.account_id = account_id
    executor.client.account_id = account_id
    executor.risk.client.account_id = account_id
    used_key = api_key or get_account_api_key(account_id) or ""
    if used_key:
        from oandapyV20 import API as OandaAPI
        from config import OANDA_ENVIRONMENT
        env = "practice" if OANDA_ENVIRONMENT == "practice" else "live"
        new_api = OandaAPI(access_token=used_key, environment=env)
        client.client = new_api
        executor.client.client = new_api
        executor.risk.client.client = new_api
        _update_env("OANDA_API_KEY", used_key)
    _fetch_and_save_account(account_id, used_key)


# ── In-memory state (resets on server restart) ─────────────────────────────
_agent_paused    = False
_cycle_running   = False
_cycle_cancelled = False
_cycle_log       = []

# Auto-restore test mode if open test trades exist
_test_mode = bool(get_open_test_trades())

_test_params = {
    "bypass_hours":        True,
    "confidence_min":      0.60,
    "max_risk_pct":        1.0,
    "max_positions":       3,
    "max_daily_loss_pct":  3.0,
    "stop_pips":           20,
    "confluence_min":      0.60,
    "adx_threshold":       25,
    "mtf_daily_threshold": 0.30,
    "require_h1_confirm":  True,
    "take_profit_ratio":   2.0,
}


def _conn():
    return sqlite3.connect(DB_PATH)


def get_trade_history(limit=50):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE account_id=? ORDER BY opened DESC LIMIT ?",
            (_current_account_id, limit)
        ).fetchall()
    return rows


def get_account_snapshots(limit=48):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, balance, nav, open_pnl FROM account_snapshots WHERE account_id=? ORDER BY ts DESC LIMIT ?",
            (_current_account_id, limit)
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

    signals  = get_recent_signals(10)
    trades   = get_trade_history(20)
    snapshots = get_account_snapshots(48)

    test_pairs       = {t[3] for t in get_open_test_trades()}
    pnl_summary      = get_pnl_summary(account_id=_current_account_id)
    pnl_summary_all  = get_pnl_summary(account_id=None)
    saved_accounts   = get_saved_accounts()

    return render_template(
        "index.html",
        account=account,
        positions=positions,
        signals=signals,
        trades=trades,
        snapshots=snapshots,
        test_pairs=test_pairs,
        pnl_summary=pnl_summary,
        pnl_summary_all=pnl_summary_all,
        paused=_agent_paused,
        test_mode=_test_mode,
        test_params=_test_params,
        current_account_id=_current_account_id,
        current_account_name=_current_account_name,
        saved_accounts=saved_accounts,
    )


# ── API endpoints ──────────────────────────────────────────────────────────

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


@app.route("/api/switch-account", methods=["POST"])
def api_switch_account():
    """Validate and switch to a new OANDA account ID, optionally updating the API key."""
    data   = request.get_json()
    new_id = (data.get("account_id") or "").strip()
    new_key = (data.get("api_key") or "").strip() or None

    if not new_id:
        return jsonify({"error": "account_id is required"}), 400

    # Validate against OANDA before switching
    try:
        test_client = OandaClient()
        test_client.account_id = new_id
        if new_key:
            from oandapyV20 import API as OandaAPI
            from config import OANDA_ENVIRONMENT
            env = "practice" if OANDA_ENVIRONMENT == "practice" else "live"
            test_client.client = OandaAPI(access_token=new_key, environment=env)
        test_client.get_account()  # throws if invalid
    except Exception as e:
        return jsonify({"error": f"OANDA rejected: {e}"}), 422

    _apply_account(new_id, api_key=new_key)
    return jsonify({"status": "switched", "account_id": new_id, "account_name": _current_account_name})


def _update_env(key, value):
    """Update a single key in the .env file."""
    env_path = Path(__file__).parent.parent / ".env"
    lines = env_path.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    # Also update the live client objects immediately
    if key == "OANDA_API_KEY":
        from oandapyV20 import API as OandaAPI
        from config import OANDA_ENVIRONMENT
        env = "practice" if OANDA_ENVIRONMENT == "practice" else "live"
        new_api = OandaAPI(access_token=value, environment=env)
        client.client = new_api
        executor.client.client = new_api
        executor.risk.client.client = new_api


@app.route("/api/saved-accounts")
def api_saved_accounts():
    rows = get_saved_accounts()
    return jsonify([
        {"account_id": r[0], "account_name": r[1], "last_used": r[2]}
        for r in rows
    ])


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


def _extract_fill(resp):
    """Pull close_price and pnl from an OANDA PositionClose response."""
    fill = resp.get("longOrderFillTransaction") or resp.get("shortOrderFillTransaction") or {}
    try:
        close_price = float(fill.get("price", 0)) or None
        pnl         = float(fill.get("pl", 0)) if fill.get("pl") is not None else None
    except (TypeError, ValueError):
        close_price, pnl = None, None
    return close_price, pnl


@app.route("/api/close-all", methods=["POST"])
def api_close_all():
    global _cycle_cancelled
    _cycle_cancelled = True
    results = executor.close_all_positions()
    with _conn() as conn:
        conn.execute(
            "UPDATE trades SET status='closed', closed=datetime('now') WHERE status='open' AND account_id=?",
            (_current_account_id,)
        )
        conn.commit()
    return jsonify({"results": results})


@app.route("/api/close/<instrument>", methods=["POST"])
def api_close_pair(instrument):
    global _cycle_cancelled
    _cycle_cancelled = True
    try:
        resp = client.close_position(instrument)
        close_price, pnl = _extract_fill(resp)

        with _conn() as conn:
            conn.execute(
                """UPDATE trades
                   SET status='closed', closed=datetime('now'), close_price=?, pnl=?
                   WHERE pair=? AND status='open' AND account_id=?""",
                (close_price, pnl, instrument, _current_account_id)
            )
            conn.commit()

        return jsonify({"status": "closed", "instrument": instrument, "pnl": pnl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/paused")
def api_paused():
    return jsonify({"paused": _agent_paused})


@app.route("/api/test-mode", methods=["GET", "POST"])
def api_test_mode():
    global _test_mode, _test_params
    if request.method == "POST":
        data = request.get_json()
        if "enabled" in data:
            open_test = get_open_test_trades()
            if open_test:
                return jsonify({
                    "status": "blocked",
                    "message": f"Cannot change mode — {len(open_test)} test trade(s) still open. Close them first."
                }), 409
            _test_mode = bool(data["enabled"])
        if "params" in data:
            _test_params.update(data["params"])
    return jsonify({"test_mode": _test_mode, "params": _test_params})


@app.route("/api/run-cycle", methods=["POST"])
def api_run_cycle():
    global _cycle_running, _cycle_log

    if _cycle_running:
        return jsonify({"status": "already_running"})

    if _agent_paused:
        return jsonify({"status": "paused", "message": "Agent is paused — resume it first"})

    active_test_mode = _test_mode
    active_params = {**_test_params, "is_test": True} if _test_mode else None

    _ANALYSIS_KEYS = {"confluence_min", "adx_threshold", "mtf_daily_threshold", "require_h1_confirm"}
    active_analysis_params = (
        {k: v for k, v in _test_params.items() if k in _ANALYSIS_KEYS}
        if _test_mode else None
    )

    def run():
        global _cycle_running, _cycle_cancelled, _cycle_log
        from config import PAIRS
        from agent.claude_agent import analyze

        _cycle_running = True
        _cycle_cancelled = False
        _cycle_log = []

        try:
            state = executor.snapshot_account()
            _cycle_log.append(f"Account: ${state['balance']:,.2f} balance | ${state['open_pnl']:,.2f} open P&L")

            if active_test_mode:
                _cycle_log.append("🧪 TEST MODE — hours bypassed, custom risk params active")
            else:
                mkt_ok, mkt_msg = executor.risk.check_market_hours()
                if not mkt_ok:
                    _cycle_log.append(f"⚠ {mkt_msg} — signals will generate but no orders will be placed.")

            all_signals = {}
            for pair in PAIRS:
                if _cycle_cancelled:
                    _cycle_log.append("Cycle cancelled.")
                    break

                _cycle_log.append(f"Analyzing {pair}...")
                try:
                    thesis = analyze(pair, all_signals=all_signals, params=active_analysis_params)
                    direction  = thesis.get("direction")
                    confidence = thesis.get("confidence", 0)
                    score      = thesis.get("confluence_score", "—")
                    regime     = thesis.get("regime", "")
                    corr_warn  = thesis.get("correlation_warning")

                    _cycle_log.append(f"{pair}: {direction} @ {confidence:.0%} (score: {score}, {regime})")
                    if corr_warn:
                        _cycle_log.append(f"  ⚠ {corr_warn}")

                    if direction not in ("NO_TRADE", "ERROR"):
                        result = executor.execute(thesis, params=active_params)
                        status = result.get("status")
                        detail = result.get("error") or result.get("reason") or ""
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
    return jsonify({"running": _cycle_running, "log": _cycle_log})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
