import sqlite3
import os
import math
import re
import time

# Import new v2 collectors (simplified, no scipy or HTTP)
try:
    from collectors.recent_form import calculate_recent_form
    from collectors.goals_markets import predict_ou_and_btts
except ImportError as e:
    print(f"WARN: New collectors import failed: {e}", flush=True)
    calculate_recent_form = None
    predict_ou_and_btts = None

# ============================================================
# IN-MEMORY CACHE — get_value_bets() is expensive (Poisson + Elo
# per event on every request). Cache results for 5 minutes.
# Invalidated immediately when manual odds are saved/deleted.
# ============================================================
_vb_cache = {"data": None, "ts": 0.0}
_VB_CACHE_TTL = 300  # 5 minutes

# Per-call session caches: populated once at start of get_value_bets(), None when inactive.
# Eliminates 250+ individual Neon connections per call (N+1 problem).
_session_elo = None   # (entity_lower, category, surface) -> rating dict
_session_xg = None    # team_lower -> xg dict
_session_hist = None  # event_id -> [history rows]

def invalidate_value_bets_cache():
    _vb_cache["data"] = None
    _vb_cache["ts"] = 0.0


def purge_out_of_scope_and_stale(max_age_hours=24):
    """Remove junk rows from odds_events: (1) sports we no longer collect (anything
    that isn't football/tennis — MMA/Boxing/etc. lingered for weeks because DELETE
    only removes PAST games, so far-future ones rotted); (2) events whose odds are
    older than `max_age_hours` (dead prices — the books stopped pricing them). Ages
    are computed in Python to stay dialect-safe (SQLite + Postgres)."""
    from datetime import datetime, timedelta
    conn = get_connection()
    try:
        oos = conn.execute(
            "SELECT COUNT(*) AS c FROM odds_events "
            "WHERE sport_key NOT LIKE 'soccer%' AND sport_key NOT LIKE 'tennis%'"
        ).fetchone()["c"]
        conn.execute(
            "DELETE FROM odds_events "
            "WHERE sport_key NOT LIKE 'soccer%' AND sport_key NOT LIKE 'tennis%'"
        )
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        stale = 0
        for r in conn.execute("SELECT event_id, updated_at FROM odds_events").fetchall():
            u = r["updated_at"]
            try:
                dt = datetime.strptime(str(u)[:16], "%Y-%m-%d %H:%M") if u else None
            except Exception:
                dt = None
            if dt and dt < cutoff:
                conn.execute("DELETE FROM odds_events WHERE event_id = ?", (r["event_id"],))
                stale += 1
        conn.commit()
        return {"out_of_scope": oos, "stale": stale}
    finally:
        conn.close()

# Guard so only one background recompute runs at a time.
_vb_refreshing = {"active": False}


def value_bets_cache_state():
    """Return (data_or_None, is_stale) without triggering a compute.
    Lets the API serve cached picks instantly and decide whether to refresh."""
    data = _vb_cache["data"]
    if data is None:
        return None, True
    stale = (time.time() - _vb_cache["ts"]) >= _VB_CACHE_TTL
    return data, stale


def refresh_value_bets():
    """Recompute value bets in the background (stale-while-revalidate).
    No-op if the cache is already fresh or another refresh is in flight."""
    if _vb_refreshing["active"]:
        return
    _vb_refreshing["active"] = True
    try:
        get_value_bets()  # recomputes only if cache is stale; updates memory + disk
    except Exception as e:
        import traceback
        print(f"VALUE_BETS_REFRESH_ERROR: {e}\n{traceback.format_exc()}", flush=True)
    finally:
        _vb_refreshing["active"] = False

# ============================================================
# DATABASE LAYER — supports both SQLite (local) and PostgreSQL (production)
# If DATABASE_URL is set (Supabase/Render Postgres), use PostgreSQL.
# Otherwise fall back to local SQLite for development.
# ============================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

VOLUME_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(VOLUME_DIR, "betting.db")

# Disk-persisted value-bets cache. Lets the app serve the last computed picks
# instantly after a (slow) Render cold start, while a fresh compute runs in the
# background — so users never stare at "Loading..." waiting for the engine.
_VB_CACHE_FILE = os.path.join(VOLUME_DIR, "vb_cache.json")


def _persist_vb_cache(data):
    """Best-effort write of the value-bets result to disk (survives restarts)."""
    try:
        import json
        os.makedirs(VOLUME_DIR, exist_ok=True)
        tmp = _VB_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f)
        os.replace(tmp, _VB_CACHE_FILE)  # atomic — never leaves a half-written file
    except Exception as e:
        print(f"VB_CACHE_PERSIST_WARN: {e}", flush=True)


def load_vb_cache_from_disk():
    """Populate the in-memory cache from disk if empty. Returns True if loaded.
    Called once at startup so the first request after a cold boot is instant."""
    if _vb_cache["data"] is not None:
        return False
    try:
        import json
        if not os.path.exists(_VB_CACHE_FILE):
            return False
        with open(_VB_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("data"):
            _vb_cache["data"] = payload["data"]
            _vb_cache["ts"] = payload.get("ts", 0.0)
            print(f"VB_CACHE: loaded {len(payload['data'])} events from disk "
                  f"(age {int(time.time() - _vb_cache['ts'])}s)", flush=True)
            return True
    except Exception as e:
        print(f"VB_CACHE_LOAD_WARN: {e}", flush=True)
    return False


def _translate_query(query):
    """Translate SQLite-specific SQL to PostgreSQL syntax."""
    q = query
    # Placeholders: ? -> %s
    q = q.replace("?", "%s")

    # INSERT OR IGNORE -> INSERT INTO ... ON CONFLICT DO NOTHING
    has_ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\b", q, re.IGNORECASE))
    q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", q, flags=re.IGNORECASE)
    if has_ignore:
        q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # INSERT OR REPLACE -> INSERT INTO ... ON CONFLICT (pk) DO UPDATE SET ...
    # Capture table name + column list BEFORE removing OR REPLACE so we can generate
    # the proper ON CONFLICT clause from TABLE_PRIMARY_KEYS.
    replace_m = re.search(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)",
        q, re.IGNORECASE
    )
    q = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", q, flags=re.IGNORECASE)
    if replace_m:
        table = replace_m.group(1)
        cols_raw = replace_m.group(2)
        cols = [c.strip() for c in cols_raw.split(',')]
        pk = TABLE_PRIMARY_KEYS.get(table)
        if pk:
            pk_cols = {c.strip() for c in pk.split(',')}
            update_cols = [c for c in cols if c not in pk_cols]
            if update_cols:
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                q = q.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
            else:
                q = q.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO NOTHING"
        else:
            q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # datetime('now', '-1 day') -> NOW() - INTERVAL '1 day'  etc.
    def _dt_modifier(m):
        mod = m.group(1).strip()  # e.g. '-1 day', '+2 hours'
        sign = '-' if mod.startswith('-') else '+'
        amount = mod.lstrip('+-').strip()
        return f"NOW() {sign} INTERVAL '{amount}'"
    q = re.sub(r"datetime\('now',\s*'([^']+)'\)", _dt_modifier, q, flags=re.IGNORECASE)
    # datetime('now') -> NOW()
    q = re.sub(r"datetime\('now'\)", "NOW()", q, flags=re.IGNORECASE)
    # commence_time is stored as TEXT but must be cast to TIMESTAMP for NOW() comparisons.
    # PostgreSQL raises "operator does not exist: text > timestamp" otherwise.
    # Handles both plain column and aliased (b.commence_time, e.commence_time, etc.)
    q = re.sub(
        r'(\b(?:\w+\.)?commence_time)\s*([<>]=?)\s*NOW\b',
        lambda m: f'CAST({m.group(1)} AS TIMESTAMP) {m.group(2)} NOW',
        q, flags=re.IGNORECASE
    )
    # AUTOINCREMENT -> SERIAL (handled in schema translation)
    return q


def _translate_schema(query):
    """Translate CREATE TABLE statements for PostgreSQL."""
    q = query
    q = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", q, flags=re.IGNORECASE)
    # Fix DEFAULT datetime('now') and DEFAULT (datetime('now'))
    q = re.sub(r"DEFAULT\s+\(?datetime\('now'\)\)?", "DEFAULT NOW()", q, flags=re.IGNORECASE)
    q = re.sub(r"datetime\('now'\)", "NOW()", q, flags=re.IGNORECASE)
    q = q.replace("?", "%s")
    return q


# Primary key columns per table (for PostgreSQL upsert)
TABLE_PRIMARY_KEYS = {
    "football_matches": "match_id",
    "tennis_matches": "match_id",
    "tennis_odds": "match_id",
    "national_matches": "match_id",
    "understat_matches": "match_id",
    "team_xg": "team",
    "odds_events": "event_id",
    "elo_ratings": "entity, category, surface",
    "bets": "id",
    "national_xg": "team",
    "national_xg_meta": "key",
    "manual_odds": "event_id, selection",
}


def _extract_table_name(query, keyword="INTO"):
    m = re.search(rf"{keyword}\s+(\w+)", query, flags=re.IGNORECASE)
    return m.group(1) if m else None


