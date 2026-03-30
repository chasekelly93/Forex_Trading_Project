"""
SQLite store — persists signals, trades, and account snapshots.
This is what the dashboard reads from.
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "forex_agent.db"


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create tables if they don't exist."""
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
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                opened     TEXT NOT NULL,
                closed     TEXT,
                pair       TEXT NOT NULL,
                direction  TEXT NOT NULL,
                units      INTEGER NOT NULL,
                open_price REAL,
                close_price REAL,
                pnl        REAL,
                status     TEXT DEFAULT 'open',
                signal_id  INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                balance REAL,
                nav     REAL,
                open_pnl REAL
            )
        """)
        conn.commit()


def save_signal(pair, timeframe, direction, confidence, reasoning):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO signals (created, pair, timeframe, direction, confidence, reasoning) VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), pair, timeframe, direction, confidence, reasoning)
        )
        conn.commit()


def save_trade(pair, direction, units, open_price, signal_id=None):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO trades (opened, pair, direction, units, open_price, signal_id) VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), pair, direction, units, open_price, signal_id)
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
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows


def get_open_trades():
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='open'"
        ).fetchall()
    return rows
