"""
Flask dashboard — view account state, signals, trades, and control the agent.
Run with: python -m dashboard.app
"""
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, flash
import threading
import os
import secrets
import bcrypt
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from config import OANDA_ACCOUNT_ID
from data.oanda_client import OandaClient
from data import store
from data.store import (
    _conn,
    get_recent_signals, get_open_trades, get_open_test_trades,
    get_pnl_summary, get_pip_summary, get_setting, set_setting, set_active_account, init_db,
    save_account, get_saved_accounts, get_account_api_key, reconcile_trades,
    create_user, get_user_by_email, get_user_by_id, touch_last_login,
    link_user_account, get_user_accounts, user_owns_account,
)
from execution.executor import Executor
from agent.feedback import (
    run_feedback_analysis, get_pending_suggestions,
    apply_suggestion, dismiss_suggestion, get_latest_session
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


def _current_user():
    """Return the logged-in user id or None."""
    return session.get("user_id")


def _require_auth():
    if not _current_user():
        if request.path.startswith("/api/"):
            from flask import abort
            abort(401)
        return redirect(url_for("login"))
    return None

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
    # Link the current logged-in user to this account
    user_id = session.get("user_id")
    if user_id:
        link_user_account(user_id, account_id)


# ── In-memory state (resets on server restart) ─────────────────────────────
_agent_paused    = False
_cycle_running   = False
_cycle_cancelled = False
_cycle_log       = []
_cycle_source    = "manual"   # "manual" | "auto"

# ── Scheduler state ────────────────────────────────────────────────────────
_scheduler_enabled  = False        # off by default — user must enable
_schedule_interval  = 30           # minutes between auto-cycles
_next_run_utc       = None         # datetime of next scheduled cycle

# Auto-restore test mode if open test trades exist
_test_mode = bool(get_open_test_trades())

_test_params = {
    "bypass_hours":        True,
    "confidence_min":      0.55,
    "max_risk_pct":        1.0,
    "max_positions":       3,
    "max_daily_loss_pct":  3.0,
    "stop_pips":           15,
    "confluence_min":      0.60,
    "adx_threshold":       25,
    "mtf_daily_threshold": 0.30,
    "require_h1_confirm":  True,
    "take_profit_ratio":   3.0,
    "trailing_stop":       False,
    "trailing_stop_pips":  30,
    "atr_multiplier":      1.5,
    "use_atr_stops":       True,
}


def get_trade_history(limit=50):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE account_id=%s ORDER BY opened DESC LIMIT %s",
            (_current_account_id, limit)
        ).fetchall()
    from data.store import _str_row
    return [_str_row(r) for r in rows]


def get_account_snapshots(limit=48):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, balance, nav, open_pnl FROM account_snapshots WHERE account_id=%s ORDER BY ts DESC LIMIT %s",
            (_current_account_id, limit)
        ).fetchall()
    from data.store import _str_row
    return list(reversed([_str_row(r) for r in rows]))


# ── Auth gate ──────────────────────────────────────────────────────────────

@app.before_request
def require_login():
    public = {"/login", "/logout", "/register"}
    if request.path in public:
        return None
    return _require_auth()


