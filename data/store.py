"""
Supabase (Postgres) store — persists signals, trades, and account snapshots.
All reads/writes are automatically scoped to the active account.
Call set_active_account() before any other function.

Schema is managed in Supabase — run schema.sql once to create all tables.
"""
import os
import psycopg2
import psycopg2.pool
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.environ["DATABASE_URL"],
)

# Active account — set via set_active_account() on startup and when switching
_active_account_id = None


def set_active_account(account_id):
    global _active_account_id
    _active_account_id = account_id


class _conn:
    """
    Context manager — checks out a pooled Postgres connection.
    Rolls back on exception; caller is responsible for explicit commit().
    """
    def __enter__(self):
        self._raw = _pool.getconn()
        self._cur = self._raw.cursor()
        return self

    def execute(self, sql, params=()):
        self._cur.execute(sql, params)
        return self._cur

    def commit(self):
        self._raw.commit()

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._raw.rollback()
        _pool.putconn(self._raw)


def init_db():
    """Verify DB connection is healthy. Schema is managed in Supabase."""
    with _conn() as conn:
        conn.execute("SELECT 1")


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key, default=None):
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=%s", (key,)
        ).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (key, value)
        )
        conn.commit()


# ── Writes ────────────────────────────────────────────────────────────────────

def save_signal(pair, timeframe, direction, confidence, reasoning):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO signals
               (created, pair, timeframe, direction, confidence, reasoning, account_id)
               VALUES (NOW(), %s, %s, %s, %s, %s, %s)""",
            (pair, timeframe, direction, confidence, reasoning, _active_account_id)
        )
        conn.commit()


def save_trade(pair, direction, units, open_price, signal_id=None, is_test=False,
               sl_price=None, tp_price=None):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (opened, pair, direction, units, open_price, signal_id, is_test,
                account_id, sl_price, tp_price)
               VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (pair, direction, units, open_price, signal_id, int(is_test),
             _active_account_id, sl_price, tp_price)
        )
        conn.commit()


def close_trade(trade_id, close_price, pnl):
    with _conn() as conn:
        conn.execute(
            "UPDATE trades SET closed=NOW(), close_price=%s, pnl=%s, status='closed' WHERE id=%s",
            (close_price, pnl, trade_id)
        )
        conn.commit()


def save_snapshot(balance, nav, open_pnl):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO account_snapshots (ts, balance, nav, open_pnl, account_id) VALUES (NOW(), %s, %s, %s, %s)",
            (balance, nav, open_pnl, _active_account_id)
        )
        conn.commit()


# ── Reads (scoped to active account) ─────────────────────────────────────────

def _str_row(row):
    """Convert any datetime values in a tuple to ISO strings."""
    return tuple(v.isoformat() if hasattr(v, 'isoformat') else v for v in row)


def get_recent_signals(limit=20):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE account_id=%s ORDER BY created DESC LIMIT %s",
            (_active_account_id, limit)
        ).fetchall()
    return [_str_row(r) for r in rows]


def get_open_trades():
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='open' AND account_id=%s",
            (_active_account_id,)
        ).fetchall()
    return [_str_row(r) for r in rows]


def get_open_test_trades():
    """Returns open test trades for the active account."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE status='open' AND is_test=1 AND account_id=%s",
            (_active_account_id,)
        ).fetchall()


def save_account(account_id, account_name, api_key, user_id=None):
    """Upsert a known account. Called on every successful connect/switch."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO accounts (account_id, account_name, api_key, last_used, user_id)
               VALUES (%s, %s, %s, NOW(), %s)
               ON CONFLICT (account_id) DO UPDATE SET
                   account_name = EXCLUDED.account_name,
                   api_key      = EXCLUDED.api_key,
                   last_used    = NOW()""",
            (account_id, account_name, api_key, user_id)
        )
        conn.commit()


def get_saved_accounts():
    """Return all known accounts ordered by most recently used."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT account_id, account_name, last_used FROM accounts ORDER BY last_used DESC"
        ).fetchall()
    # Normalize last_used to string (Postgres returns datetime, SQLite returned str)
    return [(r[0], r[1], str(r[2])[:19] if r[2] else None) for r in rows]


def get_account_api_key(account_id):
    """Return the stored API key for a known account."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT api_key FROM accounts WHERE account_id=%s", (account_id,)
        ).fetchone()
    return row[0] if row else None


def get_pnl_summary(account_id=None):
    """
    Returns realized P&L totals.
    account_id=<id>  → scoped to that account
    account_id=None  → all accounts combined
    """
    if account_id:
        where = "WHERE status='closed' AND pnl IS NOT NULL AND account_id=%s"
        params = (account_id,)
    else:
        where = "WHERE status='closed' AND pnl IS NOT NULL"
        params = ()

    with _conn() as conn:
        row = conn.execute(f"""
            SELECT
                COALESCE(SUM(pnl), 0)                               AS total_pnl,
                COALESCE(SUM(CASE WHEN is_test=0 THEN pnl END), 0)  AS live_pnl,
                COALESCE(SUM(CASE WHEN is_test=1 THEN pnl END), 0)  AS test_pnl,
                COUNT(*)                                             AS closed_count,
                COUNT(CASE WHEN pnl > 0 THEN 1 END)                 AS wins
            FROM trades
            {where}
        """, params).fetchone()

    return {
        "total_pnl":    round(row[0], 2),
        "live_pnl":     round(row[1], 2),
        "test_pnl":     round(row[2], 2),
        "closed_count": row[3],
        "win_rate":     round(row[4] / row[3] * 100, 1) if row[3] else 0,
    }


