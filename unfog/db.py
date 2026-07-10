"""Storage for Unfog.

Two backends behind the same 4 helpers (q / one / x / init):
- SQLite (default) — local dev, zero config, file at DB_PATH.
- Postgres — set DATABASE_URL (e.g. a free Neon database) and it's used
  automatically. Lets the app run on hosts with no persistent disk
  (Render free tier) without losing accounts or the waitlist.

App code writes SQLite-flavoured SQL ("?" placeholders, INSERT OR IGNORE,
datetime('now')); _adapt() translates it for Postgres. Timestamps are stored
as 'YYYY-MM-DD HH:MM:SS' UTC strings in both backends so date logic
(substr(at,1,10)) behaves identically.
"""
import os
import re
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if IS_PG:
    import psycopg2
    import psycopg2.extras

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "unfog.db")
)

_NOW_PG = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"

_TABLES = [
    ("users", """
        id {pk},
        email TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        energy_peak TEXT DEFAULT 'morning',
        created_at TEXT DEFAULT ({now})"""),
    ("tasks", """
        id {pk},
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        status TEXT DEFAULT 'active',
        estimate_min INTEGER DEFAULT 25,
        order_idx INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ({now}),
        done_at TEXT"""),
    ("microsteps", """
        id {pk},
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        done INTEGER DEFAULT 0,
        order_idx INTEGER DEFAULT 0"""),
    ("focus_sessions", """
        id {pk},
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        task_id INTEGER,
        minutes INTEGER NOT NULL,
        completed INTEGER DEFAULT 1,
        started_at TEXT DEFAULT ({now})"""),
    ("events", """
        id {pk},
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        at TEXT DEFAULT ({now})"""),
    ("waitlist", """
        id {pk},
        email TEXT UNIQUE NOT NULL,
        source TEXT DEFAULT 'landing',
        created_at TEXT DEFAULT ({now})"""),
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, status, order_idx)",
    "CREATE INDEX IF NOT EXISTS idx_steps_task ON microsteps(task_id, order_idx)",
    "CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, at)",
]


def _schema_statements():
    if IS_PG:
        pk, now = "SERIAL PRIMARY KEY", _NOW_PG
    else:
        pk, now = "INTEGER PRIMARY KEY", "datetime('now')"
    stmts = [
        f"CREATE TABLE IF NOT EXISTS {name} ({cols.format(pk=pk, now=now)})"
        for name, cols in _TABLES
    ]
    return stmts + _INDEXES


def _connect():
    if IS_PG:
        return psycopg2.connect(DATABASE_URL)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _adapt(sql):
    """Translate the app's SQLite-flavoured SQL for Postgres."""
    if not IS_PG:
        return sql
    s = sql.replace("?", "%s")
    s = s.replace("datetime('now')", _NOW_PG)
    if re.match(r"(?is)^\s*INSERT\s+OR\s+IGNORE", s):
        s = re.sub(r"(?is)^\s*INSERT\s+OR\s+IGNORE", "INSERT", s).rstrip()
        s += " ON CONFLICT DO NOTHING"
    return s


def init():
    con = _connect()
    try:
        if IS_PG:
            with con.cursor() as cur:
                for stmt in _schema_statements():
                    cur.execute(stmt)
        else:
            for stmt in _schema_statements():
                con.execute(stmt)
        con.commit()
    finally:
        con.close()


def q(sql, params=()):
    con = _connect()
    try:
        if IS_PG:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_adapt(sql), params)
                return [dict(r) for r in cur.fetchall()]
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def one(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql, params=()):
    """Execute a write. Returns the new row id for INSERTs (None on conflict-skip)."""
    con = _connect()
    try:
        if IS_PG:
            s = _adapt(sql)
            is_insert = re.match(r"(?is)^\s*INSERT", s) is not None
            if is_insert and "RETURNING" not in s.upper():
                s += " RETURNING id"
            with con.cursor() as cur:
                cur.execute(s, params)
                row = cur.fetchone() if is_insert else None
            con.commit()
            return row[0] if row else None
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()
