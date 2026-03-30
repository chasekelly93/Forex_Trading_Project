"""
SQLite store — persists signals, trades, and account snapshots.
All reads/writes are automatically scoped to the active account.
Call set_active_account() before any other function.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "forex_agent.db"

# Active account — set via set_active_account() on startup and when switching
_active_account_id = None


def set_active_account(account_id):
    global _active_account_id
    _active_account_id = account_id


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id   TEXT PRIMARY KEY,
                account_name TEXT,
                api_key      TEXT,
                last_used    TEXT,
                user_id      TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created    TEXT NOT NULL,
                pair       TEXT NOT NULL,
                timeframe  TEXT NOT NULL,
                direction  TEXT NOT NULL,
                confidence REAL,
                reasoning  TEXT,
                acted_on   INTEGER DEFAULT 0,
                account_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                opened      TEXT NOT NULL,
                closed      TEXT,
                pair        TEXT NOT NULL,
                direction   TEXT NOT NULL,
                units       INTEGER NOT NULL,
                open_price  REAL,
                close_price REAL,
                pnl         REAL,
                status      TEXT DEFAULT 'open',
                signal_id   INTEGER,
                is_test     INTEGER DEFAULT 0,
                account_id  TEXT,
                sl_price    REAL,
                tp_price    REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                balance    REAL,
                nav        REAL,
                open_pnl   REAL,
                account_id TEXT
            )
        """)

        # ── Migrations ────────────────────────────────────────────────────────
        for table in ("trades", "signals", "account_snapshots"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

            if table == "trades":
                if "is_test" not in cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN is_test INTEGER DEFAULT 0")
                if "sl_price" not in cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN sl_price REAL")
                if "tp_price" not in cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN tp_price REAL")

            if "account_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id TEXT")
                if _active_account_id:
                    conn.execute(
                        f"UPDATE {table} SET account_id=? WHERE account_id IS NULL",
                        (_active_account_id,)
                    )

        conn.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
        )
        conn.commit()


# ── Writes ────────────────────────────────────────────────────────────────────

def save_signal(pair, timeframe, direction, confidence, reasoning):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO signals
               (created, pair, timeframe, direction, confidence, reasoning, account_id)
               VALUES (?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), pair, timeframe, direction,
             confidence, reasoning, _active_account_id)
        )
        conn.commit()


def save_trade(pair, direction, units, open_price, signal_id=None, is_test=False,
               sl_price=None, tp_price=None):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (opened, pair, direction, units, open_price, signal_id, is_test,
                account_id, sl_price, tp_price)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), pair, direction, units,
             open_price, signal_id, int(is_test),
             _active_account_id, sl_price, tp_price)
        )
        conn.commit()


def close_trade(trade_id, close_price, pnl):
    with _conn() as conn:
        conn.execute(
            "UPDATE trades SET closed=?, close_price=?, pnl=?, status='closed' WHERE id=?",
            (datetime.utcnow().isoformat(), close_price, pnl, trade_id)
        )
        conn.commit()


def save_snapshot(balance, nav, open_pnl):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO account_snapshots (ts, balance, nav, open_pnl, account_id) VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), balance, nav, open_pnl, _active_account_id)
        )
        conn.commit()


# ── Reads (scoped to active account) ─────────────────────────────────────────

def get_recent_signals(limit=20):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE account_id=? ORDER BY created DESC LIMIT ?",
            (_active_account_id, limit)
        ).fetchall()


def get_open_trades():
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE status='open' AND account_id=?",
            (_active_account_id,)
        ).fetchall()


def get_open_test_trades():
    """Returns open test trades for the active account."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE status='open' AND is_test=1 AND account_id=?",
            (_active_account_id,)
        ).fetchall()


def save_account(account_id, account_name, api_key, user_id=None):
    """Upsert a known account. Called on every successful connect/switch."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO accounts (account_id, account_name, api_key, last_used, user_id)
            VALUES (?, ?, ?, datetime('now'), ?)
            ON CONFLICT(account_id) DO UPDATE SET
                account_name = excluded.account_name,
                api_key      = excluded.api_key,
                last_used    = datetime('now')
        """, (account_id, account_name, api_key, user_id))
        conn.commit()


def get_saved_accounts():
    """Return all known accounts ordered by most recently used."""
    with _conn() as conn:
        return conn.execute(
            "SELECT account_id, account_name, last_used FROM accounts ORDER BY last_used DESC"
        ).fetchall()


def get_account_api_key(account_id):
    """Return the stored API key for a known account."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT api_key FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
    return row[0] if row else None


def get_pnl_summary(account_id=None):
    """
    Returns realized P&L totals.
    account_id=<id>  → scoped to that account
    account_id=None  → all accounts combined
    """
    if account_id:
        where = "WHERE status='closed' AND pnl IS NOT NULL AND account_id=?"
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