# ── Auth routes ────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if _current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_email(email)
        if user and bcrypt.checkpw(password.encode(), user[5].encode()):
            session["user_id"]   = user[0]
            session["user_name"] = user[1]
            touch_last_login(user[0])
            # Auto-link current active account to this user if not already linked
            if _current_account_id:
                link_user_account(user[0], _current_account_id)
            return redirect(url_for("index"))
        error = "Incorrect email or password."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if _current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        first  = request.form.get("first_name", "").strip()
        last   = request.form.get("last_name", "").strip()
        email  = request.form.get("email", "").strip()
        phone  = request.form.get("phone", "").strip()
        pw     = request.form.get("password", "")
        pw2    = request.form.get("password2", "")

        if not all([first, last, email, pw]):
            error = "First name, last name, email and password are required."
        elif pw != pw2:
            error = "Passwords do not match."
        elif len(pw) < 8:
            error = "Password must be at least 8 characters."
        else:
            try:
                pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
                user_id = create_user(first, last, email, phone, pw_hash)
                session["user_id"]   = user_id
                session["user_name"] = first
                # Auto-link current active account to new user
                if _current_account_id:
                    link_user_account(user_id, _current_account_id)
                return redirect(url_for("index"))
            except Exception as e:
                if "unique" in str(e).lower():
                    error = "An account with that email already exists."
                else:
                    error = "Registration failed — please try again."
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    pip_summary      = get_pip_summary(account_id=_current_account_id)
    pip_summary_all  = get_pip_summary(account_id=None)
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
        pip_summary=pip_summary,
        pip_summary_all=pip_summary_all,
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
    data    = request.get_json()
    new_id  = (data.get("account_id") or "").strip()
    new_key = (data.get("api_key") or "").strip() or None
    user_id = session.get("user_id")

    if not new_id:
        return jsonify({"error": "account_id is required"}), 400

    # If the account already exists, verify the requesting user owns it
    if user_id and not new_key and user_owns_account.__module__:
        if not user_owns_account(user_id, new_id):
            # Account exists but belongs to someone else — block unless a new key is provided
            existing_key = get_account_api_key(new_id)
            if existing_key and not new_key:
                return jsonify({"error": "You do not have access to that account."}), 403

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
    user_id = session.get("user_id")
    if user_id:
        rows = get_user_accounts(user_id)
    else:
        rows = get_saved_accounts()
    return jsonify([
        {"account_id": r[0], "account_name": r[1], "last_used": str(r[2])[:19] if r[2] else None}
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
    try:
        open_positions = client.get_open_positions()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    for pos in open_positions:
        pair = pos["instrument"]
        try:
            resp = client.close_position(pair)
            close_price, pnl = _extract_fill(resp)
            with _conn() as conn:
                conn.execute(
                    """UPDATE trades
                       SET status='closed', closed=NOW(), close_price=%s, pnl=%s
                       WHERE pair=%s AND status='open' AND account_id=%s""",
                    (close_price, pnl, pair, _current_account_id)
                )
                conn.commit()
            results.append({"pair": pair, "status": "closed", "pnl": pnl})
        except Exception as e:
            results.append({"pair": pair, "status": "error", "error": str(e)})

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
                   SET status='closed', closed=NOW(), close_price=%s, pnl=%s
                   WHERE pair=%s AND status='open' AND account_id=%s""",
                (close_price, pnl, instrument, _current_account_id)
            )
            conn.commit()

        return jsonify({"status": "closed", "instrument": instrument, "pnl": pnl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync-trades", methods=["POST"])
def api_sync_trades():
    """Reconcile DB open trades against OANDA's closed trade list."""
    try:
        oanda_closed = client.get_closed_trades(count=50)
        updated = reconcile_trades(oanda_closed, _current_account_id)
        return jsonify({"status": "ok", "updated": updated})
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
        global _cycle_running, _cycle_cancelled, _cycle_log, _cycle_source
        from config import PAIRS
        from agent.claude_agent import analyze

        _cycle_running = True
        _cycle_cancelled = False
        _cycle_source = "manual"
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

            # Fetch open positions once — skip pairs already held (saves Claude API calls)
            try:
                _open_positions = client.get_open_positions()
                open_pairs = {pos["instrument"] for pos in _open_positions}
            except Exception:
                open_pairs = set()

            all_signals = {}
            for pair in PAIRS:
                if _cycle_cancelled:
                    _cycle_log.append("Cycle cancelled.")
                    break

                if pair in open_pairs:
                    _cycle_log.append(f"{pair}: skipped — position already open")
                    continue

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

            # Reconcile any trades closed by SL/TP while cycle ran
            try:
                oanda_closed = client.get_closed_trades(count=50)
                updated = reconcile_trades(oanda_closed, _current_account_id)
                if updated:
                    _cycle_log.append(f"Synced {updated} trade(s) closed by SL/TP.")
            except Exception as e:
                _cycle_log.append(f"Sync warning: {e}")
        finally:
            _cycle_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/cycle-status")
def api_cycle_status():
    next_run_iso = _next_run_utc.isoformat() if _next_run_utc else None
    return jsonify({
        "running":            _cycle_running,
        "source":             _cycle_source,
        "log":                _cycle_log,
        "scheduler_enabled":  _scheduler_enabled,
        "schedule_interval":  _schedule_interval,
        "next_run_utc":       next_run_iso,
        "last_sync_utc":      _last_sync_utc,
        "last_sync_updated":  _last_sync_updated,
    })


# ── Feedback loop ─────────────────────────────────────────────────────────

@app.route("/api/feedback/run", methods=["POST"])
def api_feedback_run():
    """Trigger a Claude feedback analysis on closed trade history."""
    try:
        result = run_feedback_analysis(
            account_id=_current_account_id,
            current_params=_test_params
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/feedback/suggestions")
def api_feedback_suggestions():
    rows = get_pending_suggestions(account_id=_current_account_id)
    return jsonify([
        {
            "id":              r[0],
            "param":           r[1],
            "current_value":   r[2],
            "suggested_value": r[3],
            "rationale":       r[4],
            "confidence":      r[5],
            "pair_specific":   r[6],
            "created":         r[7],
            "session_summary": r[8],
        }
        for r in rows
    ])


@app.route("/api/feedback/apply/<int:suggestion_id>", methods=["POST"])
def api_feedback_apply(suggestion_id):
    """Apply a suggestion — updates _test_params live."""
    global _test_params
    rows = get_pending_suggestions(account_id=_current_account_id)
    match = next((r for r in rows if r[0] == suggestion_id), None)
    if not match:
        return jsonify({"error": "Suggestion not found"}), 404

    param = match[1]
    raw   = match[3]

    # Coerce to correct type
    try:
        if raw.lower() in ("true", "false"):
            value = raw.lower() == "true"
        elif "." in raw:
            value = float(raw)
        else:
            value = int(raw)
    except ValueError:
        value = raw

    if param in _test_params:
        _test_params[param] = value

    apply_suggestion(suggestion_id)
    return jsonify({"status": "applied", "param": param, "value": value})


@app.route("/api/feedback/dismiss/<int:suggestion_id>", methods=["POST"])
def api_feedback_dismiss(suggestion_id):
    dismiss_suggestion(suggestion_id)
    return jsonify({"status": "dismissed"})


@app.route("/api/feedback/latest")
def api_feedback_latest():
    session = get_latest_session(account_id=_current_account_id)
    if not session:
        return jsonify({"status": "none"})
    return jsonify(session)


# ── Scheduler ──────────────────────────────────────────────────────────────

def _scheduled_cycle():
    """Called by APScheduler. Fires a cycle if conditions are met."""
    global _next_run_utc
    _next_run_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Reschedule next run immediately so UI always shows upcoming time
    _update_next_run()

    if _cycle_running or _agent_paused:
        return

    # Kick off via the same thread pattern as api_run_cycle
    active_test_mode = _test_mode
    active_params = {**_test_params, "is_test": True} if _test_mode else None
    _ANALYSIS_KEYS = {"confluence_min", "adx_threshold", "mtf_daily_threshold", "require_h1_confirm"}
    active_analysis_params = (
        {k: v for k, v in _test_params.items() if k in _ANALYSIS_KEYS}
        if _test_mode else None
    )

    def run():
        global _cycle_running, _cycle_cancelled, _cycle_log, _cycle_source
        from config import PAIRS
        from agent.claude_agent import analyze

        _cycle_running = True
        _cycle_cancelled = False
        _cycle_source = "auto"
        _cycle_log = []

        try:
            state = executor.snapshot_account()
            _cycle_log.append(f"[AUTO] Account: ${state['balance']:,.2f} balance | ${state['open_pnl']:,.2f} open P&L")

            if active_test_mode:
                _cycle_log.append("🧪 TEST MODE — hours bypassed, custom risk params active")
            else:
                mkt_ok, mkt_msg = executor.risk.check_market_hours()
                if not mkt_ok:
                    _cycle_log.append(f"⚠ {mkt_msg} — signals will generate but no orders placed.")

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

            try:
                oanda_closed = client.get_closed_trades(count=50)
                updated = reconcile_trades(oanda_closed, _current_account_id)
                if updated:
                    _cycle_log.append(f"Synced {updated} trade(s) closed by SL/TP.")
            except Exception as e:
                _cycle_log.append(f"Sync warning: {e}")
        finally:
            _cycle_running = False

    threading.Thread(target=run, daemon=True).start()


def _update_next_run():
    """Recalculate _next_run_utc based on current interval."""
    global _next_run_utc
    from datetime import timedelta
    _next_run_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0) + \
                    timedelta(minutes=_schedule_interval)


_last_sync_utc    = None   # timestamp of last successful background sync
_last_sync_updated = 0     # number of trades reconciled in last sync


def _background_sync():
    """Runs every 2 minutes — reconciles any trades closed by SL/TP on OANDA."""
    global _last_sync_utc, _last_sync_updated
    try:
        oanda_closed = client.get_closed_trades(count=50)
        updated = reconcile_trades(oanda_closed, _current_account_id)
        _last_sync_utc    = datetime.now(timezone.utc).strftime("%H:%M UTC")
        _last_sync_updated = updated
        if updated:
            print(f"[SYNC] Reconciled {updated} trade(s) closed by SL/TP")
    except Exception as e:
        print(f"[SYNC] Error: {e}")


def _scheduler_error_listener(event):
    """Log APScheduler job failures — prevents silent failures in background jobs."""
    if hasattr(event, "exception") and event.exception:
        print(f"[SCHEDULER ERROR] Job '{event.job_id}' failed: {event.exception}")
        import traceback
        traceback.print_tb(event.traceback)

_scheduler = BackgroundScheduler(timezone="UTC", misfire_grace_time=60)
_scheduler.add_listener(_scheduler_error_listener, mask=0x8000)  # EVENT_JOB_ERROR
_scheduler.add_job(_background_sync, "interval", minutes=2, id="background_sync",
                   max_instances=1, coalesce=True)
_scheduler.start()


@app.route("/api/scheduler", methods=["GET", "POST"])
def api_scheduler():
    global _scheduler_enabled, _schedule_interval, _next_run_utc

    if request.method == "POST":
        data = request.get_json()

        if "interval" in data:
            new_interval = int(data["interval"])
            if new_interval < 5:
                return jsonify({"error": "Minimum interval is 5 minutes"}), 400
            _schedule_interval = new_interval

        if "enabled" in data:
            _scheduler_enabled = bool(data["enabled"])

            if _scheduler_enabled:
                # Remove only the auto_cycle job, keep background_sync running
                if _scheduler.get_job("auto_cycle"):
                    _scheduler.remove_job("auto_cycle")
                _scheduler.add_job(
                    _scheduled_cycle,
                    "interval",
                    minutes=_schedule_interval,
                    id="auto_cycle",
                    next_run_time=datetime.now(timezone.utc),
                    max_instances=1,
                    coalesce=True,
                )
                _update_next_run()
            else:
                if _scheduler.get_job("auto_cycle"):
                    _scheduler.remove_job("auto_cycle")
                _next_run_utc = None

    next_run_iso = _next_run_utc.isoformat() if _next_run_utc else None
    return jsonify({
        "enabled":  _scheduler_enabled,
        "interval": _schedule_interval,
        "next_run": next_run_iso,
    })


@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/backtest-all", methods=["POST"])
def api_backtest_all():
    """
    Run a full parameter sweep across all pairs and 3 configs.
    Returns ranked results + a plain-English summary.
    """
    data  = request.get_json()
    start = data.get("start", "")
    end   = data.get("end", "")
    balance  = float(data.get("initial_balance", 10000))
    risk_pct = float(data.get("risk_pct", 1.0))

    if not start or not end:
        return jsonify({"error": "start and end dates are required"}), 400

    from analysis.backtest import run_backtest
    from config import PAIRS

    CONFIGS = [
        {"label": "H4 · 15pip · 3:1",  "granularity": "H4", "stop_pips": 15, "take_profit_ratio": 3.0, "confidence_min": 0.55, "confluence_min": 0.60},
        {"label": "H4 · 20pip · 2:1",  "granularity": "H4", "stop_pips": 20, "take_profit_ratio": 2.0, "confidence_min": 0.55, "confluence_min": 0.60},
        {"label": "H1 · 20pip · 2:1",  "granularity": "H1", "stop_pips": 20, "take_profit_ratio": 2.0, "confidence_min": 0.55, "confluence_min": 0.60},
    ]

    results = []
    errors  = []

    for pair in PAIRS:
        for cfg in CONFIGS:
            try:
                r = run_backtest(
                    pair=pair,
                    start=start,
                    end=end,
                    granularity=cfg["granularity"],
                    stop_pips=cfg["stop_pips"],
                    take_profit_ratio=cfg["take_profit_ratio"],
                    confidence_min=cfg["confidence_min"],
                    confluence_min=cfg["confluence_min"],
                    initial_balance=balance,
                    risk_pct=risk_pct,
                )
                s = r["summary"]
                results.append({
                    "pair":         pair,
                    "config":       cfg["label"],
                    "granularity":  cfg["granularity"],
                    "stop_pips":    cfg["stop_pips"],
                    "tp_ratio":     cfg["take_profit_ratio"],
                    "total_trades": s["total_trades"],
                    "win_rate":     s["win_rate"],
                    "total_pnl":    s["total_pnl"],
                    "return_pct":   s["return_pct"],
                    "max_drawdown": s["max_drawdown"],
                    "sharpe":       s["sharpe"],
                    "avg_win":      s["avg_win"],
                    "avg_loss":     s["avg_loss"],
                    "equity_curve": r["equity_curve"],
                })
            except Exception as e:
                errors.append(f"{pair} {cfg['label']}: {e}")

    if not results:
        return jsonify({"error": "All backtests failed", "details": errors}), 500

    # Sort by Sharpe descending
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    # Generate plain-English summary
    best    = results[0]
    winners = [r for r in results if r["total_pnl"] > 0]
    losers  = [r for r in results if r["total_pnl"] <= 0]
    no_trades = [r for r in results if r["total_trades"] == 0]

    best_pairs = {}
    for r in results:
        if r["total_pnl"] > 0:
            if r["pair"] not in best_pairs or r["sharpe"] > best_pairs[r["pair"]]["sharpe"]:
                best_pairs[r["pair"]] = r

    top_pair = max(best_pairs.values(), key=lambda x: x["sharpe"]) if best_pairs else None

    summary_lines = []
    summary_lines.append(
        f"Across {len(PAIRS)} pairs and {len(CONFIGS)} parameter configurations ({len(results)} total runs), "
        f"{len(winners)} were profitable and {len(losers)} were not."
    )

    if top_pair:
        summary_lines.append(
            f"The strongest result was {top_pair['pair']} on {top_pair['config']} — "
            f"{top_pair['win_rate']}% win rate, +{top_pair['return_pct']}% return, "
            f"Sharpe {top_pair['sharpe']}. This pair/config combination has the best risk-adjusted edge in this period."
        )

    if no_trades:
        nt_pairs = list({r["pair"] for r in no_trades})
        summary_lines.append(
            f"{', '.join(nt_pairs)} produced no signals at these confidence thresholds — "
            f"the signal engine found no qualifying setups in those markets during this period."
        )

    h4_results  = [r for r in results if r["granularity"] == "H4" and r["total_trades"] > 0]
    h1_results  = [r for r in results if r["granularity"] == "H1" and r["total_trades"] > 0]
    avg_h4_sharpe = sum(r["sharpe"] for r in h4_results) / len(h4_results) if h4_results else 0
    avg_h1_sharpe = sum(r["sharpe"] for r in h1_results) / len(h1_results) if h1_results else 0

    if h4_results and h1_results:
        if avg_h4_sharpe > avg_h1_sharpe:
            summary_lines.append(
                f"H4 timeframe outperformed H1 on average (Sharpe {avg_h4_sharpe:.2f} vs {avg_h1_sharpe:.2f}), "
                f"suggesting the signal engine performs better on slower, higher-quality setups."
            )
        else:
            summary_lines.append(
                f"H1 timeframe outperformed H4 on average (Sharpe {avg_h1_sharpe:.2f} vs {avg_h4_sharpe:.2f}), "
                f"suggesting more frequent signals are working better in this period."
            )

    return jsonify({
        "results":  results,
        "summary":  " ".join(summary_lines),
        "errors":   errors,
        "start":    start,
        "end":      end,
    })


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    data = request.get_json()
    pair       = data.get("pair", "EUR_USD")
    start      = data.get("start", "")
    end        = data.get("end", "")
    granularity = data.get("granularity", "H1")
    stop_pips   = float(data.get("stop_pips", 20))
    tp_ratio    = float(data.get("take_profit_ratio", 2.0))
    conf_min    = float(data.get("confidence_min", 0.55))
    confl_min   = float(data.get("confluence_min", 0.55))
    balance     = float(data.get("initial_balance", 10000))
    risk_pct    = float(data.get("risk_pct", 1.0))

    if not start or not end:
        return jsonify({"error": "start and end dates are required"}), 400

    try:
        from analysis.backtest import run_backtest
        result = run_backtest(
            pair=pair,
            start=start,
            end=end,
            granularity=granularity,
            stop_pips=stop_pips,
            take_profit_ratio=tp_ratio,
            confidence_min=conf_min,
            confluence_min=confl_min,
            initial_balance=balance,
            risk_pct=risk_pct,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