_PIP_SIZE = {
    "USD_JPY": 0.01, "EUR_JPY": 0.01, "GBP_JPY": 0.01,
    "AUD_JPY": 0.01, "CAD_JPY": 0.01, "CHF_JPY": 0.01, "NZD_JPY": 0.01,
    "XAU_USD": 0.1,
}
_DEFAULT_PIP = 0.0001


def get_pip_summary(account_id=None):
    """
    Returns total pips won/lost across closed trades.
    Pips = (close_price - open_price) / pip_size for BUY,
           (open_price - close_price) / pip_size for SELL.
    account_id=<id> → scoped; None → all accounts.
    """
    if account_id:
        where = "WHERE status='closed' AND close_price IS NOT NULL AND open_price IS NOT NULL AND account_id=%s"
        params = (account_id,)
    else:
        where = "WHERE status='closed' AND close_price IS NOT NULL AND open_price IS NOT NULL"
        params = ()

    with _conn() as conn:
        rows = conn.execute(
            f"SELECT pair, direction, open_price, close_price, is_test FROM trades {where}",
            params
        ).fetchall()

    total_pips = 0.0
    live_pips  = 0.0
    test_pips  = 0.0

    for pair, direction, open_price, close_price, is_test in rows:
        pip = _PIP_SIZE.get(pair, _DEFAULT_PIP)
        raw = (close_price - open_price) if direction == "BUY" else (open_price - close_price)
        pips = round(raw / pip, 1)
        total_pips += pips
        if is_test:
            test_pips += pips
        else:
            live_pips += pips

    return {
        "total_pips": round(total_pips, 1),
        "live_pips":  round(live_pips, 1),
        "test_pips":  round(test_pips, 1),
    }


def get_account_snapshot_24h_ago():
    """Return the account snapshot closest to 24 hours ago for rolling drawdown calculation."""
    with _conn() as conn:
        try:
            row = conn.execute("""
                SELECT balance FROM account_snapshots
                WHERE account_id=%s
                  AND ts <= NOW() - INTERVAL '23 hours'
                ORDER BY ts DESC LIMIT 1
            """, (_active_account_id,)).fetchone()
            return {"balance": row[0]} if row else None
        except Exception:
            return None


def get_rolling_performance(n=50):
    """
    Return win rate and avg win/loss from the last N closed trades.
    Used for Kelly Criterion position sizing.
    """
    with _conn() as conn:
        try:
            rows = conn.execute("""
                SELECT pnl FROM trades
                WHERE status='closed' AND pnl IS NOT NULL AND account_id=%s
                ORDER BY closed DESC LIMIT %s
            """, (_active_account_id, n)).fetchall()
        except Exception:
            return None

    if not rows:
        return None

    pnls   = [r[0] for r in rows]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = len(pnls)

    return {
        "total":    total,
        "win_rate": len(wins) / total,
        "avg_win":  sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
    }


# ── Users ─────────────────────────────────────────────────────────────────────

def create_user(first_name, last_name, email, phone, password_hash):
    """Insert a new user. Raises psycopg2.errors.UniqueViolation if email taken."""
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO users (first_name, last_name, email, phone, password_hash)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (first_name, last_name, email.lower().strip(), phone, password_hash)
        ).fetchone()
        conn.commit()
    return row[0]


def get_user_by_email(email):
    with _conn() as conn:
        return conn.execute(
            "SELECT id, first_name, last_name, email, phone, password_hash FROM users WHERE email=%s",
            (email.lower().strip(),)
        ).fetchone()


def get_user_by_id(user_id):
    with _conn() as conn:
        return conn.execute(
            "SELECT id, first_name, last_name, email, phone FROM users WHERE id=%s",
            (user_id,)
        ).fetchone()


def touch_last_login(user_id):
    with _conn() as conn:
        conn.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user_id,))
        conn.commit()


# ── User ↔ Account linking ─────────────────────────────────────────────────

def link_user_account(user_id, account_id):
    """Associate a user with an OANDA account. Safe to call multiple times."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_accounts (user_id, account_id)
               VALUES (%s, %s)
               ON CONFLICT (user_id, account_id) DO NOTHING""",
            (user_id, account_id)
        )
        conn.commit()


def get_user_accounts(user_id):
    """Return all accounts a user has access to, most recently used first."""
    with _conn() as conn:
        return conn.execute(
            """SELECT a.account_id, a.account_name, a.last_used
               FROM accounts a
               JOIN user_accounts ua ON ua.account_id = a.account_id
               WHERE ua.user_id = %s
               ORDER BY a.last_used DESC NULLS LAST""",
            (user_id,)
        ).fetchall()


def user_owns_account(user_id, account_id):
    """Return True if this user has access to the given account."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_accounts WHERE user_id=%s AND account_id=%s",
            (user_id, account_id)
        ).fetchone()
    return row is not None


def reconcile_trades(oanda_closed_trades, account_id):
    """
    Cross-reference OANDA's closed trade list against our open DB rows.
    Returns the number of rows updated.
    """
    if not oanda_closed_trades:
        return 0

    updated = 0
    with _conn() as conn:
        open_rows = conn.execute(
            "SELECT id, pair, open_price, units FROM trades WHERE status='open' AND account_id=%s",
            (account_id,)
        ).fetchall()

        for row in open_rows:
            db_id, pair, open_price, units = row
            match = None
            for ct in oanda_closed_trades:
                if ct["instrument"] != pair:
                    continue
                if open_price and abs(ct["open_price"] - open_price) > 0.005:
                    continue
                match = ct
                break

            if match:
                conn.execute(
                    """UPDATE trades
                       SET status='closed', closed=%s, close_price=%s, pnl=%s
                       WHERE id=%s""",
                    (match["close_time"][:19] if match["close_time"] else None,
                     match["close_price"],
                     match["pnl"],
                     db_id)
                )
                updated += 1

        conn.commit()
    return updated