class DualAccessRow(dict):
    """A dict row that also supports integer index access (row[0])
    so it's compatible with both SQLite-style and dict-style code."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._values = list(self.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PostgresCursorWrapper:
    """Wraps a psycopg2 cursor to translate SQLite syntax + provide dict rows."""
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        is_ddl = "CREATE TABLE" in query.upper() or "ALTER TABLE" in query.upper() or "CREATE INDEX" in query.upper()
        if is_ddl:
            q = _translate_schema(query)
        else:
            q = _translate_query(query)

        is_replace = bool(re.search(r"INSERT\s+OR\s+REPLACE", query, flags=re.IGNORECASE))
        is_ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE", query, flags=re.IGNORECASE))

        if (is_replace or is_ignore) and "ON CONFLICT" not in q.upper():
            table = _extract_table_name(query, "INTO")
            pk = TABLE_PRIMARY_KEYS.get(table)
            if is_ignore or not pk:
                q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            else:
                # Build UPDATE SET for all columns (real upsert / replace)
                cols = self._extract_insert_columns(query)
                pk_cols = [c.strip() for c in pk.split(",")]
                update_cols = [c for c in cols if c not in pk_cols]
                if update_cols:
                    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
                    q = q.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
                else:
                    q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

        try:
            if params:
                self._cursor.execute(q, params)
            else:
                self._cursor.execute(q)
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate column" in msg:
                pass  # Expected during schema upgrades — ignore
            else:
                raise
        return self

    def _extract_insert_columns(self, query):
        """Extract column names from INSERT INTO table (col1, col2, ...) ..."""
        m = re.search(r"INTO\s+\w+\s*\(([^)]+)\)", query, flags=re.IGNORECASE)
        if not m:
            return []
        return [c.strip() for c in m.group(1).split(",")]

    def fetchone(self):
        row = self._cursor.fetchone()
        return DualAccessRow(row) if row is not None else None

    def fetchall(self):
        return [DualAccessRow(r) for r in self._cursor.fetchall()]

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class PostgresConnectionWrapper:
    """Wraps psycopg2 connection to mimic sqlite3.Connection API."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        import psycopg2.extras
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return PostgresCursorWrapper(cur)

    def execute(self, query, params=None):
        # sqlite3.Connection.execute is a shortcut — replicate it
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_connection():
    if USE_POSTGRES:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return PostgresConnectionWrapper(conn)
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    try:
        conn = get_connection()
    except Exception as e:
        print(f"DB connection failed: {e}. Falling back to SQLite.", flush=True)
        import sqlite3 as _sq
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = _sq.connect(DB_PATH)
        conn.row_factory = _sq.Row

    # For PostgreSQL, use autocommit for DDL to avoid transaction cascade failures
    if USE_POSTGRES:
        conn._conn.autocommit = True

    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS football_matches (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            season TEXT,
            league TEXT,
            league_name TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            result TEXT,
            home_shots INTEGER,
            away_shots INTEGER,
            home_shots_target INTEGER,
            away_shots_target INTEGER,
            home_corners INTEGER,
            away_corners INTEGER,
            b365_home REAL,
            b365_draw REAL,
            b365_away REAL,
            pinnacle_home_open REAL,
            pinnacle_draw_open REAL,
            pinnacle_away_open REAL,
            pinnacle_home_close REAL,
            pinnacle_draw_close REAL,
            pinnacle_away_close REAL,
            max_home REAL,
            max_draw REAL,
            max_away REAL,
            avg_home REAL,
            avg_draw REAL,
            avg_away REAL,
            over25_pinnacle REAL,
            under25_pinnacle REAL,
            over25_max REAL,
            over25_avg REAL,
            collected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Index for the backtest engine: it scans settled matches ordered by date.
    c.execute("CREATE INDEX IF NOT EXISTS idx_fm_date ON football_matches(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fm_league ON football_matches(league_name)")

    # Historical tennis odds (tennis-data.co.uk) — real B365/Pinnacle/Max/Avg
    # match-winner prices + results, for the 2-way tennis backtest. Separate from
    # tennis_matches (JeffSackmann) which has stats/rankings but NO odds.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tennis_odds (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            tour TEXT,
            tournament TEXT,
            surface TEXT,
            round TEXT,
            best_of INTEGER,
            winner TEXT,
            loser TEXT,
            wrank INTEGER,
            lrank INTEGER,
            b365_w REAL, b365_l REAL,
            ps_w REAL, ps_l REAL,
            max_w REAL, max_l REAL,
            avg_w REAL, avg_l REAL,
            collected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_to_date ON tennis_odds(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_to_tour ON tennis_odds(tour)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS tennis_matches (
            match_id TEXT PRIMARY KEY,
            tourney_id TEXT,
            tourney_name TEXT,
            surface TEXT,
            tourney_level TEXT,
            tourney_date TEXT,
            round TEXT,
            best_of INTEGER,
            winner_name TEXT,
            winner_id TEXT,
            winner_hand TEXT,
            winner_age REAL,
            winner_rank INTEGER,
            winner_rank_points INTEGER,
            loser_name TEXT,
            loser_id TEXT,
            loser_hand TEXT,
            loser_age REAL,
            loser_rank INTEGER,
            loser_rank_points INTEGER,
            score TEXT,
            minutes INTEGER,
            w_ace INTEGER,
            w_df INTEGER,
            w_svpt INTEGER,
            w_1stIn INTEGER,
            w_1stWon INTEGER,
            w_2ndWon INTEGER,
            w_bpSaved INTEGER,
            w_bpFaced INTEGER,
            l_ace INTEGER,
            l_df INTEGER,
            l_svpt INTEGER,
            l_1stIn INTEGER,
            l_1stWon INTEGER,
            l_2ndWon INTEGER,
            l_bpSaved INTEGER,
            l_bpFaced INTEGER,
            collected_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS collection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            status TEXT,
            records_added INTEGER,
            message TEXT,
            ran_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS odds_events (
            event_id TEXT PRIMARY KEY,
            sport_key TEXT,
            sport_name TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            pin_home REAL, pin_draw REAL, pin_away REAL,
            best_home REAL, best_draw REAL, best_away REAL,
            x1_home REAL, x1_draw REAL, x1_away REAL,
            pin_over25 REAL, pin_under25 REAL,
            x1_over25 REAL, x1_under25 REAL,
            best_over25 REAL, best_under25 REAL,
            pin_btts_yes REAL, pin_btts_no REAL,
            x1_btts_yes REAL, x1_btts_no REAL,
            best_btts_yes REAL, best_btts_no REAL,
            updated_at TEXT
        )
    """)
    # Add new columns if upgrading from old schema
    for col, typ in [
        ("pin_over25", "REAL"), ("pin_under25", "REAL"),
        ("x1_over25", "REAL"), ("x1_under25", "REAL"),
        ("best_over25", "REAL"), ("best_under25", "REAL"),
        ("pin_btts_yes", "REAL"), ("pin_btts_no", "REAL"),
        ("x1_btts_yes", "REAL"), ("x1_btts_no", "REAL"),
        ("best_btts_yes", "REAL"), ("best_btts_no", "REAL"),
        # Betfair Exchange back prices — primary reference for true probability
        ("bf_home", "REAL"), ("bf_draw", "REAL"), ("bf_away", "REAL"),
        # Bet365 specific odds for edge calculation
        ("b365_home", "REAL"), ("b365_draw", "REAL"), ("b365_away", "REAL"),
        # Asian Handicap (spreads): Pinnacle's main line + prices, the best price
        # among books offering the SAME line, and the user's 1xBet price at that
        # line. ah_line is the HOME handicap (e.g. -1.5).
        ("ah_line", "REAL"),
        ("pin_ah_home", "REAL"), ("pin_ah_away", "REAL"),
        ("best_ah_home", "REAL"), ("best_ah_away", "REAL"),
        ("x1_ah_home", "REAL"), ("x1_ah_away", "REAL"),
    ]:
        try:
            c.execute(f"ALTER TABLE odds_events ADD COLUMN {col} {typ}")
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS understat_matches (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            season TEXT,
            league_key TEXT,
            league_name TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            home_xg REAL,
            away_xg REAL,
            forecast_home REAL,
            forecast_draw REAL,
            forecast_away REAL,
            is_result INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS team_xg (
            team TEXT PRIMARY KEY,
            league TEXT,
            matches_sample INTEGER,
            avg_xg_for REAL,
            avg_xg_against REAL,
            avg_goals_for REAL,
            avg_goals_against REAL,
            xg_overperf_attack REAL,
            xg_overperf_defense REAL,
            last_match_date TEXT
        )
    """)

    # Snapshots of odds for movement tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            pin_home REAL, pin_draw REAL, pin_away REAL,
            best_home REAL, best_draw REAL, best_away REAL,
            x1_home REAL, x1_draw REAL, x1_away REAL,
            pin_over25 REAL, pin_under25 REAL,
            pin_btts_yes REAL, pin_btts_no REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_odds_history_event ON odds_history(event_id, captured_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS telegram_alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            selection TEXT NOT NULL,
            book TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tg_alerts ON telegram_alerts_sent(event_id, selection, book, sent_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS elo_ratings (
            entity TEXT NOT NULL,
            category TEXT NOT NULL,
            surface TEXT,
            rating REAL,
            games INTEGER,
            updated_at TEXT,
            PRIMARY KEY (entity, category, surface)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS national_matches (
            match_id TEXT PRIMARY KEY,
            date TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            result TEXT,
            tournament TEXT,
            city TEXT,
            country TEXT,
            neutral INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS national_xg (
            team TEXT PRIMARY KEY,
            goals_for_avg REAL,
            goals_against_avg REAL,
            matches_used INTEGER,
            last_update TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS national_xg_meta (
            key TEXT PRIMARY KEY,
            value REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS manual_odds (
            event_id TEXT NOT NULL,
            selection TEXT NOT NULL,
            odd REAL NOT NULL,
            updated_at TEXT,
            PRIMARY KEY (event_id, selection)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT NOT NULL,
            event_id TEXT,
            sport_name TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            bookmaker TEXT,
            odds REAL NOT NULL,
            stake REAL NOT NULL,
            pin_implied_prob REAL,
            pin_close_odds REAL,
            pin_close_implied REAL,
            status TEXT DEFAULT 'pending',
            result TEXT,
            profit REAL DEFAULT 0,
            notes TEXT,
            settled_at TEXT
        )
    """)

    # Migrate: add edge_pct column to bets if not present
    try:
        c.execute("ALTER TABLE bets ADD COLUMN edge_pct REAL")
    except Exception:
        pass  # already exists

    # Migrate: when the Pinnacle close was captured (audit trail for CLV)
    try:
        c.execute("ALTER TABLE bets ADD COLUMN pin_close_captured_at TEXT")
    except Exception:
        pass  # already exists

    # Migrate: DEVIGGED (no-vig) closing odd — realized CLV is measured against
    # this fair close, not the raw price, so beating the vig doesn't count as value
    try:
        c.execute("ALTER TABLE bets ADD COLUMN pin_close_fair_odds REAL")
    except Exception:
        pass  # already exists

    if USE_POSTGRES:
        conn._conn.autocommit = False
    else:
        conn.commit()
    conn.close()
    print("Database initialised.", flush=True)

def log_collection(source, status, records_added, message=""):
    conn = get_connection()
    conn.execute(
        "INSERT INTO collection_log (source, status, records_added, message) VALUES (?,?,?,?)",
        (source, status, records_added, message)
    )
    conn.commit()
    conn.close()

def get_football_summary():
    conn = get_connection()
    rows = conn.execute("""
        SELECT league_name, COUNT(*) as matches, MIN(date) as from_date, MAX(date) as to_date
        FROM football_matches
        GROUP BY league_name
        ORDER BY league_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_tennis_summary():
    conn = get_connection()
    rows = conn.execute("""
        SELECT surface, COUNT(*) as matches, MIN(tourney_date) as from_date, MAX(tourney_date) as to_date
        FROM tennis_matches
        GROUP BY surface
        ORDER BY surface
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_recent_football(limit=50):
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, league_name, home_team, away_team,
               home_goals, away_goals, result,
               pinnacle_home_close, pinnacle_draw_close, pinnacle_away_close,
               avg_home, avg_draw, avg_away
        FROM football_matches
        WHERE home_goals IS NOT NULL
        ORDER BY date DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_recent_tennis(limit=50):
    conn = get_connection()
    rows = conn.execute("""
        SELECT tourney_date, tourney_name, surface, round,
               winner_name, loser_name, winner_rank, loser_rank,
               score, minutes
        FROM tennis_matches
        ORDER BY tourney_date DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_collection_log(limit=20):
    conn = get_connection()
    rows = conn.execute("""
        SELECT source, status, records_added, message, ran_at
        FROM collection_log
        ORDER BY ran_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stats():
    conn = get_connection()
    football_count = conn.execute("SELECT COUNT(*) FROM football_matches").fetchone()[0]
    tennis_count = conn.execute("SELECT COUNT(*) FROM tennis_matches").fetchone()[0]
    football_leagues = conn.execute("SELECT COUNT(DISTINCT league_name) FROM football_matches").fetchone()[0]
    last_collection = conn.execute(
        "SELECT ran_at FROM collection_log ORDER BY ran_at DESC LIMIT 1"
    ).fetchone()
    try:
        odds_count = conn.execute("SELECT COUNT(*) FROM odds_events").fetchone()[0]
    except Exception:
        odds_count = 0
    try:
        national_count = conn.execute("SELECT COUNT(*) FROM national_matches").fetchone()[0]
    except Exception:
        national_count = 0
    try:
        # Events with odds updated in the last 24h — proxy for "live" coverage today
        value_bets_today = conn.execute("""
            SELECT COUNT(*) FROM odds_events
            WHERE commence_time > datetime('now')
        """).fetchone()[0]
    except Exception:
        value_bets_today = 0
    try:
        pending_bets = conn.execute(
            "SELECT COUNT(*) FROM bets WHERE status='pending'"
        ).fetchone()[0]
    except Exception:
        pending_bets = 0
    conn.close()
    return {
        "football_matches": football_count,
        "tennis_matches": tennis_count,
        "football_leagues": football_leagues,
        "value_bets_today": value_bets_today,
        "pending_bets": pending_bets,
        "odds_events": odds_count,
        "national_matches": national_count,
        "last_collection": last_collection[0] if last_collection else "Never"
    }


def implied_prob(odds):
    """Decimal odds -> raw implied probability (includes margin)."""
    if not odds or odds <= 1:
        return None
    return 1.0 / odds


def remove_vig(odds_home, odds_draw, odds_away):
    """
    Remove the bookmaker's vig (margin) to get TRUE probabilities.
    Industry standard: Pinnacle no-vig odds = closest proxy to true probability.
    
    Returns dict with home/draw/away true probabilities (summing to 100%).
    """
    if not odds_home or odds_home <= 1:
        return None
    
    # Implied probabilities (with vig)
    imp_home = 1 / odds_home
    imp_away = 1 / odds_away if odds_away and odds_away > 1 else 0
    imp_draw = 1 / odds_draw if odds_draw and odds_draw > 1 else 0
    
    # Total implied probability (will be > 100% due to vig)
    total = imp_home + imp_draw + imp_away
    if total <= 0:
        return None
    
    # Proportional method (simplest and most used in industry)
    # Removes vig by dividing each probability by total
    true_home = (imp_home / total) * 100
    true_away = (imp_away / total) * 100
    true_draw = (imp_draw / total) * 100 if imp_draw > 0 else None
    
    return {
        "home": round(true_home, 2),
        "draw": round(true_draw, 2) if true_draw is not None else None,
        "away": round(true_away, 2),
        "vig_pct": round((total - 1) * 100, 2),  # bookmaker margin
    }


def remove_vig_power(odds_home, odds_draw, odds_away):
    """
    Power (Shin) devig — more accurate than proportional for 3-way markets.

    Finds exponent k such that sum(implied^(1/k)) = 1.0 via binary search.
    Distributes vig proportionally to each outcome's implied probability,
    which gives slightly higher true probability to favorites vs proportional.

    Used by OddsJam and professional sharp bettors.
    Falls back to proportional if binary search fails.
    """
    if not odds_home or odds_home <= 1:
        return None

    imp = [1.0 / odds_home]
    if odds_away and odds_away > 1:
        imp.append(1.0 / odds_away)
    if odds_draw and odds_draw > 1:
        imp.insert(1, 1.0 / odds_draw)  # keep home/draw/away order

    raw_vig = sum(imp)
    if raw_vig <= 1.0:
        # No vig — already fair odds
        s = sum(imp)
        normed = [round(p / s * 100, 2) for p in imp]
        result = {"home": normed[0], "vig_pct": 0.0}
        if odds_draw and odds_draw > 1:
            result["draw"] = normed[1]
            result["away"] = normed[2]
        else:
            result["draw"] = None
            result["away"] = normed[1]
        return result

    # Binary search for power exponent k
    lo, hi = 0.2, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        try:
            total = sum(p ** (1.0 / mid) for p in imp)
        except (ValueError, ZeroDivisionError):
            return None
        if total > 1.0:
            hi = mid
        else:
            lo = mid
    k = (lo + hi) / 2.0

    try:
        true_probs = [p ** (1.0 / k) for p in imp]
    except (ValueError, ZeroDivisionError):
        return None

    s = sum(true_probs)
    if s <= 0:
        return None
    true_probs = [round(p / s * 100, 2) for p in true_probs]

    vig_pct = round((raw_vig - 1.0) * 100, 2)

    if odds_draw and odds_draw > 1:
        return {"home": true_probs[0], "draw": true_probs[1], "away": true_probs[2], "vig_pct": vig_pct}
    return {"home": true_probs[0], "draw": None, "away": true_probs[1], "vig_pct": vig_pct}


# Actionable value-bets window: events further out than this are not analysed
# (early season-opener lines are placeholders → phantom edges, no real close).
VB_MAX_HOURS_AHEAD = 48

# Pinnacle margin threshold above which market is considered illiquid.
# Pinnacle's typical margins: 1.5-2.5% (major leagues), 3-4% (minor).
# Above 4% = Pinnacle doesn't have strong info → trust less.
PINNACLE_MAX_LIQUID_VIG = 4.0

# Sharp-consensus gate: when BOTH Pinnacle and Betfair exist, we blend them into
# one stable fair line and check how far apart their no-vig probabilities are.
# If the largest gap on any outcome is within this many percentage points, the
# two independent sharps "agree" (high trust). Beyond it they "diverge" → the
# true probability is genuinely uncertain, so we don't celebrate it as a green
# value bet. Tunable; 2.5pp ≈ the user's "within ~2%" intuition with a little slack.
REF_AGREE_PP = 2.5

# Teams with massive public following — market odds often biased downward by casual bettors.
# When model probability is significantly lower than market-implied, flag as possible public trap.
POPULAR_CLUBS = {
    # National teams
    "brazil", "argentina", "france", "england", "germany", "spain",
    "portugal", "italy", "netherlands",
    # Club football
    "real madrid", "barcelona", "fc barcelona", "manchester united",
    "manchester city", "liverpool", "chelsea", "arsenal", "tottenham",
    "paris saint-germain", "psg", "juventus", "inter milan", "ac milan",
    "bayern munich", "fc bayern", "borussia dortmund", "atletico madrid",
    "atletico de madrid",
}


def calculate_edge(soft_odd, true_prob_pct):
    """
    Industry-standard edge calculation.

    Formula: edge = (soft_odd × true_prob) - 1
    
    Args:
        soft_odd: The decimal odd from soft bookmaker (e.g. 1xBet)
        true_prob_pct: True probability (0-100%)
    
    Returns:
        Edge as a percentage (e.g. 5.2 means +5.2% edge)
    """
    if not soft_odd or not true_prob_pct or soft_odd <= 1:
        return None
    return (soft_odd * (true_prob_pct / 100) - 1) * 100


def calculate_clv(entry_odd, closing_odd):
    """
    Closing Line Value: did our bet entry odd beat the closing line?
    
    Positive CLV is the strongest predictor of long-term profitability.
    Pros typically achieve 2-4% positive CLV.
    
    Returns CLV percentage. >0 means we got a better price than closing.
    """
    if not entry_odd or not closing_odd or entry_odd <= 1 or closing_odd <= 1:
        return None
    # CLV = (entry_odd / closing_odd - 1) × 100
    return round((entry_odd / closing_odd - 1) * 100, 2)


def kelly_stake(true_prob, odds, fraction=0.25):
    """Quarter-Kelly stake as fraction of bankroll. Returns 0 if no edge."""
    if not odds or not true_prob or odds <= 1:
        return 0
    b = odds - 1
    p = true_prob
    q = 1 - p
    edge = b * p - q
    if edge <= 0:
        return 0
    return max(0, (edge / b) * fraction)


def confidence_score(edge_pct, pin_implied, sport_name, agreement=None):
    """
    Confidence score 1-5 stars based on:
    - Edge sweet spot (2-8% = highest, >15% suspect)
    - Probability magnitude (0.20-0.80 = liquid market)
    - Market quality (top leagues = sharper)
    - Signal agreement (xG/Elo match Pinnacle = high confidence)
    """
    if edge_pct is None or edge_pct < 0:
        return 0
    score = 0
    
    # Edge sweet spot
    if 2 <= edge_pct <= 8:
        score += 3
    elif edge_pct < 2:
        score += 1
    elif edge_pct <= 10:
        score += 2
    elif edge_pct <= 15:
        score += 1
    else:
        # Edge >15% is SUSPECT (likely model error)
        score = 0
        return 0
    
    # Probability magnitude (liquid market)
    if pin_implied is not None and 0.20 <= pin_implied <= 0.80:
        score += 1
    
    # Top tier markets
    if sport_name and any(s in sport_name for s in ["Premier League", "La Liga", "Bundesliga", "Champions", "World Cup", "Serie A", "Ligue", "Wimbledon", "ATP", "WTA"]):
        score += 1
    
    # Signal agreement bonus
    if agreement == "high":
        score += 1
    elif agreement == "low":
        score = max(1, score - 1)  # Penalize divergence
    
    return max(1, min(5, score))


def get_value_bets():
    """Full analysis engine across 1X2, O/U 2.5, BTTS markets."""
    global _session_elo, _session_xg, _session_hist
    # --- TTL cache ---
    now = time.time()
    if _vb_cache["data"] is not None and (now - _vb_cache["ts"]) < _VB_CACHE_TTL:
        return _vb_cache["data"]

    conn = get_connection()
    try:
        # Only analyse upcoming events (3h grace for in-play). Past games are dead
        # weight: they bloat the slow engine and, when a stale cache keeps them, the
        # page shows finished matches and hides the real upcoming ones.
        # Upper bound: the ACTIONABLE betting window. Bookmakers price season
        # openers weeks ahead with thin placeholder lines — a soft price vs an
        # early Pinnacle line produces phantom "edges" (and there is no real
        # closing line until near kickoff), so those games must not be analysed,
        # shown, sorted or alerted as value. They enter the page naturally once
        # they come inside the window.
        rows = conn.execute(
            f"SELECT * FROM odds_events WHERE commence_time > datetime('now', '-3 hours') "
            f"AND commence_time < datetime('now', '+{int(VB_MAX_HOURS_AHEAD)} hours') "
            "ORDER BY commence_time ASC"
        ).fetchall()
    except Exception as e:
        import traceback
        print(f"VALUE_BETS_ERROR: failed to load odds_events: {e}\n{traceback.format_exc()}", flush=True)
        conn.close()
        return []

    # Bulk pre-load all session data in one connection (eliminates N+1 Neon round-trips)
    event_ids = [r["event_id"] for r in rows]
    try:
        elo_rows = conn.execute("SELECT * FROM elo_ratings").fetchall()
        _session_elo = {(r["entity"].lower(), r["category"], r.get("surface") or ''): dict(r) for r in elo_rows}
    except Exception:
        _session_elo = {}
    try:
        xg_rows = conn.execute("SELECT * FROM team_xg").fetchall()
        _session_xg = {r["team"].lower(): dict(r) for r in xg_rows}
    except Exception:
        _session_xg = {}
    try:
        if event_ids:
            placeholders = ",".join(["?" for _ in event_ids])
            hist_rows = conn.execute(
                f"SELECT * FROM odds_history WHERE event_id IN ({placeholders}) ORDER BY event_id, captured_at ASC",
                event_ids
            ).fetchall()
            _session_hist = {}
            for r in hist_rows:
                eid = r["event_id"]
                _session_hist.setdefault(eid, []).append(r)
        else:
            _session_hist = {}
    except Exception:
        _session_hist = {}
    conn.close()

    # Load manual odds once
    manual_odds_map = get_manual_odds_map()

    results = []
    for r in rows:
        d = dict(r)
        picks = []

        # ========== 1X2 — Reference odds priority: Betfair Exchange > Pinnacle > xG ==========
        # Betfair Exchange: peer-to-peer market, 0% bookmaker margin, sharpest reference
        # Pinnacle: sharp bookmaker, ~2% margin, strong secondary reference
        bf_h = d.get("bf_home")
        bf_d = d.get("bf_draw")
        bf_a = d.get("bf_away")
        ph_raw = d.get("pin_home")
        pd_raw = d.get("pin_draw")
        pa_raw = d.get("pin_away")

        # ── A+B: blend Pinnacle + Betfair into ONE stable fair line, and record
        # whether the two sharps AGREE. Using both (instead of switching between
        # them) stops the edge from jumping just because one source updated; the
        # agreement flag lets the UI reserve the green "value" tier for picks that
        # two independent sharp markets CONFIRM. ──
        def _novig(h, dr, a):
            dv = remove_vig_power(h, dr, a)
            if not dv:
                return None
            return {
                "h": dv["home"] / 100,
                "d": (dv["draw"] / 100) if dv.get("draw") is not None else None,
                "a": dv["away"] / 100,
                "vig": dv["vig_pct"],
            }

        pin_nv = _novig(ph_raw, pd_raw, pa_raw) if (ph_raw and pa_raw) else None
        bf_nv = _novig(bf_h, bf_d, bf_a) if (bf_h and bf_a) else None

        true_h = true_d = true_a = None
        if pin_nv and bf_nv:
            # Average the two no-vig lines → stable fair probabilities.
            true_h = (pin_nv["h"] + bf_nv["h"]) / 2
            true_a = (pin_nv["a"] + bf_nv["a"]) / 2
            if pin_nv["d"] is not None and bf_nv["d"] is not None:
                true_d = (pin_nv["d"] + bf_nv["d"]) / 2
            else:
                true_d = pin_nv["d"] if pin_nv["d"] is not None else bf_nv["d"]
            diffs = [abs(pin_nv["h"] - bf_nv["h"]) * 100, abs(pin_nv["a"] - bf_nv["a"]) * 100]
            if pin_nv["d"] is not None and bf_nv["d"] is not None:
                diffs.append(abs(pin_nv["d"] - bf_nv["d"]) * 100)
            max_diff = max(diffs)
            d["ref_max_diff_pp"] = round(max_diff, 1)
            # Two independent SHARP markets: if they disagree, the true prob is
            # genuinely uncertain → block the green tier (handled in the UI).
            d["ref_agreement"] = "agree" if max_diff <= REF_AGREE_PP else "diverge_sharp"
            d["ref_sources"] = "Pinnacle + Betfair"
            d["odds_source"] = "blend"
            d["pin_vig_pct"] = round(min(pin_nv["vig"], bf_nv["vig"]), 2)
        elif bf_nv:
            true_h, true_d, true_a = bf_nv["h"], bf_nv["d"], bf_nv["a"]
            d["ref_agreement"] = "single"
            d["ref_sources"] = "Betfair only"
            d["odds_source"] = "betfair"
            d["pin_vig_pct"] = bf_nv["vig"]
        elif pin_nv:
            true_h, true_d, true_a = pin_nv["h"], pin_nv["d"], pin_nv["a"]
            d["ref_agreement"] = "single"
            d["ref_sources"] = "Pinnacle only"
            d["odds_source"] = "pinnacle"
            d["pin_vig_pct"] = pin_nv["vig"]
        else:
            d["ref_agreement"] = None
            d["odds_source"] = "xg_model"
            print(f"NO_REFERENCE: {d.get('home_team')} vs {d.get('away_team')} "
                  f"— no Betfair or Pinnacle odds, using xG model", flush=True)

        if true_h is not None and true_a is not None:
            d["true_home_pct"] = round(true_h * 100, 1)
            d["true_draw_pct"] = round(true_d * 100, 1) if true_d else None
            d["true_away_pct"] = round(true_a * 100, 1)
            d["pin_low_liquidity"] = d.get("pin_vig_pct", 0) > PINNACLE_MAX_LIQUID_VIG

            for sel, tp, x1, b365, best, pin_o in [
                (d.get("home_team"), true_h, d.get("x1_home"), d.get("b365_home"), d.get("best_home"), bf_h or ph_raw),
                ("Draw", true_d, d.get("x1_draw"), d.get("b365_draw"), d.get("best_draw"), bf_d or pd_raw),
                (d.get("away_team"), true_a, d.get("x1_away"), d.get("b365_away"), d.get("best_away"), bf_a or pa_raw),
            ]:
                if not tp: continue
                for book, odd in (("1xBet", x1), ("Bet365", b365), ("Best", best)):
                    if not odd: continue
                    edge = (odd * tp - 1) * 100
                    pk = {
                        "market": "Match Result",
                        "selection": sel,
                        "true_prob": round(tp * 100, 1),
                        "pin_odd": pin_o,
                        "book": book,
                        "book_odd": odd,
                        "edge_pct": round(edge, 2),
                        "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                    }
                    # Label which real book holds the best price (so the user knows
                    # WHERE to bet — 1xBet is theirs). Best == max across all books.
                    if book == "Best":
                        if x1 and abs(odd - x1) < 1e-9:
                            pk["best_book"] = "1xBet"
                        elif b365 and abs(odd - b365) < 1e-9:
                            pk["best_book"] = "Bet365"
                        else:
                            pk["best_book"] = "outra casa"
                    picks.append(pk)

        # ========== OVER/UNDER 2.5 ==========
        pov, pun = d.get("pin_over25"), d.get("pin_under25")
        if pov and pun:
            inv_o = implied_prob(pov) or 0
            inv_u = implied_prob(pun) or 0
            margin = inv_o + inv_u
            if margin > 0:
                true_o = inv_o / margin
                true_u = inv_u / margin
                d["true_over25_pct"] = round(true_o * 100, 1)
                d["true_under25_pct"] = round(true_u * 100, 1)
                for sel, tp, x1, best, pin_o in [
                    ("Over 2.5 Goals", true_o, d.get("x1_over25"), d.get("best_over25"), pov),
                    ("Under 2.5 Goals", true_u, d.get("x1_under25"), d.get("best_under25"), pun),
                ]:
                    for book, odd in (("1xBet", x1), ("Best", best)):
                        if not odd: continue
                        edge = (odd * tp - 1) * 100
                        picks.append({
                            "market": "Over/Under 2.5",
                            "selection": sel,
                            "true_prob": round(tp * 100, 1),
                            "pin_odd": pin_o,
                            "book": book,
                            "book_odd": odd,
                            "edge_pct": round(edge, 2),
                            "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                        })

        # ========== BTTS ==========
        pby, pbn = d.get("pin_btts_yes"), d.get("pin_btts_no")
        if pby and pbn:
            inv_y = implied_prob(pby) or 0
            inv_n = implied_prob(pbn) or 0
            margin = inv_y + inv_n
            if margin > 0:
                true_y = inv_y / margin
                true_n = inv_n / margin
                d["true_btts_yes_pct"] = round(true_y * 100, 1)
                d["true_btts_no_pct"] = round(true_n * 100, 1)
                for sel, tp, x1, best, pin_o in [
                    ("BTTS Yes", true_y, d.get("x1_btts_yes"), d.get("best_btts_yes"), pby),
                    ("BTTS No", true_n, d.get("x1_btts_no"), d.get("best_btts_no"), pbn),
                ]:
                    for book, odd in (("1xBet", x1), ("Best", best)):
                        if not odd: continue
                        edge = (odd * tp - 1) * 100
                        picks.append({
                            "market": "Both Teams to Score",
                            "selection": sel,
                            "true_prob": round(tp * 100, 1),
                            "pin_odd": pin_o,
                            "book": book,
                            "book_odd": odd,
                            "edge_pct": round(edge, 2),
                            "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                        })

        # ========== ASIAN HANDICAP (spreads) ==========
        ah_line = d.get("ah_line")
        pah_h, pah_a = d.get("pin_ah_home"), d.get("pin_ah_away")
        if ah_line is not None and pah_h and pah_a:
            dv = remove_vig_power(pah_h, None, pah_a)   # 2-way devig (no draw)
            if dv:
                true_h_ah = dv["home"] / 100
                true_a_ah = dv["away"] / 100
                mkt_lbl = f"Asian Handicap {ah_line:+g}"
                for sel, tp, x1_ah, best_ah, pin_o in [
                    (f"{d.get('home_team')} {ah_line:+g}", true_h_ah, d.get("x1_ah_home"), d.get("best_ah_home"), pah_h),
                    (f"{d.get('away_team')} {-ah_line:+g}", true_a_ah, d.get("x1_ah_away"), d.get("best_ah_away"), pah_a),
                ]:
                    for book, odd in (("1xBet", x1_ah), ("Best", best_ah)):
                        if not odd:
                            continue
                        edge = (odd * tp - 1) * 100
                        picks.append({
                            "market": mkt_lbl,
                            "selection": sel,
                            "true_prob": round(tp * 100, 1),
                            "pin_odd": pin_o,
                            "book": book,
                            "book_odd": odd,
                            "edge_pct": round(edge, 2),
                            "kelly_pct": round(kelly_stake(tp, odd) * 100, 2),
                        })

        # Confidence per pick
        for p in picks:
            pin_implied = 1.0/p["pin_odd"] if p["pin_odd"] else None
            p["confidence"] = confidence_score(p["edge_pct"], pin_implied, d.get("sport_name"))

        d["all_picks"] = picks

        # MOST LIKELY across all markets (prefer 1X2 Best)
        most_likely = None
        h2h_best = [p for p in picks if p["market"] == "Match Result" and p["book"] == "Best"]
        if h2h_best:
            most_likely = max(h2h_best, key=lambda p: p["true_prob"])
        d["most_likely"] = most_likely

        # BEST VALUE: highest credible edge (2-15%), weighted by confidence
        credible = [p for p in picks if p["edge_pct"] is not None and 2 <= p["edge_pct"] <= 15]
        if credible:
            best_value = max(credible, key=lambda p: (p["confidence"], p["edge_pct"]))
        else:
            any_pos = [p for p in picks if p["edge_pct"] is not None and p["edge_pct"] > 0]
            best_value = max(any_pos, key=lambda p: p["edge_pct"]) if any_pos else None
        d["best_value"] = best_value
        d["best_edge"] = best_value["edge_pct"] if best_value else None
        d["best_confidence"] = best_value["confidence"] if best_value else 0

        # === xG SIGNAL (alternative independent model) ===
        # MUST init before the branches: a national/World Cup fixture whose teams
        # can't be modelled (e.g. knockout bracket placeholders "1C", "W85") makes
        # predict_national_match return None, and without this the `else` path leaves
        # xg_sig unbound → UnboundLocalError crashes the WHOLE engine mid-loop (the
        # cache then never refreshes and the page freezes on stale data).
        xg_sig = None
        sport_check = (d.get("sport_name") or "").lower()
        is_national = ("world cup" in sport_check or "nations" in sport_check
                       or "euro" in sport_check or "copa" in sport_check
                       or "gold cup" in sport_check or "afcon" in sport_check
                       or "asian cup" in sport_check)
        if is_national:
            # Use national team xG proxy (Dixon-Coles on historical matches)
            from collectors.national_xg import predict_national_match
            try:
                nat_pred = predict_national_match(d.get("home_team"), d.get("away_team"))
            except Exception as e:
                print(f"WARN: national_xg error ({d.get('home_team')} v {d.get('away_team')}): {e}", flush=True)
                nat_pred = None
            if nat_pred:
                xg_sig = {
                    "home": nat_pred["prob_home"],
                    "draw": nat_pred["prob_draw"],
                    "away": nat_pred["prob_away"],
                    "over25": nat_pred["prob_over25"],
                    "under25": nat_pred["prob_under25"],
                    "btts_yes": nat_pred["prob_btts_yes"],
                    "btts_no": nat_pred["prob_btts_no"],
                    "expected_home_goals": nat_pred["lambda_home"],
                    "expected_away_goals": nat_pred["lambda_away"],
                    "source": "national_xg_proxy",
                }
                # If no odds exist (Mundial fixtures without market), populate
                # true probabilities directly so manual odds input has prob to compare against
                if d.get("true_home_pct") is None:
                    d["true_home_pct"] = nat_pred["prob_home"]
                    d["true_draw_pct"] = nat_pred["prob_draw"]
                    d["true_away_pct"] = nat_pred["prob_away"]
                if d.get("true_over25_pct") is None:
                    d["true_over25_pct"] = nat_pred["prob_over25"]
                    d["true_under25_pct"] = nat_pred["prob_under25"]
                if d.get("true_btts_yes_pct") is None:
                    d["true_btts_yes_pct"] = nat_pred["prob_btts_yes"]
                    d["true_btts_no_pct"] = nat_pred["prob_btts_no"]
            else:
                # If national_xg failed, try goals_markets for O/U and BTTS
                if predict_ou_and_btts and not d.get("true_over25_pct"):
                    try:
                        goals_pred = predict_ou_and_btts(d.get("home_team"), d.get("away_team"))
                        if goals_pred:
                            d["true_over25_pct"] = goals_pred["ou_over_25"]
                            d["true_under25_pct"] = goals_pred["ou_under_25"]
                            d["true_btts_yes_pct"] = goals_pred["btts_yes"]
                            d["true_btts_no_pct"] = goals_pred["btts_no"]
                            d["goals_ou_confidence"] = goals_pred["ou_confidence"]
                            d["goals_btts_confidence"] = goals_pred["btts_confidence"]
                    except Exception as e:
                        print(f"WARN: Goals markets error: {e}", flush=True)
        else:
            try:
                xg_sig = xg_based_probability(d.get("home_team"), d.get("away_team"))
            except Exception as e:
                print(f"WARN: xg_based error: {e}", flush=True)
                xg_sig = None
        d["xg_signal"] = xg_sig
        
        # ========== RECENT FORM (4th signal) ==========
        d["home_form_strength"] = "Unknown"
        d["away_form_strength"] = "Unknown"
        if calculate_recent_form and is_national:
            try:
                form_conn = get_connection()
                all_nat = form_conn.execute("SELECT home_team, away_team, home_goals, away_goals FROM national_matches ORDER BY date DESC LIMIT 100").fetchall()
                form_conn.close()
                match_list = [{"home_team": m["home_team"], "away_team": m["away_team"], "home_score": m["home_goals"], "away_score": m["away_goals"]} for m in all_nat]
                
                home_form = calculate_recent_form(d.get("home_team"), match_list, last_n=5)
                away_form = calculate_recent_form(d.get("away_team"), match_list, last_n=5)
                
                if home_form and away_form:
                    d["home_form_strength"] = home_form.get('form_strength', 'Unknown')
                    d["away_form_strength"] = away_form.get('form_strength', 'Unknown')
                    d["form_multiplier_home"] = home_form.get('form_multiplier', 1.0)
                    d["form_multiplier_away"] = away_form.get('form_multiplier', 1.0)
            except Exception as e:
                print(f"WARN: Recent form error: {e}", flush=True)
        
        if xg_sig and d.get("true_home_pct") is not None:
            # How much does xG disagree with Pinnacle?
            d["xg_vs_pin_home"] = round(xg_sig["home"] - d["true_home_pct"], 1)
            d["xg_vs_pin_away"] = round(xg_sig["away"] - d["true_away_pct"], 1)
            # Agreement score: small disagreement = both models agree = stronger signal
            disagree = abs(d["xg_vs_pin_home"]) + abs(d["xg_vs_pin_away"])
            d["xg_pin_agreement"] = "strong" if disagree < 10 else "moderate" if disagree < 25 else "weak"

        # === ELO SIGNAL ===
        sport = (d.get("sport_name") or "").lower()
        elo_sig = None
        try:
            if "world cup" in sport or "nations" in sport or "euro" in sport or "copa" in sport:
                elo_sig = elo_based_probability(d.get("home_team"), d.get("away_team"), "national")
            elif "tennis" in sport or "atp" in sport or "wta" in sport:
                elo_sig = elo_based_probability(d.get("home_team"), d.get("away_team"), "tennis", surface="All")
            else:
                elo_sig = elo_based_probability(d.get("home_team"), d.get("away_team"), "football_club")
        except Exception as e:
            print(f"WARN: elo error: {e}", flush=True)
            elo_sig = None
        d["elo_signal"] = elo_sig

        # === BETIQ UNIFIED PROBABILITY (fuse all signals) ===
        pin_probs = None
        if d.get("true_home_pct") is not None:
            pin_probs = {
                "home": d.get("true_home_pct"),
                "draw": d.get("true_draw_pct"),
                "away": d.get("true_away_pct"),
            }
        try:
            betiq = fuse_signals(pin_probs, xg_sig, elo_sig)
        except Exception as e:
            print(f"WARN: fuse_signals error: {e}", flush=True)
            betiq = None
        d["betiq_probs"] = betiq

        # ── A+B v2: when there's no 2nd SHARP market (Betfair), use our own
        # independent MODEL (xG/Elo) as the second opinion. The edge is still vs
        # Pinnacle (the market truth); the model only modulates CONFIDENCE:
        #   agree  → high trust (green, more stars)
        #   diverge_model → Pinnacle is still truth, but flag caution (green, capped stars)
        # (Sharp-vs-sharp 'diverge_sharp' is stronger and blocks green in the UI.)
        if d.get("ref_agreement") == "single" and betiq and betiq.get("n_signals", 0) >= 2:
            agr = betiq.get("agreement")
            if agr == "high":
                d["ref_agreement"] = "agree"
                d["ref_sources"] = "Pinnacle + modelo"
            elif agr == "low":
                d["ref_agreement"] = "diverge_model"
                d["ref_sources"] = "Pinnacle vs modelo"
            else:  # medium / other
                d["ref_sources"] = "Pinnacle + modelo (parcial)"

        # Recompute best value using BetIQ probability (if available and multi-signal)
        if betiq and betiq["n_signals"] >= 2:
            # Re-evaluate edges using fused probability for Match Result picks
            betiq_picks = []
            for p in picks:
                if p["market"] != "Match Result":
                    continue
                sel = p["selection"]
                true_p = None
                if sel == d.get("home_team"): true_p = betiq["home"]
                elif sel == d.get("away_team"): true_p = betiq["away"]
                elif sel == "Draw": true_p = betiq.get("draw")
                if true_p and p.get("book_odd"):
                    betiq_edge = (p["book_odd"] * true_p / 100 - 1) * 100
                    betiq_picks.append({**p, "betiq_true_prob": true_p, "betiq_edge": round(betiq_edge, 2)})
            d["betiq_picks"] = betiq_picks
            # Best BetIQ value pick
            credible_betiq = [p for p in betiq_picks if 2 <= p["betiq_edge"] <= 15]
            if credible_betiq:
                d["betiq_best"] = max(credible_betiq, key=lambda p: p["betiq_edge"])
            else:
                d["betiq_best"] = None

        # === LINE MOVEMENT ===
        movement = get_line_movement(d.get("event_id"))
        d["line_movement"] = movement

        # === SHARP CONFIRMATION ===
        # If best value pick goes same direction as line movement, signal is stronger
        if movement and best_value:
            sel = best_value.get("selection")
            mkt = best_value.get("market")
            home_team = d.get("home_team")
            away_team = d.get("away_team")
            direction = None
            if mkt == "Match Result":
                if sel == home_team: direction = movement["directions"].get("home")
                elif sel == away_team: direction = movement["directions"].get("away")
                elif sel == "Draw": direction = movement["directions"].get("draw")
            elif mkt == "Over/Under 2.5":
                if "Over" in sel: direction = movement["directions"].get("over25")
            elif mkt == "Both Teams to Score":
                if "Yes" in sel: direction = movement["directions"].get("btts_yes")
            # Odd dropped on our selection = sharp money agrees with us
            d["sharp_confirmation"] = (direction == "down")

        # === PUBLIC BIAS DETECTION ===
        # Flag when a popular club's market-implied prob is >10% above our model.
        # Indicates the public is inflating the favourite — value may be on the other side.
        public_bias = None
        home_name = (d.get("home_team") or "").strip()
        away_name = (d.get("away_team") or "").strip()
        import unicodedata as _ud
        def _nl(s):
            return " ".join(_ud.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").lower().split())

        for is_home, team, model_p, market_odd in [
            (True,  home_name, d.get("true_home_pct"), d.get("x1_home") or d.get("b365_home") or d.get("best_home")),
            (False, away_name, d.get("true_away_pct"), d.get("x1_away") or d.get("b365_away") or d.get("best_away")),
        ]:
            if _nl(team) not in POPULAR_CLUBS:
                continue
            if not model_p or not market_odd or market_odd <= 1:
                continue
            market_implied_pct = round(100 / market_odd, 1)
            gap = market_implied_pct - model_p
            if gap >= 8:
                other_team = away_name if is_home else home_name
                public_bias = {
                    "popular_team": team,
                    "other_team": other_team,
                    "market_implied": market_implied_pct,
                    "model_prob": model_p,
                    "gap_pct": round(gap, 1),
                    "severity": "high" if gap >= 15 else "medium",
                }
                break
        d["public_bias"] = public_bias

        # Legacy compatibility for old UI
        d["edges"] = {}
        for p in picks:
            if p["market"] != "Match Result": continue
            book = "1x" if p["book"] == "1xBet" else "best"
            sel = p["selection"]
            if sel == d.get("home_team"): d["edges"][f"home_{book}"] = p["edge_pct"]
            elif sel == d.get("away_team"): d["edges"][f"away_{book}"] = p["edge_pct"]
            elif sel == "Draw"  : d["edges"][f"draw_{book}"] = p["edge_pct"]

        # === BEST PICK + SAFEST PICK ===
        # Build a list of all available market predictions with our model probability
        market_picks = []
        # 1X2
        if d.get("true_home_pct") is not None:
            market_picks.append({
                "market": "Match Result",
                "selection": d.get("home_team"),
                "model_prob": d.get("true_home_pct"),
                "fair_odd": round(100.0 / d["true_home_pct"], 2) if d["true_home_pct"] > 0 else None,
            })
            if d.get("true_draw_pct") is not None and d["true_draw_pct"] > 0:
                market_picks.append({
                    "market": "Match Result",
                    "selection": "Draw",
                    "model_prob": d.get("true_draw_pct"),
                    "fair_odd": round(100.0 / d["true_draw_pct"], 2),
                })
            market_picks.append({
                "market": "Match Result",
                "selection": d.get("away_team"),
                "model_prob": d.get("true_away_pct"),
                "fair_odd": round(100.0 / d["true_away_pct"], 2) if d["true_away_pct"] > 0 else None,
            })
        # Over/Under 2.5
        if d.get("true_over25_pct") is not None:
            market_picks.append({
                "market": "Over/Under 2.5",
                "selection": "Over 2.5 goals",
                "model_prob": d["true_over25_pct"],
                "fair_odd": round(100.0 / d["true_over25_pct"], 2) if d["true_over25_pct"] > 0 else None,
            })
            market_picks.append({
                "market": "Over/Under 2.5",
                "selection": "Under 2.5 goals",
                "model_prob": d["true_under25_pct"],
                "fair_odd": round(100.0 / d["true_under25_pct"], 2) if d["true_under25_pct"] > 0 else None,
            })
        # BTTS
        if d.get("true_btts_yes_pct") is not None:
            market_picks.append({
                "market": "Both Teams To Score",
                "selection": "BTTS Yes",
                "model_prob": d["true_btts_yes_pct"],
                "fair_odd": round(100.0 / d["true_btts_yes_pct"], 2) if d["true_btts_yes_pct"] > 0 else None,
            })
            market_picks.append({
                "market": "Both Teams To Score",
                "selection": "BTTS No",
                "model_prob": d["true_btts_no_pct"],
                "fair_odd": round(100.0 / d["true_btts_no_pct"], 2) if d["true_btts_no_pct"] > 0 else None,
            })

        # Compute target odds (for 2% and 5% edge)
        for p in market_picks:
            if p["fair_odd"]:
                p["target_odd_2pct"] = round(p["fair_odd"] * 1.02, 2)
                p["target_odd_5pct"] = round(p["fair_odd"] * 1.05, 2)

                # Confidence: combines probability magnitude with model agreement
                # Check agreement of available signals for THIS specific selection
                agreement = "high"  # default if only one signal exists
                model_count = 0
                model_probs = []

                # Collect probabilities from each available model for this selection
                # 1X2 selections compare across pin/xg/elo; O/U & BTTS only from xG model.
                if p["market"] == "Match Result":
                    sel = p["selection"]
                    home_t = d.get("home_team")
                    away_t = d.get("away_team")

                    pin_p = None
                    if sel == home_t: pin_p = d.get("true_home_pct")
                    elif sel == away_t: pin_p = d.get("true_away_pct")
                    elif sel == "Draw": pin_p = d.get("true_draw_pct")

                    xg_p = None
                    if xg_sig:
                        if sel == home_t: xg_p = xg_sig.get("home")
                        elif sel == away_t: xg_p = xg_sig.get("away")
                        elif sel == "Draw": xg_p = xg_sig.get("draw")

                    elo_p = None
                    if elo_sig:
                        if sel == home_t: elo_p = elo_sig.get("home")
                        elif sel == away_t: elo_p = elo_sig.get("away")
                        elif sel == "Draw": elo_p = elo_sig.get("draw")

                    for prob in [pin_p, xg_p, elo_p]:
                        if prob is not None:
                            model_probs.append(prob)
                            model_count += 1
                else:
                    # O/U or BTTS — only xG model available
                    model_count = 1

                # Determine agreement quality (only meaningful with 2+ signals)
                if len(model_probs) >= 2:
                    max_diff = max(model_probs) - min(model_probs)
                    if max_diff < 8: agreement = "high"
                    elif max_diff < 18: agreement = "moderate"
                    else: agreement = "low"
                elif len(model_probs) == 1 or model_count == 1:
                    agreement = "single"

                # Base score from probability
                base_conf = 0
                if p["model_prob"] >= 65: base_conf = 3
                elif p["model_prob"] >= 55: base_conf = 2
                elif p["model_prob"] >= 45: base_conf = 1
                else: base_conf = 0

                # Adjustments by model agreement
                if agreement == "high" and len(model_probs) >= 2:
                    base_conf += 2  # Multiple models agree → strong signal
                elif agreement == "moderate":
                    base_conf += 1
                elif agreement == "low":
                    base_conf = max(1, base_conf - 1)  # Models diverge → cap low
                elif agreement == "single":
                    # Single model (no cross-check) → cap at 3 stars
                    pass

                # Hard cap: divergent models can never be 5⭐
                if agreement == "low":
                    base_conf = min(2, base_conf)
                elif agreement == "moderate":
                    base_conf = min(4, base_conf)
                elif agreement == "single":
                    base_conf = min(3, base_conf)

                p["confidence"] = max(1, min(5, base_conf))
                p["agreement"] = agreement
                p["models_used"] = len(model_probs) if model_probs else 1

        # SAFEST PICK: highest model probability (>= 55%)
        # Filter out picks with suspiciously high edges (likely model errors)
        safe_candidates = [
            p for p in market_picks
            if p["model_prob"] is not None and p["model_prob"] >= 55
            and not (p.get("fair_odd") and p.get("manual_odd")
                     and ((p["manual_odd"] * (p["model_prob"]/100) - 1) * 100) > 15)  # edge <15%
        ]
        if safe_candidates:
            d["safest_pick"] = max(safe_candidates, key=lambda p: p["model_prob"])
        else:
            d["safest_pick"] = None

        # BEST PICK: best balance of probability + edge potential
        # Also filter suspicious edges
        best_candidates = [
            p for p in market_picks
            if p["model_prob"] is not None
            and not (p.get("fair_odd") and p.get("manual_odd")
                     and ((p["manual_odd"] * (p["model_prob"]/100) - 1) * 100) > 15)
        ]
        
        # Sweet spot scoring for best picks
        def best_pick_score(p):
            prob = p["model_prob"] or 0
            if 50 <= prob <= 70: bonus = 1.0
            elif 45 <= prob < 50 or 70 < prob <= 80: bonus = 0.7
            else: bonus = 0.4
            return prob * bonus

        if best_candidates:
            d["best_pick"] = max(best_candidates, key=best_pick_score)
        else:
            d["best_pick"] = None

        d["all_market_picks"] = market_picks

        # Update best_confidence to max across best_pick, safest_pick, and best_value
        # (best_value confidence may be low even when model picks are high-confidence)
        conf_vals = [
            (d.get("best_pick") or {}).get("confidence", 0),
            (d.get("safest_pick") or {}).get("confidence", 0),
            d.get("best_confidence", 0),
        ]
        d["best_confidence"] = max(conf_vals)

        # === MANUAL ODDS — persisted user-entered odds ===
        eid = d.get("event_id")
        d["manual_odds"] = manual_odds_map.get(eid, {})

        # Compute "Best Manual Value" — the user's best edge from their entered odds
        best_manual = None
        for p in market_picks:
            stored_odd = d["manual_odds"].get(p["selection"])
            if stored_odd and stored_odd > 1 and p.get("model_prob"):
                edge_pct = (stored_odd * p["model_prob"] / 100 - 1) * 100
                if best_manual is None or edge_pct > best_manual["edge_pct"]:
                    kelly = 0
                    if edge_pct > 0:
                        kelly = (edge_pct / 100) / (stored_odd - 1) * 0.25
                    best_manual = {
                        "market": p["market"],
                        "selection": p["selection"],
                        "model_prob": p["model_prob"],
                        "fair_odd": p["fair_odd"],
                        "manual_odd": stored_odd,
                        "edge_pct": round(edge_pct, 2),
                        "kelly_pct": round(kelly * 100, 2),
                        "confidence": p.get("confidence", 0),
                    }
        d["best_manual_value"] = best_manual

        # Liquidity penalty: if Pinnacle margin > 4%, reduce best_confidence by 1
        if d.get("pin_low_liquidity") and d.get("best_confidence"):
            d["best_confidence"] = max(1, d["best_confidence"] - 1)

        results.append(d)

    results.sort(key=lambda x: x.get("commence_time") or "9999")

    # Store in cache
    _vb_cache["data"] = results
    _vb_cache["ts"] = time.time()
    _persist_vb_cache(results)  # survive cold starts
    # Clear session caches — data is now in _vb_cache
    _session_elo = None
    _session_xg = None
    _session_hist = None
    return results


# ===========================================================
# MANUAL ODDS — User-entered 1xBet odds, persisted per event/selection
# ===========================================================

def save_manual_odd(event_id, selection, odd):
    """Insert or update a manual odd for a (event, selection)."""
    from datetime import datetime
    if odd is None or odd <= 1:
        return False
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO manual_odds (event_id, selection, odd, updated_at)
            VALUES (?, ?, ?, ?)
        """, (event_id, selection, float(odd), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        invalidate_value_bets_cache()
        return True
    finally:
        conn.close()


def delete_manual_odd(event_id, selection):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM manual_odds WHERE event_id = ? AND selection = ?",
                     (event_id, selection))
        conn.commit()
        invalidate_value_bets_cache()
        return True
    finally:
        conn.close()


def get_manual_odds_map():
    """Returns dict {event_id: {selection: odd}} for all stored manual odds."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT event_id, selection, odd FROM manual_odds").fetchall()
        result = {}
        for r in rows:
            eid = r["event_id"] if "event_id" in r.keys() else r[0]
            sel = r["selection"] if "selection" in r.keys() else r[1]
            o = r["odd"] if "odd" in r.keys() else r[2]
            result.setdefault(eid, {})[sel] = o
        return result
    finally:
        conn.close()


# ===========================================================
# BET TRACKER — Functions for managing user bets
# ===========================================================

def add_bet(bet_data):
    """Insert a new bet. Returns the new bet ID."""
    from datetime import datetime
    conn = get_connection()
    cur = conn.cursor()

    pin_implied = None
    if bet_data.get("pin_implied_prob"):
        pin_implied = bet_data["pin_implied_prob"]

    edge_pct = None
    if bet_data.get("edge_pct") is not None:
        try:
            edge_pct = float(bet_data["edge_pct"])
        except (ValueError, TypeError):
            pass

    cur.execute("""
        INSERT INTO bets (
            placed_at, event_id, sport_name, home_team, away_team, commence_time,
            market, selection, bookmaker, odds, stake,
            pin_implied_prob, edge_pct, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        bet_data.get("placed_at") or datetime.now().strftime("%Y-%m-%d %H:%M"),
        bet_data.get("event_id"),
        bet_data.get("sport_name"),
        bet_data.get("home_team"),
        bet_data.get("away_team"),
        bet_data.get("commence_time"),
        bet_data.get("market", "h2h"),
        bet_data.get("selection"),
        bet_data.get("bookmaker"),
        float(bet_data.get("odds", 0)),
        float(bet_data.get("stake", 0)),
        pin_implied,
        edge_pct,
        bet_data.get("notes"),
    ))
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def update_bet_result(bet_id, result):
    """Mark bet as Won/Lost/Push and compute profit."""
    conn = get_connection()
    cur = conn.cursor()
    row = cur.execute("SELECT odds, stake FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not row:
        conn.close()
        return False
    odds = row["odds"] or 0
    stake = row["stake"] or 0
    if result == "won":
        profit = round(stake * (odds - 1), 2)
        status = "settled"
    elif result == "lost":
        profit = -stake
        status = "settled"
    elif result == "push":
        profit = 0.0
        status = "settled"
    else:
        profit = 0.0
        status = "pending"

    from datetime import datetime
    cur.execute("""
        UPDATE bets SET result=?, profit=?, status=?, settled_at=?
        WHERE id=?
    """, (result, profit, status, datetime.now().strftime("%Y-%m-%d %H:%M"), bet_id))
    conn.commit()
    conn.close()
    return True


def delete_bet(bet_id):
    conn = get_connection()
    conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    conn.commit()
    conn.close()
    return True


def get_bets(limit=200):
    """Return all bets, newest first, with CLV calculated where available."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM bets ORDER BY placed_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    bets = []
    for r in rows:
        d = dict(r)
        # Realized CLV = your odds vs the DEVIGGED Pinnacle close (no-vig CLV):
        # CLV % = (your_odds / pin_close_fair_odds - 1) × 100. Measured against the
        # raw close a bet could look +CLV while sitting inside the bookmaker's
        # margin (~2-4pp inflation) — no-vig is the honest ruler: >0 means real
        # value at the close.
        # Realized CLV only once the bet is SETTLED — i.e. the match has actually
        # happened. Before that, a captured "close" can be premature (esp. tennis,
        # whose scheduled time != actual start), so we report it as pending (None)
        # rather than a definitive-looking number.
        if d.get("status") == "settled" and d.get("odds") and d.get("pin_close_fair_odds"):
            d["clv_pct"] = round((d["odds"] / d["pin_close_fair_odds"] - 1) * 100, 2)
        else:
            d["clv_pct"] = None
        bets.append(d)
    return bets


def get_bet_stats():
    """Return aggregated stats: total bets, ROI, profit, win rate, CLV avg."""
    conn = get_connection()

    settled = conn.execute("""
        SELECT COUNT(*) as n,
               SUM(stake) as total_staked,
               SUM(profit) as total_profit,
               SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN result='push' THEN 1 ELSE 0 END) as pushes
        FROM bets WHERE status='settled'
    """).fetchone()

    pending = conn.execute("""
        SELECT COUNT(*) as n, SUM(stake) as staked
        FROM bets WHERE status='pending'
    """).fetchone()

    # CLV stats — only SETTLED bets with a real captured close (consistent with the
    # per-bet rule: no premature/pending closes in the aggregate CLV). No-vig:
    # measured against the DEVIGGED close, same ruler as the per-bet clv_pct.
    clv = conn.execute("""
        SELECT odds, pin_close_fair_odds FROM bets
        WHERE status='settled' AND pin_close_fair_odds IS NOT NULL AND odds IS NOT NULL
    """).fetchall()

    clv_values = []
    for row in clv:
        try:
            clv_values.append((row["odds"] / row["pin_close_fair_odds"] - 1) * 100)
        except Exception:
            pass

    avg_clv = round(sum(clv_values) / len(clv_values), 2) if clv_values else None
    positive_clv_pct = round(sum(1 for v in clv_values if v > 0) / len(clv_values) * 100, 1) if clv_values else None

    conn.close()

    total_staked = settled["total_staked"] or 0
    total_profit = settled["total_profit"] or 0
    roi = round((total_profit / total_staked * 100), 2) if total_staked > 0 else 0
    n_settled = settled["n"] or 0
    decisive = (settled["wins"] or 0) + (settled["losses"] or 0)
    win_rate = round((settled["wins"] or 0) / decisive * 100, 1) if decisive > 0 else 0

    return {
        "n_settled": n_settled,
        "n_pending": pending["n"] or 0,
        "pending_stake": round(pending["staked"] or 0, 2),
        "total_staked": round(total_staked, 2),
        "total_profit": round(total_profit, 2),
        "roi": roi,
        "wins": settled["wins"] or 0,
        "losses": settled["losses"] or 0,
        "pushes": settled["pushes"] or 0,
        "win_rate": win_rate,
        "avg_clv": avg_clv,
        "positive_clv_rate": positive_clv_pct,
        "clv_sample": len(clv_values),
    }


def get_bankroll_evolution():
    """Return list of settled bets for bankroll chart, with result + selection for markers."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT settled_at, profit, result, selection, home_team, away_team FROM bets
        WHERE status='settled' AND settled_at IS NOT NULL
        ORDER BY settled_at ASC
    """).fetchall()
    conn.close()

    cumulative = 0
    series = []
    for r in rows:
        cumulative += r["profit"] or 0
        series.append({
            "date": r["settled_at"],
            "result": r["result"],
            "selection": r["selection"] or '',
            "profit": round(r["profit"] or 0, 2),
            "cumulative": round(cumulative, 2),
            "delta": round(r["profit"] or 0, 2),
        })
    return series


def _parse_ts(value):
    """Tolerant timestamp parser for the mixed formats stored in the DB:
    'YYYY-MM-DD HH:MM[:SS]', 'YYYY-MM-DDTHH:MM[:SS][Z]', with optional
    fractional seconds / '+00:00' offset, or a datetime object.
    Returns a naive UTC datetime, or None."""
    from datetime import datetime as _dt
    if value is None:
        return None
    if isinstance(value, _dt):
        return value.replace(tzinfo=None)
    s = str(value).strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    plus = s.find("+", 10)
    if plus != -1:
        s = s[:plus]
    for length, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M")):
        try:
            return _dt.strptime(s[:length], fmt)
        except ValueError:
            continue
    return None


def _pin_price_for_bet(snapshot, market, sel, home_lower, away_lower):
    """Pinnacle price in an odds_history snapshot for this bet's market/selection."""
    # Match Result / 1X2
    if market in ("h2h", "match result", "1x2"):
        if sel == home_lower or sel in ("home", "1"):
            return snapshot["pin_home"]
        if sel == away_lower or sel in ("away", "2"):
            return snapshot["pin_away"]
        if sel in ("draw", "empate", "x"):
            return snapshot["pin_draw"]
    # Over/Under 2.5
    elif market in ("over_under", "over/under 2.5", "totals"):
        if "over" in sel:
            return snapshot["pin_over25"]
        if "under" in sel:
            return snapshot["pin_under25"]
    # BTTS
    elif market in ("btts", "both teams to score"):
        if "yes" in sel or "sim" in sel:
            return snapshot["pin_btts_yes"]
        if "no" in sel or "não" in sel or "nao" in sel:
            return snapshot["pin_btts_no"]
    return None


def _fair_close_prob(snapshot, market, sel, home_lower, away_lower):
    """Devigged (no-vig, power method) close probability for the bet's selection,
    from a full odds_history snapshot. Requires the counter-price(s) so the vig
    can actually be stripped — returns None when they're missing rather than
    normalizing a one-sided market to 100%."""
    if market in ("h2h", "match result", "1x2"):
        if not (snapshot["pin_home"] and snapshot["pin_away"]):
            return None
        if sel in ("draw", "empate", "x") and not snapshot["pin_draw"]:
            return None
        fair = remove_vig_power(snapshot["pin_home"], snapshot["pin_draw"], snapshot["pin_away"])
        if not fair:
            return None
        if sel == home_lower or sel in ("home", "1"):
            p = fair["home"]
        elif sel == away_lower or sel in ("away", "2"):
            p = fair["away"]
        elif sel in ("draw", "empate", "x"):
            p = fair["draw"]
        else:
            p = None
    elif market in ("over_under", "over/under 2.5", "totals"):
        if not (snapshot["pin_over25"] and snapshot["pin_under25"]):
            return None
        fair = remove_vig_power(snapshot["pin_over25"], None, snapshot["pin_under25"])
        if not fair:
            return None
        p = fair["home"] if "over" in sel else fair["away"] if "under" in sel else None
    elif market in ("btts", "both teams to score"):
        if not (snapshot["pin_btts_yes"] and snapshot["pin_btts_no"]):
            return None
        fair = remove_vig_power(snapshot["pin_btts_yes"], None, snapshot["pin_btts_no"])
        if not fair:
            return None
        if "yes" in sel or "sim" in sel:
            p = fair["home"]
        elif "no" in sel or "não" in sel or "nao" in sel:
            p = fair["away"]
        else:
            p = None
    else:
        return None
    return (p / 100.0) if p else None


def capture_pinnacle_close_for_started_events(recapture=False):
    """
    Capture the TRUE closing Pinnacle odds (last snapshot at/before kickoff)
    from odds_history. Supports 1X2, Over/Under 2.5, and BTTS markets.
    Run automatically after odds refresh.

    Timestamps are compared in Python (_parse_ts), NOT in SQL: bets.commence_time
    is stored as 'YYYY-MM-DDTHH:MM' while odds_history.captured_at is
    'YYYY-MM-DD HH:MM:SS', and a raw string comparison between the two formats
    (' ' sorts before 'T') made EVERY same-day snapshot — including in-play
    prices written after kickoff — pass the "before kickoff" filter, so the
    stored "close" could be a live price instead of the real closing line.

    recapture=True re-derives the close for ALL bets with an event_id (repairs
    values captured by the old comparison). Bets whose history has no genuine
    pre-kickoff snapshot get their close cleared back to NULL (CLV pending) —
    an honest blank instead of a fabricated number.

    Alongside the raw close we store pin_close_fair_odds — the DEVIGGED (no-vig,
    power method) close for the bet's selection. Realized CLV is measured against
    THIS fair close: vs the raw price a bet can "beat the close" by merely sitting
    inside Pinnacle's margin, which inflates CLV by ~2-4pp and is not real value.
    Normal mode also backfills fair odds for bets captured before this column
    existed (runs automatically after odds refresh / on My Bets load).
    """
    from datetime import datetime as _dt
    conn = get_connection()
    where_close = "" if recapture else \
        "AND (b.pin_close_odds IS NULL OR b.pin_close_fair_odds IS NULL)"
    rows = conn.execute(f"""
        SELECT b.id, b.selection, b.market, b.home_team, b.away_team,
               b.event_id, b.commence_time, b.pin_close_odds
        FROM bets b
        WHERE b.event_id IS NOT NULL
          AND b.commence_time IS NOT NULL
          {where_close}
    """).fetchall()

    now = _dt.utcnow()
    updated = 0
    for r in rows:
        kickoff = _parse_ts(r["commence_time"])
        if not kickoff or kickoff > now:
            continue  # not kicked off yet (or unparseable) — nothing to capture

        snapshots = conn.execute("""
            SELECT pin_home, pin_draw, pin_away,
                   pin_over25, pin_under25,
                   pin_btts_yes, pin_btts_no,
                   captured_at
            FROM odds_history
            WHERE event_id = ?
        """, (r["event_id"],)).fetchall()

        sel = (r["selection"] or "").strip().lower()
        market = (r["market"] or "h2h").lower()
        home_lower = (r["home_team"] or "").lower()
        away_lower = (r["away_team"] or "").lower()

        # Latest snapshot at/before kickoff that actually quotes our selection.
        # NO fallback to post-kickoff snapshots or current odds_events: those are
        # live/near-now prices, which would fabricate a closing line (= CLV vs now,
        # not a real close). If there is no genuine PRE-kickoff snapshot, leave
        # pin_close NULL so the CLV stays pending — never a faked number.
        best_ts, pin_close, close_at, close_snap = None, None, None, None
        for s in snapshots:
            ts = _parse_ts(s["captured_at"])
            if ts is None or ts > kickoff:
                continue
            price = _pin_price_for_bet(s, market, sel, home_lower, away_lower)
            if price and (best_ts is None or ts > best_ts):
                best_ts, pin_close, close_at, close_snap = ts, price, s["captured_at"], s

        if pin_close:
            fair_prob = _fair_close_prob(close_snap, market, sel, home_lower, away_lower)
            fair_odds = round(1.0 / fair_prob, 3) if fair_prob else None
            conn.execute("""
                UPDATE bets SET pin_close_odds=?, pin_close_implied=?,
                                pin_close_fair_odds=?, pin_close_captured_at=?
                WHERE id=?
            """, (pin_close, round(1.0 / pin_close * 100, 2), fair_odds, close_at, r["id"]))
            if r["pin_close_odds"] != pin_close or recapture:
                updated += 1
        elif recapture and r["pin_close_odds"] is not None:
            conn.execute("""
                UPDATE bets SET pin_close_odds=NULL, pin_close_implied=NULL,
                                pin_close_fair_odds=NULL, pin_close_captured_at=NULL
                WHERE id=?
            """, (r["id"],))
            updated += 1

    conn.commit()
    conn.close()
    return updated


def clv_audit(bet_id):
    """Full audit trail for one bet's CLV: entry odd, the captured Pinnacle close
    (value + when it was captured relative to kickoff), the step-by-step math,
    and the event's entire snapshot timeline so a wrong close is visible at a glance."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not row:
        conn.close()
        return None
    b = dict(row)
    kickoff = _parse_ts(b.get("commence_time"))
    sel = (b.get("selection") or "").strip().lower()
    market = (b.get("market") or "h2h").lower()
    home_lower = (b.get("home_team") or "").lower()
    away_lower = (b.get("away_team") or "").lower()

    timeline = []
    if b.get("event_id"):
        snaps = conn.execute("""
            SELECT pin_home, pin_draw, pin_away, pin_over25, pin_under25,
                   pin_btts_yes, pin_btts_no, captured_at
            FROM odds_history WHERE event_id = ? ORDER BY captured_at ASC
        """, (b["event_id"],)).fetchall()
        for s in snaps:
            ts = _parse_ts(s["captured_at"])
            price = _pin_price_for_bet(s, market, sel, home_lower, away_lower)
            mins_before = (round((kickoff - ts).total_seconds() / 60.0, 1)
                           if (kickoff and ts) else None)
            timeline.append({
                "captured_at": s["captured_at"],
                "pin_price_for_selection": price,
                "minutes_before_kickoff": mins_before,
                "pre_kickoff": (mins_before is not None and mins_before >= 0),
                "is_stored_close": (price is not None and price == b.get("pin_close_odds")
                                    and s["captured_at"] == b.get("pin_close_captured_at")),
            })
    conn.close()

    clv_steps = None
    if b.get("odds") and b.get("pin_close_odds"):
        fair = b.get("pin_close_fair_odds")
        clv_steps = {
            "your_odds": b["odds"],
            "your_implied_pct": round(100.0 / b["odds"], 2),
            "pin_close_odds_raw": b["pin_close_odds"],
            "raw_close_implied_pct": round(100.0 / b["pin_close_odds"], 2),
            "pin_close_fair_odds": fair,
            "fair_close_prob_pct": round(100.0 / fair, 2) if fair else None,
            "formula": "CLV% = (your_odds / pin_close_FAIR_odds - 1) × 100  [no-vig]",
            "clv_pct": round((b["odds"] / fair - 1) * 100, 2) if fair else None,
            "clv_vs_raw_close_pct": round((b["odds"] / b["pin_close_odds"] - 1) * 100, 2),
            "note": ("CLV medido contra o fecho DEVIGGED (no-vig, método power) da Pinnacle — "
                     ">0 significa valor real no fecho, não apenas ter batido a margem da casa. "
                     "clv_vs_raw_close_pct é a régua antiga (fecho cru), só para referência. "
                     "Independente do resultado da aposta."),
        }

    warnings = []
    close_ts = _parse_ts(b.get("pin_close_captured_at"))
    if b.get("pin_close_odds") is not None and b.get("pin_close_fair_odds") is None:
        warnings.append("Fecho cru capturado mas sem devig possível (falta o preço do outro lado "
                        "no snapshot) — CLV fica pendente em vez de usar a régua inflacionada.")
    if b.get("pin_close_odds") is not None and b.get("pin_close_captured_at") is None:
        warnings.append("Fecho capturado pela versão antiga do código (sem timestamp) — "
                        "pode ser um preço live/pós-kickoff. Corre POST /api/clv/capture?recapture=true.")
    if close_ts and kickoff:
        mins = (kickoff - close_ts).total_seconds() / 60.0
        if mins < 0:
            warnings.append(f"Fecho capturado {abs(mins):.0f} min DEPOIS do kickoff (preço live) — inválido; recaptura necessária.")
        elif mins > 120:
            warnings.append(f"Fecho capturado {mins:.0f} min antes do kickoff — linha antiga, não um verdadeiro fecho (~15 min).")
    if b.get("pin_close_odds") is None:
        warnings.append("Sem linha de fecho capturada — CLV pendente (não há snapshot Pinnacle pré-kickoff para este evento/mercado).")

    return {
        "bet": {k: b.get(k) for k in (
            "id", "placed_at", "sport_name", "home_team", "away_team", "selection",
            "market", "bookmaker", "odds", "stake", "status", "result", "profit",
            "commence_time", "event_id",
            "pin_close_odds", "pin_close_implied", "pin_close_fair_odds",
            "pin_close_captured_at")},
        "kickoff_utc": b.get("commence_time"),
        "clv": clv_steps,
        "snapshots": timeline,
        "warnings": warnings,
    }


def auto_grade_pending_bets():
    """
    Auto-settle pending bets by matching against stored match results
    (football_matches, national_matches). Supports Match Result, O/U 2.5, BTTS.
    Returns number of bets graded.
    """
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    conn = get_connection()
    # Only attempt bets where commence_time is > 2.5h in the past
    cutoff = (_dt.utcnow() - _td(hours=2, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT id, event_id, sport_name, home_team, away_team, commence_time,
               market, selection, odds, stake
        FROM bets
        WHERE status = 'pending'
          AND commence_time IS NOT NULL
          AND commence_time < ?
    """, (cutoff,)).fetchall()

    graded = 0
    for bet in rows:
        try:
            market = (bet["market"] or "h2h").lower()
            selection = (bet["selection"] or "").strip().lower()
            home = (bet["home_team"] or "").strip()
            away = (bet["away_team"] or "").strip()
            date_str = (bet["commence_time"] or "")[:10]
            if not home or not away or not date_str:
                continue

            # Date window: same day ± 1 day to handle timezone differences
            try:
                d0 = _dt.strptime(date_str, "%Y-%m-%d")
                d1 = (d0 + _td(days=2)).strftime("%Y-%m-%d")
                d_from = (d0 - _td(days=1)).strftime("%Y-%m-%d")
            except Exception:
                d1 = date_str
                d_from = date_str

            match_row = None
            # Try football_matches
            match_row = conn.execute("""
                SELECT home_goals, away_goals FROM football_matches
                WHERE LOWER(home_team) = LOWER(?) AND LOWER(away_team) = LOWER(?)
                  AND date >= ? AND date <= ? AND home_goals IS NOT NULL
                ORDER BY date DESC LIMIT 1
            """, (home, away, d_from, d1)).fetchone()

            # Try national_matches
            if not match_row:
                match_row = conn.execute("""
                    SELECT home_goals, away_goals FROM national_matches
                    WHERE LOWER(home_team) = LOWER(?) AND LOWER(away_team) = LOWER(?)
                      AND date >= ? AND date <= ? AND home_goals IS NOT NULL
                    ORDER BY date DESC LIMIT 1
                """, (home, away, d_from, d1)).fetchone()

            if not match_row:
                continue

            hg = match_row["home_goals"] if hasattr(match_row, "keys") else match_row[0]
            ag = match_row["away_goals"] if hasattr(match_row, "keys") else match_row[1]
            if hg is None or ag is None:
                continue

            # Determine bet outcome
            outcome = None
            home_lower = home.lower()
            away_lower = away.lower()

            if market in ("h2h", "match result", "1x2"):
                actual = "H" if hg > ag else ("D" if hg == ag else "A")
                if selection in (home_lower, "home", "1", "h"):
                    outcome = "won" if actual == "H" else "lost"
                elif selection in (away_lower, "away", "2", "a"):
                    outcome = "won" if actual == "A" else "lost"
                elif selection in ("draw", "x", "d", "empate"):
                    outcome = "won" if actual == "D" else "lost"

            elif market in ("over_under", "over/under 2.5", "totals"):
                total = hg + ag
                line = 2.5
                m = _re.search(r'(\d+\.?\d*)', selection)
                if m:
                    line = float(m.group(1))
                if "over" in selection:
                    outcome = "won" if total > line else ("push" if total == line else "lost")
                elif "under" in selection:
                    outcome = "won" if total < line else ("push" if total == line else "lost")

            elif market in ("btts", "both teams to score"):
                btts = (hg > 0 and ag > 0)
                if "yes" in selection or "sim" in selection:
                    outcome = "won" if btts else "lost"
                elif "no" in selection or "nao" in selection or "não" in selection:
                    outcome = "won" if not btts else "lost"

            if outcome is None:
                continue

            odds = bet["odds"] or 0
            stake = bet["stake"] or 0
            profit = round(stake * (odds - 1), 2) if outcome == "won" else (-stake if outcome == "lost" else 0.0)

            conn.execute("""
                UPDATE bets
                SET status='settled', result=?, profit=?, settled_at=datetime('now'),
                    notes=CASE WHEN notes IS NULL THEN '[auto-graded]'
                               ELSE notes || ' [auto-graded]' END
                WHERE id=?
            """, (outcome, profit, bet["id"]))
            graded += 1

        except Exception as e:
            print(f"auto_grade error bet {bet['id']}: {e}", flush=True)

    if graded:
        conn.commit()
    conn.close()
    return graded


def get_national_summary():
    """Top tournaments + counts."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT tournament, COUNT(*) as n,
               MIN(date) as first_date, MAX(date) as last_date
        FROM national_matches
        GROUP BY tournament
        ORDER BY n DESC
        LIMIT 12
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_national_recent(limit=50, team=None):
    conn = get_connection()
    if team:
        rows = conn.execute("""
            SELECT * FROM national_matches
            WHERE (home_team LIKE ? OR away_team LIKE ?)
            ORDER BY date DESC LIMIT ?
        """, (f"%{team}%", f"%{team}%", limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM national_matches
            ORDER BY date DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_team_xg(team_name):
    """Lookup xG stats for a team (fuzzy match on name)."""
    if not team_name:
        return None

    # Use pre-loaded session cache if available (avoids new Neon connection per call)
    if _session_xg is not None:
        key = team_name.lower()
        row = _session_xg.get(key)
        if not row:
            normalized = team_name.replace("FC ", "").replace(" FC", "").strip().lower()
            for k, v in _session_xg.items():
                if normalized in k or k.startswith(normalized):
                    row = v
                    break
        return row

    conn = get_connection()
    # Exact match first
    row = conn.execute("SELECT * FROM team_xg WHERE team = ?", (team_name,)).fetchone()
    if not row:
        # Fuzzy: try with normalized variations (Understat uses "Manchester United", odds API may use different)
        normalized = team_name.replace("FC ", "").replace(" FC", "").strip()
        row = conn.execute(
            "SELECT * FROM team_xg WHERE team LIKE ? OR team LIKE ?",
            (f"%{normalized}%", f"{normalized}%")
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_xg_summary():
    """Return league-level xG coverage summary."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT league_name,
               COUNT(*) as matches,
               SUM(CASE WHEN is_result = 1 THEN 1 ELSE 0 END) as completed,
               MAX(date) as last_match
        FROM understat_matches
        GROUP BY league_name
        ORDER BY matches DESC
    """).fetchall()
    teams_count = conn.execute("SELECT COUNT(*) FROM team_xg").fetchone()[0]
    conn.close()
    return {
        "leagues": [dict(r) for r in rows],
        "teams_indexed": teams_count,
    }


def xg_based_probability(home_team, away_team):
    """
    Estimate match outcome probabilities from xG rolling averages (Poisson model).
    Returns dict with 1X2, O/U 2.5, BTTS probabilities OR None if no xG data.
    """
    h_xg = get_team_xg(home_team)
    a_xg = get_team_xg(away_team)
    if not h_xg or not a_xg:
        return None

    home_lambda = (h_xg["avg_xg_for"] + a_xg["avg_xg_against"]) / 2 * 1.10
    away_lambda = (a_xg["avg_xg_for"] + h_xg["avg_xg_against"]) / 2 * 0.95
    home_lambda = max(0.3, min(4.0, home_lambda))
    away_lambda = max(0.3, min(4.0, away_lambda))

    import math
    def poisson(k, lam):
        return (lam ** k) * math.exp(-lam) / math.factorial(k)

    max_goals = 7
    p_home = p_draw = p_away = 0.0
    p_over25 = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson(h, home_lambda) * poisson(a, away_lambda)
            if h > a: p_home += p
            elif h == a: p_draw += p
            else: p_away += p
            if h + a > 2.5: p_over25 += p

    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total; p_draw /= total; p_away /= total

    p_btts_no = poisson(0, home_lambda) + poisson(0, away_lambda) - poisson(0, home_lambda) * poisson(0, away_lambda)
    p_btts_yes = 1 - p_btts_no

    return {
        "home": round(p_home * 100, 1),
        "draw": round(p_draw * 100, 1),
        "away": round(p_away * 100, 1),
        "over25": round(p_over25 * 100, 1),
        "under25": round((1 - p_over25) * 100, 1),
        "btts_yes": round(p_btts_yes * 100, 1),
        "btts_no": round(p_btts_no * 100, 1),
        "expected_home_goals": round(home_lambda, 2),
        "expected_away_goals": round(away_lambda, 2),
        "home_xg_form": h_xg,
        "away_xg_form": a_xg,
    }


def get_line_movement(event_id):
    """
    Returns line movement data. Uses best available reference per snapshot:
    pin_ (Pinnacle/Betfair) → best_ (market best) → x1_ (1xBet).

    Smart Money threshold: absolute odds change >= 0.20 on any outcome.
    Steam threshold: >5% relative move in last 6 hours.
    """
    # Use pre-loaded session cache if available (avoids new Neon connection per event)
    if _session_hist is not None:
        rows = _session_hist.get(event_id, [])
    else:
        conn = get_connection()
        rows = conn.execute("""
            SELECT * FROM odds_history
            WHERE event_id = ?
            ORDER BY captured_at ASC
        """, (event_id,)).fetchall()
        conn.close()

    if len(rows) < 2:
        return None

    opening = dict(rows[0])
    latest = dict(rows[-1])

    def best_ref(row, side):
        return row.get(f"pin_{side}") or row.get(f"best_{side}") or row.get(f"x1_{side}")

    def pct_change(old, new):
        if not old or not new or old <= 0:
            return None
        return round((new - old) / old * 100, 1)

    def direction(pct):
        if pct is None: return None
        if pct < -2: return "down"
        if pct > 2: return "up"
        return "flat"

    sides = ["home", "draw", "away"]
    movements = {}
    abs_changes = {}
    for side in sides:
        o = best_ref(opening, side)
        l = best_ref(latest, side)
        movements[side] = pct_change(o, l)
        abs_changes[side] = round(l - o, 3) if (o and l) else None

    movements["over25"]  = pct_change(opening.get("pin_over25"),  latest.get("pin_over25"))
    movements["btts_yes"] = pct_change(opening.get("pin_btts_yes"), latest.get("pin_btts_yes"))

    # Smart Money: absolute odds move >= 0.20 (strong) or >= 0.10 (moderate)
    smart_money = {}
    for side in sides:
        ch = abs_changes.get(side)
        if ch is not None and abs(ch) >= 0.10:
            smart_money[side] = {
                "abs_change": ch,
                "direction": "in" if ch < 0 else "out",
                "strength": "strong" if abs(ch) >= 0.20 else "moderate",
            }

    has_strong_movement = any(v["strength"] == "strong" for v in smart_money.values())

    # Steam: >5% move in last 6 hours
    from datetime import datetime, timedelta
    try:
        latest_dt = datetime.strptime(latest["captured_at"][:19], "%Y-%m-%d %H:%M:%S")
        cutoff = latest_dt - timedelta(hours=6)
        recent = [r for r in rows
                  if datetime.strptime(r["captured_at"][:19], "%Y-%m-%d %H:%M:%S") >= cutoff]
    except Exception:
        recent = rows

    steam = {}
    if len(recent) >= 2:
        first = dict(recent[0])
        for side in sides:
            old = best_ref(first, side)
            new = best_ref(latest, side)
            ch = pct_change(old, new)
            if ch is not None and abs(ch) >= 5:
                steam[side] = {"direction": "in" if ch < 0 else "out", "pct": ch}

    # Opening vs latest for display
    opening_display = {s: best_ref(opening, s) for s in sides}
    latest_display  = {s: best_ref(latest,  s) for s in sides}

    # Sparkline (max 30 points, using best reference)
    step = max(1, len(rows) // 30)
    sparkline = []
    for i, r in enumerate(rows):
        if i % step == 0 or i == len(rows) - 1:
            rd = dict(r)
            sparkline.append({
                "t": rd["captured_at"],
                "pin_home": best_ref(rd, "home"),
                "pin_draw": best_ref(rd, "draw"),
                "pin_away": best_ref(rd, "away"),
            })

    return {
        "opening": opening_display,
        "latest": latest_display,
        "movements": movements,
        "abs_changes": abs_changes,
        "directions": {k: direction(v) for k, v in movements.items()},
        "steam": steam,
        "smart_money": smart_money,
        "has_strong_movement": has_strong_movement,
        "snapshots": len(rows),
        "sparkline": sparkline,
    }


# ==========================================================
# PERFORMANCE ANALYTICS — for Bankroll Dashboard
# ==========================================================

def get_performance_breakdown():
    """
    Returns ROI/profit breakdown by sport, bookmaker, market, confidence band.
    Used by the Performance dashboard.
    """
    conn = get_connection()

    def aggregate(group_by_col):
        rows = conn.execute(f"""
            SELECT
                COALESCE({group_by_col}, 'Unknown') as bucket,
                COUNT(*) as n,
                SUM(stake) as staked,
                SUM(profit) as profit,
                SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result='push' THEN 1 ELSE 0 END) as pushes
            FROM bets
            WHERE status='settled'
            GROUP BY bucket
            ORDER BY staked DESC
        """).fetchall()
        out = []
        for r in rows:
            staked = r["staked"] or 0
            profit = r["profit"] or 0
            decisive = (r["wins"] or 0) + (r["losses"] or 0)
            out.append({
                "bucket": r["bucket"],
                "n": r["n"],
                "staked": round(staked, 2),
                "profit": round(profit, 2),
                "roi": round((profit / staked * 100), 2) if staked > 0 else 0,
                "wins": r["wins"],
                "losses": r["losses"],
                "pushes": r["pushes"],
                "win_rate": round((r["wins"] or 0) / decisive * 100, 1) if decisive > 0 else 0,
            })
        return out

    by_sport = aggregate("sport_name")
    by_bookmaker = aggregate("bookmaker")
    by_market = aggregate("market")

    # By odds range
    odds_buckets = conn.execute("""
        SELECT
            CASE
                WHEN odds < 1.5 THEN 'Heavy fav (< 1.50)'
                WHEN odds < 2.0 THEN 'Fav (1.50–2.00)'
                WHEN odds < 3.0 THEN 'Even (2.00–3.00)'
                WHEN odds < 5.0 THEN 'Underdog (3.00–5.00)'
                ELSE 'Long shot (5.00+)'
            END as bucket,
            COUNT(*) as n,
            SUM(stake) as staked,
            SUM(profit) as profit,
            SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses
        FROM bets WHERE status='settled'
        GROUP BY bucket
    """).fetchall()
    by_odds = []
    for r in odds_buckets:
        staked = r["staked"] or 0
        profit = r["profit"] or 0
        decisive = (r["wins"] or 0) + (r["losses"] or 0)
        by_odds.append({
            "bucket": r["bucket"],
            "n": r["n"],
            "staked": round(staked, 2),
            "profit": round(profit, 2),
            "roi": round((profit / staked * 100), 2) if staked > 0 else 0,
            "win_rate": round((r["wins"] or 0) / decisive * 100, 1) if decisive > 0 else 0,
        })

    conn.close()
    return {
        "by_sport": by_sport,
        "by_bookmaker": by_bookmaker,
        "by_market": by_market,
        "by_odds": by_odds,
    }


def get_bankroll_advanced(starting_bankroll=1000):
    """
    Detailed bankroll evolution + drawdown analysis.
    Returns time series + key statistics.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, placed_at, settled_at, stake, profit, result, selection, sport_name
        FROM bets
        WHERE status='settled' AND settled_at IS NOT NULL
        ORDER BY settled_at ASC, id ASC
    """).fetchall()
    conn.close()

    if not rows:
        return {
            "series": [],
            "starting_bankroll": starting_bankroll,
            "current_bankroll": starting_bankroll,
            "peak_bankroll": starting_bankroll,
            "max_drawdown_pct": 0,
            "max_drawdown_abs": 0,
            "current_drawdown_pct": 0,
            "longest_winning_streak": 0,
            "longest_losing_streak": 0,
            "current_streak": {"type": None, "count": 0},
        }

    series = []
    cumulative = starting_bankroll
    peak = starting_bankroll
    max_drawdown = 0
    max_drawdown_abs = 0

    current_streak_type = None
    current_streak_count = 0
    longest_win_streak = 0
    longest_lose_streak = 0

    for r in rows:
        profit = r["profit"] or 0
        cumulative += profit
        if cumulative > peak:
            peak = cumulative
        drawdown_abs = peak - cumulative
        drawdown_pct = (drawdown_abs / peak * 100) if peak > 0 else 0
        if drawdown_pct > max_drawdown:
            max_drawdown = drawdown_pct
            max_drawdown_abs = drawdown_abs

        # Streak tracking (only Wins and Losses count, Push neutral)
        result = r["result"]
        if result == "won":
            if current_streak_type == "win":
                current_streak_count += 1
            else:
                current_streak_type = "win"
                current_streak_count = 1
            longest_win_streak = max(longest_win_streak, current_streak_count)
        elif result == "lost":
            if current_streak_type == "loss":
                current_streak_count += 1
            else:
                current_streak_type = "loss"
                current_streak_count = 1
            longest_lose_streak = max(longest_lose_streak, current_streak_count)

        series.append({
            "date": r["settled_at"],
            "bankroll": round(cumulative, 2),
            "delta": round(profit, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "selection": r["selection"],
            "sport": r["sport_name"],
            "result": result,
        })

    current_dd_pct = ((peak - cumulative) / peak * 100) if peak > 0 else 0

    return {
        "series": series,
        "starting_bankroll": starting_bankroll,
        "current_bankroll": round(cumulative, 2),
        "peak_bankroll": round(peak, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "max_drawdown_abs": round(max_drawdown_abs, 2),
        "current_drawdown_pct": round(current_dd_pct, 2),
        "longest_winning_streak": longest_win_streak,
        "longest_losing_streak": longest_lose_streak,
        "current_streak": {"type": current_streak_type, "count": current_streak_count},
    }


# ==========================================================
# ELO — rating lookup and match probability
# ==========================================================

def get_elo_rating(entity, category, surface=None):
    """Lookup Elo rating with fuzzy name matching."""
    if not entity:
        return None

    # Use pre-loaded session cache if available (avoids new Neon connection per call)
    if _session_elo is not None:
        eff_surface = surface if surface else ''
        key = (entity.lower(), category, eff_surface)
        row = _session_elo.get(key)
        if not row:
            norm = entity.replace("FC ", "").replace(" FC", "").strip().lower()
            for (e, c, s), v in _session_elo.items():
                if c == category and (s or '') == eff_surface and norm in e:
                    row = v
                    break
        return row

    # PostgreSQL PRIMARY KEY makes surface NOT NULL; we store '' for categories without surface
    eff_surface = surface if surface else ''
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM elo_ratings WHERE entity = ? AND category = ? AND COALESCE(surface,'') = ?",
        (entity, category, eff_surface)
    ).fetchone()
    if not row:
        norm = entity.replace("FC ", "").replace(" FC", "").strip()
        row = conn.execute(
            "SELECT * FROM elo_ratings WHERE entity LIKE ? AND category = ? AND COALESCE(surface,'') = ?",
            (f"%{norm}%", category, eff_surface)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def elo_based_probability(home_team, away_team, category, surface=None, home_advantage=65):
    """
    Match outcome probabilities from Elo ratings.
    For football: includes home advantage + draw estimation.
    For tennis: head-to-head win probability (no draw).
    """
    rh = get_elo_rating(home_team, category, surface)
    ra = get_elo_rating(away_team, category, surface)
    if not rh or not ra:
        return None

    rating_h = rh["rating"]
    rating_a = ra["rating"]

    if category == "tennis":
        # No draw, no home advantage
        p_home = 1.0 / (1.0 + 10 ** ((rating_a - rating_h) / 400))
        return {
            "home": round(p_home * 100, 1),
            "away": round((1 - p_home) * 100, 1),
            "draw": None,
            "rating_home": round(rating_h),
            "rating_away": round(rating_a),
            "rating_diff": round(rating_h - rating_a),
            "games_home": rh["games"],
            "games_away": ra["games"],
        }
    else:
        # Football: home advantage + draw model
        ha = home_advantage if category == "football_club" else home_advantage
        p_home_raw = 1.0 / (1.0 + 10 ** ((rating_a - (rating_h + ha)) / 400))
        # Estimate draw probability (higher when teams are close)
        rating_gap = abs((rating_h + ha) - rating_a)
        draw_prob = 0.32 * math.exp(-rating_gap / 300)  # ~32% when equal, decreasing
        draw_prob = max(0.10, min(0.35, draw_prob))
        # Distribute remaining between home/away proportionally
        non_draw = 1 - draw_prob
        p_home = p_home_raw * non_draw
        p_away = (1 - p_home_raw) * non_draw
        return {
            "home": round(p_home * 100, 1),
            "draw": round(draw_prob * 100, 1),
            "away": round(p_away * 100, 1),
            "rating_home": round(rating_h),
            "rating_away": round(rating_a),
            "rating_diff": round((rating_h + ha) - rating_a),
            "games_home": rh["games"],
            "games_away": ra["games"],
        }


def get_elo_summary():
    """Top-rated entities per category."""
    conn = get_connection()
    out = {}
    for cat in ["football_club", "national", "tennis"]:
        if cat == "tennis":
            rows = conn.execute("""
                SELECT entity, rating, games, surface FROM elo_ratings
                WHERE category = ? AND surface = 'All'
                ORDER BY rating DESC LIMIT 15
            """, (cat,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT entity, rating, games, surface FROM elo_ratings
                WHERE category = ?
                ORDER BY rating DESC LIMIT 15
            """, (cat,)).fetchall()
        out[cat] = [dict(r) for r in rows]
    total = conn.execute("SELECT COUNT(*) FROM elo_ratings").fetchone()[0]
    conn.close()
    out["total"] = total
    return out


# ==========================================================
# BETIQ UNIFIED MODEL — fuse Pinnacle + xG + Elo signals
# ==========================================================

def fuse_signals(pin_probs, xg_probs, elo_probs):
    """
    INDUSTRY-STANDARD probability calculation.
    
    Key insight (from research): Pinnacle no-vig odds ARE the true probability.
    They incorporate all available information via sharp money.
    
    APPROACH:
    1. If Pinnacle available → use Pinnacle no-vig as TRUE PROBABILITY
       (xG/Elo become CONFIDENCE INDICATORS — do models agree with market?)
    2. If Pinnacle NOT available → fallback to xG/Elo weighted average
    
    Returns dict with home/draw/away probabilities + agreement metadata.
    """
    # PRIMARY PATH: Pinnacle available
    if pin_probs and pin_probs.get("home") is not None:
        # Use Pinnacle no-vig as TRUE PROBABILITY (industry standard)
        # xG and Elo are used for AGREEMENT/CONFIDENCE, not weighting
        
        fused = {
            "home": pin_probs.get("home"),
            "draw": pin_probs.get("draw"),
            "away": pin_probs.get("away"),
        }
        
        # Calculate AGREEMENT between Pinnacle and other signals
        # (Used for confidence indication, NOT for adjusting probability)
        #
        # CRITICAL: a liquid sharp line (Pinnacle) is essentially never 25pp wrong.
        # So when our (rough) model diverges by >25pp, OUR MODEL is the unreliable
        # outlier — NOT the market. Letting it into the agreement std produced a
        # FALSE "⚠ model diverges" seal (e.g. the national xG proxy rating Brazil at
        # 12% vs a 60% market). We therefore EXCLUDE such an outlier signal from the
        # agreement (still flagged separately for transparency).
        XG_OUTLIER_PP = 25
        signals_compared = []
        xg_unreliable = False
        if xg_probs and xg_probs.get("home") is not None:
            if abs(xg_probs["home"] - pin_probs["home"]) > XG_OUTLIER_PP:
                xg_unreliable = True   # wild outlier vs a sharp line → drop from agreement
            else:
                signals_compared.append(("xg", xg_probs.get("home")))
        elo_unreliable = False
        if elo_probs and elo_probs.get("home") is not None:
            if abs(elo_probs["home"] - pin_probs["home"]) > XG_OUTLIER_PP:
                elo_unreliable = True
            else:
                signals_compared.append(("elo", elo_probs.get("home")))

        # Standard deviation of home estimates across reliable signals (incl. Pinnacle)
        all_home_estimates = [pin_probs["home"]] + [s[1] for s in signals_compared]
        if len(all_home_estimates) >= 2:
            mean = sum(all_home_estimates) / len(all_home_estimates)
            variance = sum((x - mean) ** 2 for x in all_home_estimates) / len(all_home_estimates)
            std = variance ** 0.5

            if std < 5:
                agreement = "high"  # Models confirm market
            elif std < 12:
                agreement = "medium"
            else:
                agreement = "low"  # Models genuinely diverge (and are reliable enough to matter)
        else:
            agreement = "single"  # Only Pinnacle reliable here

        mismatch_flag = "xg_diverges_strongly" if xg_unreliable else None
        
        return {
            **fused,
            "method": "pinnacle_no_vig",
            "signals_used": ["pinnacle"] + [s[0] for s in signals_compared],
            "n_signals": 1 + len(signals_compared),
            "agreement": agreement,
            "mismatch_flag": mismatch_flag,
            "primary_source": "pinnacle",
        }
    
    # FALLBACK PATH: No Pinnacle — use xG + Elo weighted average
    # (For matches where bookmaker odds aren't available yet)
    FALLBACK_WEIGHTS = {"xg": 0.65, "elo": 0.35}
    
    signals = {}
    if xg_probs and xg_probs.get("home") is not None:
        signals["xg"] = xg_probs
    if elo_probs and elo_probs.get("home") is not None:
        signals["elo"] = elo_probs
    
    if not signals:
        return None
    
    total_w = sum(FALLBACK_WEIGHTS[s] for s in signals)
    fused = {"home": 0.0, "draw": 0.0, "away": 0.0}
    has_draw = any(s.get("draw") is not None for s in signals.values())
    
    for sig_name, probs in signals.items():
        w = FALLBACK_WEIGHTS[sig_name] / total_w
        fused["home"] += (probs.get("home") or 0) * w
        fused["away"] += (probs.get("away") or 0) * w
        if probs.get("draw") is not None:
            fused["draw"] += probs["draw"] * w
    
    # Normalize to 100%
    if has_draw:
        s = fused["home"] + fused["draw"] + fused["away"]
    else:
        fused["draw"] = None
        s = fused["home"] + fused["away"]
    if s > 0:
        fused["home"] = round(fused["home"] / s * 100, 1)
        fused["away"] = round(fused["away"] / s * 100, 1)
        if has_draw:
            fused["draw"] = round(fused["draw"] / s * 100, 1)
    
    return {
        **fused,
        "method": "model_fallback",
        "signals_used": list(signals.keys()),
        "n_signals": len(signals),
        "agreement": "fallback",
        "mismatch_flag": None,
        "primary_source": "models",
    }


def diagnose_national_xg(limit=15):
    """Diagnostic for the xG home/away bug report: for each current national/WC
    event, show the market vs xG vs Elo home/away probs, the underlying gf/ga, and
    whether the xG favourite matches the market favourite. Confirms whether the xG
    is home/away-swapped (it isn't) or just a weak proxy on some teams."""
    from collectors.national_xg import predict_national_match, _get_team_xg
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM odds_events ORDER BY commence_time ASC").fetchall()
    except Exception as e:
        conn.close()
        return {"error": str(e)}
    out = []
    for r in rows:
        d = dict(r)
        sport = (d.get("sport_name") or "").lower()
        if not any(k in sport for k in ("world cup", "nations", "euro", "copa")):
            continue
        home, away = d.get("home_team"), d.get("away_team")
        ref_h = d.get("bf_home") or d.get("pin_home")
        ref_a = d.get("bf_away") or d.get("pin_away")
        ref_d = d.get("bf_draw") or d.get("pin_draw")
        mkt = remove_vig_power(ref_h, ref_d, ref_a) if (ref_h and ref_a) else None
        nat = predict_national_match(home, away)
        elo = elo_based_probability(home, away, "national")
        ghx, gax = _get_team_xg(conn, home), _get_team_xg(conn, away)
        market_fav = (home if mkt["home"] >= mkt["away"] else away) if mkt else None
        xg_fav = (home if nat["prob_home"] >= nat["prob_away"] else away) if nat else None
        out.append({
            "match": f"{home} (home) vs {away} (away)",
            "market_home%": round(mkt["home"], 1) if mkt else None,
            "market_away%": round(mkt["away"], 1) if mkt else None,
            "xg_home%": nat["prob_home"] if nat else None,
            "xg_away%": nat["prob_away"] if nat else None,
            "elo_home%": elo["home"] if elo else None,
            "elo_away%": elo["away"] if elo else None,
            "home_gf/ga/n": [ghx["gf"], ghx["ga"], ghx["n"]] if ghx else "NOT FOUND",
            "away_gf/ga/n": [gax["gf"], gax["ga"], gax["n"]] if gax else "NOT FOUND",
            "market_favourite": market_fav,
            "xg_favourite": xg_fav,
            "xg_matches_market": (market_fav == xg_fav) if (market_fav and xg_fav) else None,
        })
        if len(out) >= limit:
            break
    conn.close()
    return {"events": len(out), "matches": out}
