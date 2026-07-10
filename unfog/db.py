"""SQLite storage for Unfog. One connection per call keeps it simple and
thread-safe under FastAPI's threadpool; move to Postgres when beta outgrows it."""
import os
import sqlite3

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "unfog.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    energy_peak TEXT DEFAULT 'morning',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    estimate_min INTEGER DEFAULT 25,
    order_idx INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS microsteps (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    done INTEGER DEFAULT 0,
    order_idx INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS focus_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id INTEGER,
    minutes INTEGER NOT NULL,
    completed INTEGER DEFAULT 1,
    started_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    source TEXT DEFAULT 'landing',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, status, order_idx);
CREATE INDEX IF NOT EXISTS idx_steps_task ON microsteps(task_id, order_idx);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, at);
"""


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    con = _connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def q(sql, params=()):
    con = _connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def one(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql, params=()):
    con = _connect()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()
