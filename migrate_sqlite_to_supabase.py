"""
One-time migration: SQLite → Supabase (Postgres).
Run once from the project root:
  python migrate_sqlite_to_supabase.py
Safe to re-run — uses ON CONFLICT DO NOTHING to skip duplicates.
"""
import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv
import psycopg2

load_dotenv()

SQLITE_PATH = Path(__file__).parent / "data" / "forex_agent.db"
PG_DSN = os.environ["DATABASE_URL"]

sqlite = sqlite3.connect(SQLITE_PATH)
sqlite.row_factory = sqlite3.Row
pg = psycopg2.connect(PG_DSN)
cur = pg.cursor()


def migrate_accounts():
    rows = sqlite.execute("SELECT account_id, account_name, api_key, last_used, user_id FROM accounts").fetchall()
    for r in rows:
        cur.execute("""
            INSERT INTO accounts (account_id, account_name, api_key, last_used, user_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (account_id) DO NOTHING
        """, (r["account_id"], r["account_name"], r["api_key"], r["last_used"], r["user_id"]))
    print(f"accounts: {len(rows)} rows migrated")


def migrate_signals():
    rows = sqlite.execute("SELECT created, pair, timeframe, direction, confidence, reasoning, acted_on, account_id FROM signals").fetchall()
    for r in rows:
        cur.execute("""
            INSERT INTO signals (created, pair, timeframe, direction, confidence, reasoning, acted_on, account_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (r["created"], r["pair"], r["timeframe"], r["direction"],
              r["confidence"], r["reasoning"], r["acted_on"], r["account_id"]))
    print(f"signals: {len(rows)} rows migrated")


def migrate_trades():
    rows = sqlite.execute("""
        SELECT opened, closed, pair, direction, units, open_price, close_price,
               pnl, status, signal_id, is_test, account_id, sl_price, tp_price
        FROM trades
    """).fetchall()
    for r in rows:
        cur.execute("""
            INSERT INTO trades (opened, closed, pair, direction, units, open_price, close_price,
                                pnl, status, signal_id, is_test, account_id, sl_price, tp_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (r["opened"], r["closed"], r["pair"], r["direction"], r["units"],
              r["open_price"], r["close_price"], r["pnl"], r["status"],
              r["signal_id"], r["is_test"], r["account_id"], r["sl_price"], r["tp_price"]))
    print(f"trades: {len(rows)} rows migrated")


def migrate_snapshots():
    rows = sqlite.execute("SELECT ts, balance, nav, open_pnl, account_id FROM account_snapshots").fetchall()
    for r in rows:
        cur.execute("""
            INSERT INTO account_snapshots (ts, balance, nav, open_pnl, account_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (r["ts"], r["balance"], r["nav"], r["open_pnl"], r["account_id"]))
    print(f"account_snapshots: {len(rows)} rows migrated")


def migrate_settings():
    rows = sqlite.execute("SELECT key, value FROM settings").fetchall()
    for r in rows:
        cur.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (r["key"], r["value"]))
    print(f"settings: {len(rows)} rows migrated")


try:
    migrate_accounts()
    migrate_signals()
    migrate_trades()
    migrate_snapshots()
    migrate_settings()
    pg.commit()
    print("\nMigration complete.")
except Exception as e:
    pg.rollback()
    print(f"\nMigration FAILED — rolled back. Error: {e}")
    raise
finally:
    sqlite.close()
    pg.close()
