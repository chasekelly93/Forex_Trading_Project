"""
SQLite store — persists signals, trades, and account snapshots.
This is what the dashboard reads from.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "forex_agent.db"


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                created   TEXT NOT NULL,
                pair      TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence REAL,
                reasoning TEXT,
                acted_on  INTEGER DEFAULT 0
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
                is_test     INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                balance  REAL,
                nav      REAL,
                open_pnl REAL
            )
        """)

        # Migrate existing trades table if is_test column doesn't exist
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "is_test" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN is_test INTEGER DEFAULT 0")

        conn.commit()


def save_signal(pair, timeframe, direction, confidence, reasoning):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO signals (created, pair, timeframe, direction, confidence, reasoning) VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), pair, timeframe, direction, confidence, reasoning)
        )
        conn.commit()


def save_trade(pair, direction, units, open_price, signal_id=None, is_test=False):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO trades (opened, pair, direction, units, open_price, signal_id, is_test) VALUES (?,?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), pair, direction, units, open_price, signal_id, int(is_test))
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
            "INSERT INTO account_snapshots (ts, balance, nav, open_pnl) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), balance, nav, open_pnl)
        )
        conn.commit()


def get_recent_signals(limit=20):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM signals ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()


def get_open_trades():
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE status='open'"
        ).fetchall()


def get_open_test_trades():
    """Returns open trades that were placed while in test mode."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE status='open' AND is_test=1"
        ).fetchall()


def get_pnl_summary():
    """Returns realized P&L totals across all closed trades."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT
                COALESCE(SUM(pnl), 0)                          AS total_pnl,
                COALESCE(SUM(CASE WHEN is_test=0 THEN pnl END), 0) AS live_pnl,
                COALESCE(SUM(CASE WHEN is_test=1 THEN pnl END), 0) AS test_pnl,
                COUNT(CASE WHEN status='closed' THEN 1 END)    AS closed_count,
                COUNT(CASE WHEN status='closed' AND pnl > 0 THEN 1 END) AS wins
            FROM trades
            WHERE status='closed' AND pnl IS NOT NULL
        """).fetchone()
    return {
        "total_pnl":    round(row[0], 2),
        "live_pnl":     round(row[1], 2),
        "test_pnl":     round(row[2], 2),
        "closed_count": row[3],
        "win_rate":     round(row[4] / row[3] * 100, 1) if row[3] else 0,
    }
